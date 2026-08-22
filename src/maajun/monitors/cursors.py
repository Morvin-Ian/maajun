from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9_.@-]")
MAX_NAME = 60


def cursor_path(directory: Path, target: str, suffix: str = ".cursor") -> Path:
    """Where one source's position is kept.

    Named after the target so a human can tell what it belongs to, and
    hashed so two sources that sanitize to the same name — /srv/a/error.log
    and /srv/b/error.log — cannot share a cursor.
    """
    name = UNSAFE_IN_FILENAME.sub("_", target).strip("_")[-MAX_NAME:]
    digest = hashlib.sha256(target.encode()).hexdigest()[:8]
    return directory / f"{name}-{digest}{suffix}"


def usable(directory: Path | str | None, name: str) -> Path | None:
    """The cursor directory, created, or None if it cannot be used.

    A workdir that is not writable is not a reason to stop monitoring; it
    just means positions are kept in memory and a restart re-reads.
    """
    if directory is None:
        return None
    path = Path(directory).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("%s: cannot keep a cursor in %s (%s)", name, path, e)
        return None
    return path


@dataclass
class Position:
    """Where reading a file stopped, and which file that was."""

    inode: int
    offset: int


def read_position(path: Path | None) -> Position | None:
    if path is None:
        return None
    try:
        inode, offset = path.read_text().split()
        return Position(int(inode), int(offset))
    except (OSError, ValueError):
        return None


def write_position(path: Path | None, position: Position) -> None:
    if path is None:
        return
    try:
        path.write_text(f"{position.inode} {position.offset}\n")
    except OSError as e:
        log.debug("could not write %s: %s", path, e)
