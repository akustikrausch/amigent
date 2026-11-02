"""What both command clients share, amigactld's and amigentd's: same moves, different wire."""

from __future__ import annotations

from dataclasses import dataclass


def join_path(folder: str, name: str) -> str:
    """Amiga path of name inside folder ('RAM:' + 'x' or 'Work:Games' + 'x')."""
    return folder + name if folder.endswith((":", "/")) else f"{folder}/{name}"


@dataclass
class ExecResult:
    rc: int
    output: str


class VolumeFlush:
    """flush() for any client with exec(); mixed in, not inherited twice."""

    def flush(self, volumes) -> None:
        """Lock ON/OFF per volume: write-protecting forces the file system to commit what it holds."""
        for volume in volumes:
            self.exec(f"Lock {volume} ON")
            self.exec(f"Lock {volume} OFF")
