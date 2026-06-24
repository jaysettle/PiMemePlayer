"""Sample library: browse, upload, organize (folders/rename/delete).

All paths are validated to stay within the samples root to prevent traversal.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from werkzeug.utils import secure_filename


@dataclass
class Entry:
    name: str
    rel_path: str  # POSIX-style, relative to the samples root
    is_dir: bool
    size: int
    duration: Optional[float] = None


class Library:
    def __init__(self, root: Path, allowed_ext: Set[str]) -> None:
        self.root = Path(root)
        self.allowed_ext = {e.lower() for e in allowed_ext}
        self._duration_cache: Dict[str, Tuple[int, int, Optional[float]]] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path safety --------------------------------------------------------
    def _resolve(self, rel_path: str) -> Path:
        rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        root = self.root.resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise ValueError("Path escapes samples root")
        return target

    def _entry(self, p: Path) -> Entry:
        root = self.root.resolve()
        return Entry(
            name=p.name,
            rel_path=p.resolve().relative_to(root).as_posix(),
            is_dir=p.is_dir(),
            size=(p.stat().st_size if p.is_file() else 0),
            duration=(self.cached_duration_for_path(p) if p.is_file() else None),
        )

    # -- queries ------------------------------------------------------------
    def list(self, rel_path: str = "") -> List[Entry]:
        base = self._resolve(rel_path)
        if not base.is_dir():
            raise FileNotFoundError(rel_path)
        items = sorted(
            base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())
        )
        return [self._entry(p) for p in items]

    def path_for(self, rel_path: str) -> Path:
        target = self._resolve(rel_path)
        if not target.is_file():
            raise FileNotFoundError(rel_path)
        return target

    def duration_for_path(self, path: Path) -> Optional[float]:
        """Return audio duration in seconds when local tooling can read it."""
        p = Path(path)
        try:
            stat = p.stat()
        except OSError:
            return None
        key = str(p.resolve())
        cached = self._duration_cache.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        duration = self._probe_duration(p)
        self._duration_cache[key] = (stat.st_mtime_ns, stat.st_size, duration)
        return duration

    def cached_duration_for_path(self, path: Path) -> Optional[float]:
        p = Path(path)
        try:
            stat = p.stat()
            key = str(p.resolve())
        except OSError:
            return None
        cached = self._duration_cache.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        return None

    # -- mutations ----------------------------------------------------------
    def save_upload(self, file_storage, rel_dir: str = "") -> Entry:
        base = self._resolve(rel_dir)
        base.mkdir(parents=True, exist_ok=True)
        filename = secure_filename(file_storage.filename or "")
        if not filename:
            raise ValueError("Invalid filename")
        ext = Path(filename).suffix.lower()
        if ext not in self.allowed_ext:
            raise ValueError(f"Unsupported file type: {ext or '(none)'}")
        dest = self._unique(base / filename)
        file_storage.save(str(dest))
        return self._entry(dest)

    def make_folder(self, rel_dir: str, name: str) -> Entry:
        safe = secure_filename(name)
        if not safe:
            raise ValueError("Invalid folder name")
        rel = f"{rel_dir.rstrip('/')}/{safe}" if rel_dir else safe
        folder = self._resolve(rel)
        folder.mkdir(parents=True, exist_ok=False)
        return self._entry(folder)

    def rename(self, rel_path: str, new_name: str) -> Entry:
        target = self._resolve(rel_path)
        if not target.exists():
            raise FileNotFoundError(rel_path)
        safe = secure_filename(new_name)
        if not safe:
            raise ValueError("Invalid name")
        # Keep the original extension if the user didn't supply one.
        if target.is_file() and not Path(safe).suffix:
            safe += target.suffix
        dest = target.with_name(safe)
        if dest.exists():
            raise ValueError("Target already exists")
        target.rename(dest)
        return self._entry(dest)

    def delete(self, rel_path: str) -> None:
        target = self._resolve(rel_path)
        if target == self.root.resolve():
            raise ValueError("Refusing to delete the samples root")
        if not target.exists():
            raise FileNotFoundError(rel_path)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    # -- helpers ------------------------------------------------------------
    def _unique(self, dest: Path) -> Path:
        if not dest.exists():
            return dest
        stem, suffix = dest.stem, dest.suffix
        i = 1
        while True:
            candidate = dest.with_name(f"{stem}-{i}{suffix}")
            if not candidate.exists():
                return candidate
            i += 1

    def _probe_duration(self, p: Path) -> Optional[float]:
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                result = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(p),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
                if result.returncode == 0:
                    value = float(result.stdout.strip())
                    if value > 0:
                        return value
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass

        if p.suffix.lower() == ".wav":
            try:
                with closing(wave.open(str(p), "rb")) as wav:
                    rate = wav.getframerate()
                    if rate > 0:
                        return wav.getnframes() / float(rate)
            except (EOFError, OSError, wave.Error):
                pass
        return None
