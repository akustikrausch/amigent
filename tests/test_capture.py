"""1.6.0 (#3): a video capture device for the Amiga's own video output."""

import subprocess

import pytest
from PIL import Image

from amigent import capture, paths


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "FOLDER", tmp_path)


def test_settings_are_kept_per_amiga():
    assert capture.settings("192.0.2.1") == {"device": "", "auto": True}
    capture.configure("192.0.2.1", "USB Video", False)
    assert capture.settings("192.0.2.1") == {"device": "USB Video", "auto": False}
    capture.configure("192.0.2.1", auto=True)
    assert capture.settings("192.0.2.1") == {"device": "USB Video", "auto": True}
    assert capture.settings("192.0.2.2")["device"] == ""


def test_input_options_per_system():
    assert capture.input_args("USB Video", "win32") == ["-f", "dshow", "-i", "video=USB Video"]
    assert capture.input_args("0", "darwin")[:2] == ["-f", "avfoundation"]
    assert capture.input_args("/dev/video0", "linux") == ["-f", "v4l2", "-i", "/dev/video0"]
    assert capture.input_args("rtsp://192.0.2.5/live", "linux") == ["-i", "rtsp://192.0.2.5/live"]


def test_device_lists_are_read():
    windows = ('[dshow @ 0] "USB Video" (video)\n[dshow @ 0]   Alternative name "@device_pnp"\n'
               '[dshow @ 0] "Mikrofon (USB Video)" (audio)\n')
    assert capture.parse_devices(windows, "win32") == ["USB Video"]
    mac = ("[AVFoundation indev @ 0x1] AVFoundation video devices:\n"
           "[AVFoundation indev @ 0x1] [0] FaceTime HD Camera\n"
           "[AVFoundation indev @ 0x1] [1] USB3 Video\n"
           "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n"
           "[AVFoundation indev @ 0x1] [0] MacBook Microphone\n")
    assert capture.parse_devices(mac, "darwin") == ["FaceTime HD Camera", "USB3 Video"]


def test_jpeg_pictures_are_cut_from_a_stream():
    a, b = b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BB\xff\xd9"
    buffer = bytearray(b"junk" + a + b + b"\xff\xd8CC")
    assert list(capture.frames(buffer)) == [a, b]
    assert bytes(buffer) == b"\xff\xd8CC"  # the half picture waits for the rest


def test_noise_is_no_change():
    grey = Image.new("RGB", (320, 240), (100, 100, 100))
    noisy = Image.effect_noise((320, 240), 4).convert("RGB")
    noisy = Image.blend(grey, noisy, 0.03)
    assert not capture.differs(grey, noisy)
    title = grey.copy()
    title.paste((255, 255, 255), (0, 0, 320, 120))
    assert capture.differs(grey, title)


def test_no_device_says_how_to_find_one():
    with pytest.raises(capture.AmigaError, match="amiga_capture"):
        capture.grab("")


ffmpeg = pytest.mark.skipif(not capture.ffmpeg().endswith("ffmpeg"), reason="no ffmpeg")


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "amiga.mkv"
    subprocess.run([capture.ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x256:rate=10", "-t", "2", "-c:v", "mjpeg", str(path)], check=True)
    return str(path)


@ffmpeg
def test_one_picture_from_a_file(video):
    image = capture.grab(video)
    assert image.size == (320, 256)


@ffmpeg
def test_a_stream_brings_pictures_and_stops(video):
    stream = capture.Stream(video, fps=5)
    try:
        n, first = stream.next(0, timeout=10)
        assert first.startswith(b"\xff\xd8") and n >= 1
        n2, second = stream.next(n, timeout=10)
        assert n2 > n and second
    finally:
        stream.stop()
    assert not stream.running()
