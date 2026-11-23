"""An optional video capture source for the Amiga's own video output (#3).

Programs that switch the OS off (WHDLoad games, most demos) can't be seen
through amigentd, and a chipset screen shows on the Amiga's native output. A
USB HDMI capture stick behind an RGB-to-HDMI converter brings that output to
the PC. Amigent reads it with ffmpeg, which knows the capture devices of every
system (DirectShow on Windows, AVFoundation on macOS, V4L2 on Linux), so no
Python package is needed. The picture is only shown and saved, never used for
input.

The choice is kept per Amiga under the config folder: {"device": ..., "auto": ...}.
device may also be a video file or a stream URL, which is handy for trying it out.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths
from .errors import AmigaError

FPS = 10  # frames a second for the remote desktop; plenty for a title screen
IDLE = 10.0  # s without a watcher before the stream stops


def ffmpeg() -> str:
    """Path of ffmpeg, or "" when there is none."""
    found = os.environ.get("AMIGENT_FFMPEG") or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    return found or ""


def _need() -> str:
    exe = ffmpeg()
    if not exe:
        raise AmigaError("ffmpeg is not installed; the capture device is read with it "
                         "(Windows: winget install ffmpeg, macOS: brew install ffmpeg, "
                         "Linux: the package manager)")
    return exe


def _file(host: str) -> Path:
    from . import machines
    return paths.FOLDER / "capture" / f"{paths.safe(machines.identity(host) or host)}.json"


def settings(host: str) -> dict:
    """{"device": "", "auto": True} unless chosen otherwise."""
    try:
        got = json.loads(_file(host).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    return {"device": str(got.get("device", "")), "auto": bool(got.get("auto", True))}


def configure(host: str, device: str | None = None, auto: bool | None = None) -> dict:
    now = settings(host)
    if device is not None:
        now["device"] = device.strip()
    if auto is not None:
        now["auto"] = bool(auto)
    path = _file(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(now), encoding="utf-8")
    return now


def input_args(device: str, platform: str | None = None) -> list[str]:
    """ffmpeg's input options for a device name, a file or a URL."""
    platform = platform or sys.platform
    if "://" in device or Path(device).is_file():
        return ["-re", "-stream_loop", "-1", "-i", device] if Path(device).is_file() else ["-i", device]
    if platform == "win32":
        return ["-f", "dshow", "-i", device if device.startswith("video=") else f"video={device}"]
    if platform == "darwin":
        return ["-f", "avfoundation", "-framerate", "30", "-i", device]
    return ["-f", "v4l2", "-i", device]


def parse_devices(text: str, platform: str) -> list[str]:
    """Video devices from ffmpeg's -list_devices output."""
    if platform == "win32":
        return re.findall(r'"([^"]+)"\s*\(video\)', text)
    if platform == "darwin":
        out, video = [], False
        for line in text.splitlines():
            if "video devices" in line.lower():
                video = True
            elif "audio devices" in line.lower():
                video = False
            elif video and (m := re.search(r"\]\s*\[(\d+)\]\s*(.+)$", line)):
                out.append(m.group(2).strip())
        return out
    return []


def devices() -> list[str]:
    """The video capture devices this PC knows."""
    if sys.platform.startswith("linux"):
        return sorted(str(p) for p in Path("/dev").glob("video*"))
    exe = _need()
    args = ["-f", "dshow", "-i", "dummy"] if sys.platform == "win32" else ["-f", "avfoundation", "-i", ""]
    try:
        done = subprocess.run([exe, "-hide_banner", "-list_devices", "true", *args],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AmigaError(f"ffmpeg could not list the devices: {e}") from None
    return parse_devices(done.stderr + done.stdout, sys.platform)


def grab(device: str, timeout: float = 15):
    """One picture from the device, as a PIL image."""
    from PIL import Image
    if not device:
        raise AmigaError("no capture device chosen; amiga_capture(action='devices') lists them")
    exe = _need()
    cmd = [exe, "-hide_banner", "-loglevel", "error", *input_args(device), "-frames:v", "1",
           "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AmigaError(f"the capture device {device!r} gave no picture in {timeout:.0f} s") from None
    except OSError as e:
        raise AmigaError(f"ffmpeg did not start: {e}") from None
    if done.returncode or not done.stdout:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AmigaError(f"the capture device {device!r} gave no picture"
                         + (f": {said[-1]}" if said else ""))
    return Image.open(io.BytesIO(done.stdout)).convert("RGB")


def frames(buffer: bytearray):
    """Whole JPEG pictures at the front of buffer, taken out of it."""
    while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            buffer.clear()
            return
        end = buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
            del buffer[:start]
            return
        yield bytes(buffer[start:end + 2])
        del buffer[:end + 2]


class Stream:
    """ffmpeg turning the device into JPEG pictures; the newest one is kept.

    Starts with the first watcher, stops IDLE seconds after the last look."""

    def __init__(self, device: str, fps: int = FPS):
        self.device, self.fps = device, fps
        self.latest: bytes = b""
        self.count = 0
        self.error = ""
        self.looked = time.monotonic()
        self.changed = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            cmd = [_need(), "-hide_banner", "-loglevel", "error", *input_args(self.device),
                   "-r", str(self.fps), "-q:v", "5", "-f", "mjpeg", "-"]
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              stdin=subprocess.DEVNULL)
            except OSError as e:
                self.error = f"ffmpeg did not start: {e}"
                raise AmigaError(self.error) from None
            self.error = ""
            threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
            threading.Thread(target=self._idle, args=(self._proc,), daemon=True).start()

    def _read(self, proc) -> None:
        buffer = bytearray()
        while chunk := proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096):
            buffer += chunk
            for picture in frames(buffer):
                with self.changed:
                    self.latest, self.count = picture, self.count + 1
                    self.changed.notify_all()
        said = (proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
        self.error = said[-1] if said else "the capture device stopped"
        with self.changed:
            self.changed.notify_all()

    def _idle(self, proc) -> None:
        while proc.poll() is None:
            time.sleep(1)
            if time.monotonic() - self.looked > IDLE:
                self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def next(self, after: int, timeout: float = 5.0) -> tuple[int, bytes]:
        """The first picture newer than number after; (after, b"") on a timeout."""
        self.looked = time.monotonic()
        self.start()
        with self.changed:
            self.changed.wait_for(lambda: self.count > after or not self.running(), timeout)
            return (self.count, self.latest) if self.count > after else (after, b"")


def differs(a, b, threshold: float = 6.0) -> bool:
    """Two capture pictures differ beyond noise: the mean of the per-pixel difference
    of small greyscale copies, on 0..255. A capture stick's picture is never exactly
    the same twice, so a plain comparison would always say it changed."""
    from PIL import ImageChops, ImageStat
    small = (160, 120)
    x, y = a.convert("L").resize(small), b.convert("L").resize(small)
    return ImageStat.Stat(ImageChops.difference(x, y)).mean[0] > threshold
