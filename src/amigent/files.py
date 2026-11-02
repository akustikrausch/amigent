"""Files and trees between PC and Amiga, without paying a round trip per file.

On a plipbox every round trip is dead air, so trees go out as pipelined batches
on one command channel (see AmigaChannel.pipeline), folders are listed level by
level in one go, and files the other side already has (same size, same CRC-32)
are not sent at all. amigactld can't pipeline; it gets the old one-by-one walk.
"""

from __future__ import annotations

import zlib
from pathlib import Path, PurePosixPath

from .amigactl import AmigaError, join_path
from .channel import AmigaChannel, Request

CHECK_FROM = 16384  # below this a lone file just goes: asking costs about as much as sending
BATCH_BYTES = 4 << 20  # payload per pipeline batch, so a big tree never sits in RAM at once


def channel_of(ctl) -> AmigaChannel | None:
    """The pipelining backend behind Commands, or None for amigactld."""
    backend = getattr(ctl, "backend", ctl)
    return backend if isinstance(backend, AmigaChannel) else None


def crc_of(path: Path) -> int:
    crc = 0
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            crc = zlib.crc32(block, crc)
    return crc


def _batches(items: list, size_of) -> list[list]:
    """Cut items into runs of at most BATCH_BYTES (one oversized item rides alone)."""
    out, run, total = [], [], 0
    for item in items:
        if run and total + size_of(item) > BATCH_BYTES:
            out.append(run)
            run, total = [], 0
        run.append(item)
        total += size_of(item)
    return out + ([run] if run else [])


def _failure(done: int, total: int, errors: list[tuple[str, AmigaError]]) -> AmigaError:
    detail = "; ".join(f"{p}: {e}" for p, e in errors[:5])
    more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
    return AmigaError(f"copied {done} of {total} file(s); failed: {detail}{more}")


def _parents(path: str) -> list[str]:
    """'Work:a/b' -> ['Work:a', 'Work:a/b']: every folder on the way, volume excluded."""
    volume, _, rest = path.rpartition(":")
    current, out = f"{volume}:", []
    for part in PurePosixPath(rest).parts:
        current = join_path(current, part)
        out.append(current)
    return out


# PC -> Amiga

def put(ctl, src: Path, target: str) -> str:
    if not src.exists():
        raise AmigaError(f"{src} does not exist")
    ch = channel_of(ctl)
    if ch is None:
        return _put_one_by_one(ctl, src, target)
    if src.is_file():
        return _put_file(ch, src, target)
    return _put_tree(ch, src, target)


def _put_file(ch: AmigaChannel, src: Path, target: str) -> str:
    data = src.read_bytes()
    if len(data) >= CHECK_FROM and _same_on_amiga(ch, target, len(data), zlib.crc32(data)):
        return f"copied 0 file(s) to {target}; it was already there (same size and CRC)"
    ch.write_file(target, data)
    return f"copied 1 file(s) to {target}"


def _same_on_amiga(ch: AmigaChannel, path: str, size: int, crc: int) -> bool:
    """STAT first: a CRC over a file of the wrong size is seconds of 68000 wasted."""
    try:
        if int(ch.stat(path)["size"]) != size:
            return False
        return ch.checksum(path, size) == crc
    except AmigaError as e:
        if e.code is None:
            raise
        return False


def _put_tree(ch: AmigaChannel, src: Path, target: str) -> str:
    files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    folders = sorted({str(PurePosixPath(f).parent) for f in files} - {"."},
                     key=lambda f: (f.count("/"), f))
    sizes, present = _remote_sizes(ch, target, folders)
    unchanged = _unchanged(ch, target, [f for f in files if sizes.get(f) == (src / f).stat().st_size],
                           lambda f: (src / f))
    missing = ([] if "" in present else _parents(target)) + \
              [join_path(target, f) for f in folders if f not in present]
    todo = [f for f in files if f not in unchanged]
    errors: list[tuple[str, AmigaError]] = []
    first = True
    for run in _batches(todo, lambda f: (src / f).stat().st_size) or [[]]:
        dirs = missing if first else []
        first = False
        reqs = [Request.makedir(d) for d in dirs]
        reqs += [Request.write(join_path(target, f), (src / f).read_bytes()) for f in run]
        answers = ch.pipeline(reqs)
        # MAKEDIR on a folder that's there fails too; only a write tells if it mattered
        errors += [(f, a) for f, a in zip(run, answers[len(dirs):]) if isinstance(a, AmigaError)]
    if errors:
        raise _failure(len(todo) - len(errors), len(files), errors)
    skipped = f", {len(unchanged)} unchanged ones skipped" if unchanged else ""
    return f"copied {len(todo)} file(s) to {target}{skipped}"


def _remote_sizes(ch: AmigaChannel, target: str, folders: list[str]) -> tuple[dict, set]:
    """What the target tree already holds: {relative file: size} and the folders that exist.

    All folders are known from the PC side, so one batch lists them all at once."""
    rels = [""] + folders
    answers = ch.pipeline([Request.listing(join_path(target, r) if r else target) for r in rels])
    sizes, present = {}, set()
    for rel, answer in zip(rels, answers):
        if isinstance(answer, AmigaError):
            continue
        present.add(rel)
        for e in answer:
            if e["type"] == "FILE":
                sizes[f"{rel}/{e['name']}" if rel else e["name"]] = e["size"]
    return sizes, present


def _unchanged(ch: AmigaChannel, root: str, candidates: list[str], local_of) -> set[str]:
    """Of the same-size candidates, those whose CRC matches too. One batch for all."""
    if not candidates:
        return set()
    local = {f: crc_of(local_of(f)) for f in candidates}
    answers = ch.pipeline([Request.checksum(join_path(root, f), local_of(f).stat().st_size)
                           for f in candidates])
    return {f for f, crc in zip(candidates, answers) if crc == local[f]}


def _put_one_by_one(ctl, src: Path, target: str) -> str:
    """amigactld: no pipelining, so the classic walk."""
    files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
    made: set = set()
    if src.is_dir():
        makedirs(ctl, target, made)
    for f in files:
        dest = target
        if src.is_dir():
            rel = f.relative_to(src)
            if len(rel.parts) > 1:
                makedirs(ctl, join_path(target, rel.parent.as_posix()), made)
            dest = join_path(target, rel.as_posix())
        ctl.write_file(dest, f.read_bytes())
    return f"copied {len(files)} file(s) to {target}"


def makedirs(ctl, path: str, made: set | None = None) -> None:
    """Create path with all missing parents; folders that are already there are fine."""
    for current in _parents(path):
        if made is not None:
            if current.lower() in made:
                continue
            made.add(current.lower())
        try:
            ctl.makedir(current)
        except AmigaError as e:
            if e.code is None:
                raise
            try:  # already there is fine; anything else is a real error
                if ctl.stat(current)["type"].lower() != "dir":
                    raise e
            except AmigaError:
                raise e from None


# Amiga -> PC

def get(ctl, source: str, target: Path) -> str:
    ch = channel_of(ctl)
    if ch is None:
        return _save(target, ctl.read_file(source))
    # STAT and READ in one go: a file costs one round trip, a folder gets a failed READ
    info, data = ch.pipeline([Request.stat(source), Request.read_file(source)])
    if isinstance(info, AmigaError):
        raise info
    if info["type"] == "DIR":
        return _get_tree(ch, source, target)
    if isinstance(data, AmigaError):
        raise data
    return _save(target, data)


def _save(target: Path, data: bytes) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"saved {len(data)} bytes to {target}"


def _get_tree(ch: AmigaChannel, source: str, target: Path) -> str:
    # '..' is a perfectly legal AmigaDOS file name and a perfectly bad idea on the PC
    entries = [e for e in ch.walk(source) if not isinstance(e, dict)
               or not {".", ".."} & set(e["name"].split("/"))]
    files ={e["name"]: e["size"] for e in entries if isinstance(e, dict) and e["type"] == "FILE"}
    for e in entries:
        if isinstance(e, dict) and e["type"] == "DIR":
            (target / e["name"]).mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    same = [f for f, size in files.items()
            if (target / f).is_file() and (target / f).stat().st_size == size]
    unchanged = _unchanged(ch, source, same, lambda f: target / f) if same else set()
    todo = [f for f in files if f not in unchanged]
    errors: list[tuple[str, AmigaError]] = [(e.folder, e) for e in entries
                                            if isinstance(e, AmigaError)]
    total = 0
    for run in _batches(todo, lambda f: files[f]):
        answers = ch.pipeline([Request.read_file(join_path(source, f), files[f]) for f in run])
        for f, data in zip(run, answers):
            if isinstance(data, AmigaError):
                errors.append((f, data))
            else:
                (target / f).write_bytes(data)
                total += len(data)
    if errors:
        raise _failure(len(todo) - len(errors), len(files), errors)
    skipped = f", {len(unchanged)} unchanged ones skipped" if unchanged else ""
    return f"saved {len(todo)} file(s), {total} bytes, to {target}{skipped}"
