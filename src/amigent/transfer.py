"""Verified file transfers through a daemon advertising publish and readrange."""

from __future__ import annotations

import secrets
import sys
import tempfile
import zlib
from pathlib import Path

from .channel import AmigaChannel, Commands
from .client import join_path
from .errors import AmigaError

BLOCK = 65535
MAX_SIZE = 0xffffffff


def _retry(ch, operation):
    """Retry a replayable operation once after a lost reply on this channel."""
    try:
        return operation(False)
    except (AmigaError, OSError, EOFError) as exc:
        if isinstance(exc, AmigaError) and exc.code is not None:
            raise
        ch.close()
        ch.connect()
        return operation(True)


def can_stage(ch) -> bool:
    return isinstance(ch, AmigaChannel) and all(ch.file_feature(name) for name in
                                                ("publish", "appendat", "readrange"))


def send(ch, source, target: str, size: int, crc: int, progress=None) -> None:
    """Publish a seekable binary stream only after verifying its complete contents."""
    if not 0 <= size <= MAX_SIZE:
        raise AmigaError("this daemon supports transfers through 4294967295 bytes", 1)
    parent = target[:max(target.rfind(":"), target.rfind("/")) + 1]
    folder = parent + "ag-" + secrets.token_hex(12)
    stage = join_path(folder, "data")
    ch.makedir(folder)  # only use a directory this operation created
    publishing = False
    try:
        offset = 0
        source.seek(0)
        while True:
            block = source.read(BLOCK)
            if offset and not block:
                break
            if offset + len(block) > size:
                raise AmigaError("source grew during upload", 1)

            def write(_):
                ch.append_at(stage, offset, block)
                if ch.read_range(stage, offset, len(block)) != block:
                    raise AmigaError(f"staging file differs at byte {offset}", 1)

            _retry(ch, write)
            offset += len(block)
            if progress:
                progress(offset)
            if not block:
                break
        if offset != size or int(ch.stat(stage)["size"]) != size or ch.checksum(stage, size) != crc:
            raise AmigaError("staging file differs from the original source", 1)

        publishing = True

        def publish(replay):
            if replay:
                names = {entry["name"] for entry in ch.list_dir(folder)}
                if "data.old" in names:
                    raise AmigaError("previous replacement left a recovery copy", 1)
                if "data" not in names:
                    if int(ch.stat(target)["size"]) == size and ch.checksum(target, size) == crc:
                        return
                    raise AmigaError("destination changed after replacement", 1)
            ch.publish(stage, target)

        _retry(ch, publish)
        ch.delete(folder)
    except (AmigaError, OSError, EOFError) as exc:
        if publishing:
            raise AmigaError(f"{exc}. Inspect {folder} for the staging or recovery file", 1) from exc
        try:
            for entry in ch.list_dir(folder):
                if entry["name"] == "data":
                    ch.delete(stage)
            ch.delete(folder)
        except (AmigaError, OSError, EOFError):
            raise AmigaError(f"{exc}. Unfinished upload in {folder}", 1) from exc
        raise


def receive(ch, source: str, target: Path, size: int) -> None:
    """Keep the local destination until a complete download matches the source."""
    if not 0 <= size <= MAX_SIZE:
        raise AmigaError("file size exceeds the range supported by this daemon", 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        offset = crc = 0
        with temporary.open("wb") as stream:
            while offset < size:
                length = min(BLOCK, size - offset)
                block = _retry(ch, lambda _: ch.read_range(source, offset, length))
                if len(block) != length:
                    raise AmigaError(f"source changed at byte {offset}", 1)
                stream.write(block)
                crc = zlib.crc32(block, crc)
                offset += len(block)
        if int(ch.stat(source)["size"]) != size or ch.checksum(source, size) != crc:
            raise AmigaError("download differs from source", 1)
        if target.exists():
            temporary.chmod(target.stat().st_mode & 0o777)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def upload(host: str, local: Path, target: str) -> None:
    from .files import crc_of

    ctl = Commands(host, timeout=60)
    try:
        ctl.connect()
        if not can_stage(ctl.backend):
            raise AmigaError("update amigentd to use verified staged uploads")
        with local.open("rb") as stream:
            send(ctl.backend, stream, target, local.stat().st_size, crc_of(local))
        print("verified", flush=True)
    finally:
        ctl.close()


if __name__ == "__main__":
    upload(sys.argv[1], Path(sys.argv[2]), sys.argv[3])
