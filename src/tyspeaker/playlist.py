"""Ordered playlist over the sample library.

The order is user-defined (drag-to-reorder in the UI) and persisted in settings.
Only existing files are kept; missing ones are pruned on read.
"""

from __future__ import annotations

import random
from typing import List, Optional

from .library import Library
from .settings import Settings


class Playlist:
    def __init__(self, library: Library, settings: Settings) -> None:
        self.library = library
        self.settings = settings

    def order(self) -> List[str]:
        """Return persisted order, pruned to files that still exist."""
        kept, changed = [], False
        for rel in self.settings.playlist:
            try:
                self.library.path_for(rel)
                kept.append(rel)
            except (FileNotFoundError, ValueError):
                changed = True
        if changed:
            self.settings.playlist = kept
        return kept

    def entries(self) -> List[dict]:
        out = []
        for i, rel in enumerate(self.order()):
            try:
                p = self.library.path_for(rel)
            except (FileNotFoundError, ValueError):
                continue
            out.append(
                {
                    "index": i,
                    "rel_path": rel,
                    "name": p.name,
                    "size": p.stat().st_size,
                    "duration": self.library.cached_duration_for_path(p),
                }
            )
        return out

    def add(self, rel: str) -> None:
        order = self.order()
        if rel not in order:
            order.append(rel)
            self.settings.playlist = order

    def remove(self, rel: str) -> None:
        self.settings.playlist = [r for r in self.order() if r != rel]

    def reorder(self, new_order: List[str]) -> List[str]:
        existing = set(self.order())
        # keep requested items that exist, then append any that were omitted
        result = [r for r in new_order if r in existing]
        result += [r for r in self.order() if r not in result]
        self.settings.playlist = result
        return result

    def index_of(self, rel: str) -> Optional[int]:
        order = self.order()
        return order.index(rel) if rel in order else None

    def next_index(self, current: Optional[int], mode: str) -> Optional[int]:
        order = self.order()
        if not order:
            return None
        if mode == "random":
            if len(order) == 1:
                return 0
            choices = [i for i in range(len(order)) if i != current]
            return random.choice(choices)
        if current is None:
            return 0
        return (current + 1) % len(order)

    def rel_at(self, index: int) -> Optional[str]:
        order = self.order()
        return order[index] if 0 <= index < len(order) else None
