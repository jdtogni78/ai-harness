"""The one I/O wrapper: a timestamped append-logger.

Returns a ``log(msg)`` closure rather than configuring the stdlib ``logging``
module, to match the existing scripts' plain ``<timestamp> <msg>`` line format
exactly (dashboards / muscle memory parse these files).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Union


def make_logger(path: Union[str, Path], utc: bool = False) -> Callable[[str], None]:
    """Build an append-logger writing ``<timestamp> <msg>`` lines to *path*.

    ``utc=False`` (supervisor) -> local ``YYYY-MM-DD HH:MM:SS`` to match
    ``manager.log``. ``utc=True`` (usage monitor) -> ISO-8601 UTC to match
    ``usage-limit-monitor.log``.
    """
    path = Path(path)

    def log(msg: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if utc:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        else:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a") as f:
            f.write(f"{ts} {msg}\n")

    return log
