from amigent.follow import Follower, file_fetch


def test_a_log_reaches_the_pc_piece_by_piece_and_survives_the_amiga_going(tmp_path):
    log = {"data": b"one\n"}
    alive = {"yes": True}

    def stat(path):
        if not alive["yes"]:
            raise OSError("no route to host")
        return {"size": len(log["data"])}

    def read_from(path, start):
        return log["data"][start:]

    target = tmp_path / "pt.log"
    f = Follower("PT:pt.log", target, file_fetch(read_from, stat, "PT:pt.log"), start=0)
    f.target.parent.mkdir(parents=True, exist_ok=True)
    f.tick()
    log["data"] += b"two\n"
    f.tick()
    f.tick()  # nothing new: nothing written
    alive["yes"] = False
    f.tick()
    f.tick()  # one mark only
    text = target.read_text()
    assert "one\ntwo\n" in text and text.count("does not answer") == 1
    alive["yes"] = True
    log["data"] = b"new\n"  # the program started its log again
    f.tick()
    text = target.read_text()
    assert "answers again" in text and text.endswith("new\n") and f.written == 12


def test_reading_only_what_is_new(tmp_path):
    reads = []

    def read_from(path, start):
        reads.append(start)
        return b"x" * (10 - start)

    f = Follower("RAM:log", tmp_path / "l", file_fetch(read_from, lambda p: {"size": 10}, "RAM:log"),
                 start=4)
    f.tick()
    f.tick()
    assert reads == [4] and f.position == 10 and (tmp_path / "l").read_bytes() == b"x" * 6
