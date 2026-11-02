"""The one error everybody throws, whichever daemon is on the other end."""

from __future__ import annotations


class AmigaError(Exception):
    """The daemon said no, or the wire went dead. code is set when the Amiga answered."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code
