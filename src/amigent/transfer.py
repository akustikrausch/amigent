"""Resumable upload for big files over slow links.

Usage: python -m amigent.transfer HOST LOCAL AMIGA_PATH
Sends blocks and keeps going after dropped connections or an Amiga restart.
"""

from __future__ import annotations

import sys
import time
import zlib
from pathlib import Path

from .amigactl import AmigaCtl, AmigaError

BLOCK = 256 * 1024
RETRY_SECONDS = 30


def remote_size(ctl: AmigaCtl, path: str) -> int:
    try:
        return int(ctl.stat(path)["size"])
    except AmigaError as e:
        if e.code is None:
            raise
        return 0


def upload(host: str, local: Path, target: str) -> None:
    total = local.stat().st_size
    while True:
        ctl = AmigaCtl(host, timeout=60)
        try:
            ctl.connect()
            done = remote_size(ctl, target)
            started, begin = time.monotonic(), done
            with local.open("rb") as f:
                f.seek(done)
                while done < total:
                    ctl.write_file(target, f.read(BLOCK), append=done > 0)
                    done = min(done + BLOCK, total)
                    rate = (done - begin) / (time.monotonic() - started) / 1024
                    print(f"{done // 2**20}/{total // 2**20} MB, {rate:.0f} KB/s", flush=True)
            crc = 0
            with local.open("rb") as f:
                while block := f.read(2**20):
                    crc = zlib.crc32(block, crc)
            ctl.timeout = 600
            ctl._sock.settimeout(600)
            if ctl.checksum(target) != crc:
                raise SystemExit("checksum mismatch, file on the Amiga is damaged")
            print("verified", flush=True)
            return
        except (OSError, AmigaError) as e:
            print(f"interrupted: {e}", flush=True)
            time.sleep(RETRY_SECONDS)
        finally:
            ctl.close()


if __name__ == "__main__":
    upload(sys.argv[1], Path(sys.argv[2]), sys.argv[3])
