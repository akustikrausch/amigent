"""Pipelines, skipped files, pictures and deadlines: the slow-link work, offline."""

import io
import threading
import time
import zlib

import pytest
from PIL import Image

import amigent.server as server
from amigent import files, pictures, watch
from amigent.amigactl import AmigaError, ExecResult
from amigent.channel import AmigaChannel, Request, perform
from fakeamiga import FakeAmiga, SlowLink, desktop


@pytest.fixture
def amiga():
    fake = FakeAmiga()
    yield fake
    fake.close()


def channel(port):
    return AmigaChannel("127.0.0.1", port, timeout=5, key="")


def tree(root, count=12):
    for i in range(count):
        f = root / f"d{i % 3}" / f"f{i}.bin"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(bytes([i]) * (100 + i))
    return root


def test_pipeline_keeps_answers_in_order_and_survives_refusals(amiga):
    amiga.files["RAM:a"] = b"one"
    ch = channel(amiga.port)
    answers = ch.pipeline([Request.read_file("RAM:a"), Request.write("RAM:nope/x", b"lost"),
                           Request.checksum("RAM:a"), Request.read_file("RAM:missing"),
                           Request.write("RAM:b", b"two")])
    assert answers[0] == b"one"
    assert isinstance(answers[1], AmigaError) and answers[1].code == 1
    assert answers[2] == zlib.crc32(b"one")
    assert isinstance(answers[3], AmigaError)
    assert answers[4] is None and amiga.files["RAM:b"] == b"two"
    assert ch.read_file("RAM:b") == b"two"  # still in step afterwards


def test_tree_goes_up_once_and_only_changes_go_again(amiga, tmp_path):
    src = tree(tmp_path / "src")
    ch = channel(amiga.port)
    assert files.put(ch, src, "RAM:t/deep").startswith("copied 12 file(s)")
    assert amiga.files["RAM:t/deep/d1/f4.bin"] == bytes([4]) * 104
    writes = amiga.counts[23]
    (src / "d0" / "f0.bin").write_bytes(b"changed!")
    report = files.put(ch, src, "RAM:t/deep")
    assert "copied 1 file(s)" in report and "11 unchanged" in report
    assert amiga.counts[23] == writes + 1


def test_tree_comes_back_and_a_folder_get_needs_no_extra_round_trip(amiga, tmp_path):
    for i in range(5):
        amiga.add(f"Work:g/sub{i % 2}/x{i}", bytes([i]) * 50)
    ch = channel(amiga.port)
    report = files.get(ch, "Work:g", tmp_path / "g")
    assert report.startswith("saved 5 file(s)")
    assert (tmp_path / "g/sub1/x3").read_bytes() == bytes([3]) * 50
    assert "5 unchanged" in files.get(ch, "Work:g", tmp_path / "g")
    amiga.files["Work:one"] = b"single"
    assert files.get(ch, "Work:one", tmp_path / "one").startswith("saved 6 bytes")


def test_a_dotdot_name_on_the_amiga_stays_on_the_amiga(amiga, tmp_path):
    amiga.add("Work:evil/../x", b"nope")
    amiga.dirs.add("Work:evil/..")
    amiga.add("Work:evil/fine", b"ok")
    files.get(channel(amiga.port), "Work:evil", tmp_path / "in")
    assert (tmp_path / "in/fine").exists() and not (tmp_path / "x").exists()


def test_big_single_file_already_there_is_not_sent(amiga, tmp_path):
    data = bytes(range(256)) * 100
    local = tmp_path / "big"
    local.write_bytes(data)
    ch = channel(amiga.port)
    files.put(ch, local, "RAM:big")
    assert "already there" in files.put(ch, local, "RAM:big")
    assert amiga.counts[23] == 1


def test_recursive_listing_is_one_batch_per_level(amiga):
    for i in range(6):
        amiga.add(f"RAM:r/a{i}/b/c{i}", b"z")
    names = [e["name"] for e in channel(amiga.port).list_dir("RAM:r", True)]
    assert "a3/b/c3" in names and names.index("a0") < names.index("a0/b")


def test_pipelining_beats_the_round_trips(amiga, tmp_path):
    # long latency on purpose: the round trips must dominate, not the CI box's CPU
    link = SlowLink(amiga.port, rate=2_000_000, latency=0.2)
    src = tree(tmp_path / "src", 30)
    start = time.monotonic()
    files.put(channel(link.port), src, "RAM:p")
    took = time.monotonic() - start
    link.close()
    assert len([f for f in amiga.files if f.startswith("RAM:p/")]) == 30
    assert took < 30 * 0.4 / 3  # one by one: 30 round trips of 400 ms, 12 s


def test_amigactld_walks_through_requests_one_by_one():
    calls = []

    class Ctl:
        def write_file(self, path, data, append=False):
            calls.append(("write", path, data))

        def exec(self, command, cd=None, timeout=None):
            calls.append(("exec", command, timeout))
            return ExecResult(0, "")

        def stat(self, path):
            raise AmigaError("not there", 205)

    answers = perform(Ctl(), [Request.write("RAM:x", b"1"), Request.exec("rx RAM:x", timeout=60),
                              Request.stat("RAM:y")])
    assert calls == [("write", "RAM:x", b"1"), ("exec", "rx RAM:x", 60)]
    assert isinstance(answers[2], AmigaError)


def test_palette_png_is_exact_and_small():
    picture = desktop(640, 256).quantize(16).convert("RGB")
    data = pictures.png(picture)
    back = Image.open(io.BytesIO(data))
    assert back.mode == "P"
    assert list(back.convert("RGB").getdata()) == list(picture.getdata())
    plain = io.BytesIO()
    picture.save(plain, "PNG", optimize=True)
    assert len(data) < len(plain.getvalue())


def test_big_screens_shrink_to_what_the_model_takes():
    image, scale = pictures.fit(Image.new("RGB", (1920, 1080)))
    assert image.width * image.height <= pictures.MAX_PIXELS and scale > 1.3
    small, one = pictures.fit(Image.new("RGB", (640, 256)))
    assert small.size == (640, 256) and one == 1.0


class SlowScreen:
    """A screen whose every poll takes longer than anyone wants to wait."""

    def __init__(self, delay):
        self.delay, self.frame, self.changes = delay, None, []

    def snapshot(self):
        return self.frame

    def poll(self, *region):
        time.sleep(self.delay)
        if self.frame is None:  # a forced first frame: every tile comes, none is news
            self.frame = object()
            self.changes.append(time.monotonic())
        return 1

    def changed_since(self, since, *region):
        return sum(1 for t in self.changes if t > since)


def test_wait_change_keeps_its_deadline_on_a_slow_link():
    lookout = watch.Lookout()
    start = time.monotonic()
    report, changed = watch.wait_change(SlowScreen(2.0), lambda fn: fn(), lookout,
                                        (0, 0, None, None), start, 0.4)
    took = time.monotonic() - start
    assert not changed and "still on its way" in report
    assert took < 0.8


def test_a_quiet_screen_on_a_fast_link_is_no_slow_link():
    lookout = watch.Lookout()
    screen = SlowScreen(0.0)
    screen.frame = object()  # the picture is there: polls come back at once
    report, changed = watch.wait_change(screen, lambda fn: fn(), lookout, (0, 0, None, None),
                                        time.monotonic(), 0.3)
    assert not changed and report == "no change within 0.3 s"


def test_a_first_picture_is_not_a_change():
    lookout = watch.Lookout()
    screen = SlowScreen(0.0)
    report, changed = watch.wait_change(screen, lambda fn: fn(), lookout, (0, 0, None, None),
                                        time.monotonic() - 10, 0.3, sleep=lambda s: time.sleep(0.01))
    assert not changed and report.startswith("no change")


def test_unchanged_screenshot_comes_back_as_a_note(monkeypatch):
    class Screen:
        width, height = 640, 256
        tiles = 0

        def capture(self, *region):
            return Image.new("RGB", (640, 256), (170, 170, 170))

        def changed_since(self, since, *region):
            return self.tiles

        def pause_left(self, ask=True):
            return 0

    fake = Screen()
    monkeypatch.setattr(server, "screen", fake)
    monkeypatch.setattr(server, "_last_shot", None)
    assert len(server.amiga_screenshot()) == 2
    assert len(server.amiga_screenshot()) == 1  # nothing new: a note, no picture
    assert len(server.amiga_screenshot()) == 2  # asked again: the picture after all
    fake.tiles = 3
    assert len(server.amiga_screenshot()) == 2


def test_amiga_put_and_get_through_the_tools(amiga, tmp_path, monkeypatch):
    from amigent.channel import Commands

    ctl = Commands("127.0.0.1", 1, amiga.port, timeout=5)
    monkeypatch.setattr(server, "ctl", ctl)
    monkeypatch.setattr(server, "_exclusive", lambda name=None: threading.Lock())
    src = tree(tmp_path / "src", 4)
    assert server.amiga_put(str(src), "RAM:w").startswith("copied 4")
    assert server.amiga_get("RAM:w", str(tmp_path / "back")).startswith("saved 4 file(s)")
    assert (tmp_path / "back/d1/f1.bin").read_bytes() == bytes([1]) * 101
