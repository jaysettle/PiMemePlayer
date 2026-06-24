"""Central logging for TySpeaker.

Logs go to two places:
  * stdout  -> captured by systemd, view with ``journalctl -u tyspeaker -f``
  * an in-memory ring buffer -> served at ``/api/logs`` and shown in the
    Settings > Diagnostics > Logs panel (so you can debug from the browser
    without SSH).

Set the env var ``TYSPEAKER_LOG_LEVEL=DEBUG`` for verbose output.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from typing import Deque, List

_RING: Deque[str] = deque(maxlen=500)
_configured = False


class _RingHandler(logging.Handler):
    """Keep the last N formatted log lines in memory for the UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(self.format(record))
        except Exception:  # never let logging crash the app
            pass


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    level_name = os.environ.get("TYSPEAKER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"
    )
    root = logging.getLogger("tyspeaker")
    root.setLevel(level)
    for handler in (logging.StreamHandler(), _RingHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.propagate = False
    _configured = True
    root.info("logging configured (level=%s)", level_name)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"tyspeaker.{name}")


def recent_logs(limit: int = 200) -> List[str]:
    limit = max(1, min(500, int(limit)))
    return list(_RING)[-limit:]
