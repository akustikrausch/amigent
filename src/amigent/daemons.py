"""Which packaged amigentd can run on a machine, and whether an update may replace one.

The catalogue is the extension point. A future CPU-specific file is another entry
with the CPU it requires and whether that build is supported. A larger CPU
number is not treated as a better daemon: preference is the entry's own
specialisation, and only among supported builds that can actually run.

``amigentd.080`` may exist in the source tree as a historical artifact. It is
excluded from the Python package and is never selected automatically. A Vampire
runs the plain ``amigentd``.

amigactld is not one of these files. It is the older command transport used
before protocol 3, fetched separately, and it does not change TLS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Instruction sets these builds were compiled for. 68010 runs 68000 code and
# does not run a 68020 binary. The numbers are only compared that way.
CPU_68000, CPU_68010 = 68000, 68010
CPU_68020, CPU_68030, CPU_68040, CPU_68060, CPU_68080 = 68020, 68030, 68040, 68060, 68080

HUNK_HEADER = b"\x00\x00\x03\xf3"  # an Amiga executable starts with HUNK_HEADER

SUPPORTED, EXPERIMENTAL = "supported", "experimental"


@dataclass(frozen=True)
class Spec:
    name: str
    min_cpu: int
    specialisation: int
    status: str
    family: str
    target: str
    label: str
    system: str = ""  # an OS the build needs, such as ApolloOS; "" for any

    @property
    def verified(self) -> bool:
        return self.status == SUPPORTED


# vbcc -cpu=68000, vbcc -cpu=68020, and the historical vbcc -cpu=68080 build
# (retired from packages). See docs/PROTOCOL.md.
SPECS: tuple[Spec, ...] = (
    Spec("amigentd.000", CPU_68000, 0, SUPPORTED, "68000", "68000", "68000"),
    Spec("amigentd", CPU_68020, 1, SUPPORTED, "general", "68020", "general"),
    Spec("amigentd.080", CPU_68080, 0, EXPERIMENTAL, "68080", "68080", "68080 experimental"),
    # optional, not shipped in upstream release packages (build from src/native/amiga/);
    # picked only where ApolloOS is recognised, and never replaced by a less specialised build
    Spec("amigentd.apollo", CPU_68080, 2, SUPPORTED, "apollo", "68080", "ApolloOS", "ApolloOS"),
)


@dataclass(frozen=True)
class Machine:
    cpu: int | None
    apollo: bool = False  # ApolloOS runs here


def fits(spec: Spec, machine: Machine) -> bool:
    """True when this build's instructions can run on the machine."""
    if spec.system == "ApolloOS" and not machine.apollo:
        return False
    if machine.cpu is None:
        return spec.min_cpu <= CPU_68000
    return machine.cpu >= spec.min_cpu


def choose(machine: Machine, present: set[str]) -> Spec | None:
    """The preferred supported build that is packaged and can run, or None."""
    fits_machine = [
        spec for spec in SPECS
        if spec.verified and spec.name in present and fits(spec, machine)
    ]
    if not fits_machine:
        return None
    return max(fits_machine, key=lambda spec: spec.specialisation)


def available_names(directory: Path) -> set[str]:
    return {spec.name for spec in SPECS if (directory / spec.name).is_file()}


def detect_cpu(run) -> int | None:
    """CPU CHECK 68020: 0 means this CPU or better, 5 means less, anything else stops.

    ``run`` returns the exit code. The supported builds only part at the 68020,
    so one check is all a choice needs; a 68000 here stands for a 68010 too,
    which runs the same build. A build that needs more adds its check here.
    An unrecognised answer is an unknown CPU.
    """
    code = run("CPU CHECK 68020")
    if code == 5:
        return CPU_68000
    if code != 0:
        return None
    return CPU_68020


# An installed Apollo daemon is recognised without a packaged file, by text that only the
# AMIGENT_APOLLO build of amigentd.c contains. That survives an upstream package that has
# no amigentd.apollo, which is what makes an update safe there, for any Apollo build.
APOLLO_MARKERS = (
    b"tracepoints are not supported on this Apollo build yet",
    b"that range includes Apollo registers that a read can change",
)


def is_apollo_daemon(data: bytes) -> bool:
    """True when ``data`` is an Apollo-family amigentd, whether or not it is packaged."""
    return data.startswith(HUNK_HEADER) and any(mark in data for mark in APOLLO_MARKERS)


def apollo_spec() -> Spec:
    return next(spec for spec in SPECS if spec.family == "apollo")


def installed_specialisation(data: bytes, directory: Path) -> tuple[int, str]:
    """(specialisation, label) of a supported installed daemon, or (-1, "") when unknown.

    The packaged bytes say it exactly. An Apollo daemon the package does not carry
    (no amigentd.apollo, or another build of it) is still an Apollo daemon.
    """
    spec = identify(data, directory)
    best = (spec.specialisation, spec.label) if spec and spec.verified else (-1, "")
    if is_apollo_daemon(data):
        apollo = apollo_spec()
        if apollo.specialisation > best[0]:
            best = (apollo.specialisation, apollo.label)
    return best


def specialisation_guard(installed: bytes | None, candidate_name: str, directory: Path) -> str:
    """Why the installed daemon must stay, or "" when it may be replaced.

    A more specialised installed daemon (ApolloOS) is not replaced by a less
    specialised candidate (classic amigentd) on a normal update, whatever the version
    numbers say. This keeps an installed Apollo daemon when the package has no
    amigentd.apollo, or selection falls back to classic because detection failed.
    ``setup.update`` consults it only through ``consider_replacement``; ``--force``
    skips that comparison, as the ``apollo_without_build`` policy documents.
    """
    if installed is None:
        return ""
    candidate = next((spec for spec in SPECS if spec.name == candidate_name), None)
    if candidate is None or not candidate.verified:
        return ""
    level, label = installed_specialisation(installed, directory)
    if level > candidate.specialisation:
        return (f"the installed {label} daemon was left in place. "
                f"{candidate.label} would replace it with a less specialised build.")
    return ""


def identify(data: bytes, directory: Path) -> Spec | None:
    """The catalogue entry whose packaged bytes are exactly ``data``."""
    for spec in SPECS:
        path = directory / spec.name
        if path.is_file() and path.read_bytes() == data:
            return spec
    return None


def describe_connected(protocol: int, version: str, file_bytes: bytes | None,
                       info: dict, directory: Path) -> dict:
    """Identity of the connected session, separate from the computer it runs on.

    Protocol and capabilities come from the live daemon. Family and CPU target
    come only from the file SYSINFO named in ``self=``. That path is where the
    process started; after an in-place update the bytes on disk may already be
    the replacement, so ``daemon_identity`` says "named file" and never claims
    a proven running image. The machine's OS and CPU are never used for the
    family. What the daemon did not tell is "" (a page shows its own localised
    "not reported"); this module only reports and never selects or installs.
    """
    spec = identify(file_bytes, directory) if file_bytes else None
    capabilities = [
        name for key, name in (("audio", "AUDIO"), ("loaded", "loaded"), ("probe", "probe"),
                               ("poke", "POKE"), ("trace", "TRACE"))
        if (info.get(key) in ("49", "1") if key == "audio" else info.get(key) == "1")
    ]
    return {
        "daemon_protocol": protocol if protocol else None,
        "daemon_version": (version or "").strip(),
        "daemon_family": spec.label if spec else "",
        "daemon_target": spec.target if spec else "",
        "daemon_identity": "named file" if file_bytes is not None else "",
        "daemon_capabilities": capabilities,
    }


def _ver_text(data: bytes) -> bytes | None:
    mark = b"$VER: amigentd "
    at = data.find(mark)
    if at < 0:
        return None
    return data[at + len(mark):at + len(mark) + 40].split(b"\x00", 1)[0]


def embedded_version(data: bytes) -> tuple[int, ...] | None:
    """The ``$VER: amigentd`` number in a binary, when it has one."""
    raw = _ver_text(data)
    if raw is None:
        return None
    raw = raw.split(b" ", 1)[0]
    parts: list[int] = []
    for piece in raw.decode("ascii", "replace").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) if parts else None


def embedded_date(data: bytes) -> tuple[int, int, int] | None:
    """The date in ``$VER: amigentd 1.4.0 (18.11.2026)`` as (year, month, day)."""
    raw = _ver_text(data)
    if raw is None or b"(" not in raw:
        return None
    inside = raw.split(b"(", 1)[1].split(b")", 1)[0].decode("ascii", "replace")
    parts = inside.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    day, month, year = (int(p) for p in parts)
    return year, month, day


def consider_replacement(installed: bytes | None, candidate: bytes, candidate_name: str,
                         directory: Path) -> tuple[bool, str]:
    """Whether an update may copy ``candidate`` over the installed file.

    Fresh installation does not call this. True means replace. False keeps the
    file and the string says why. An experimental build never ranks above a
    supported candidate solely by its CPU target.
    """
    if installed is None:
        return False, ("there is no C:amigentd on the Amiga to compare with (amigentd runs from "
                       "elsewhere), so nothing was replaced; --force puts this one there.")
    if installed == candidate:
        return False, "already the same executable"
    installed_spec = identify(installed, directory)
    candidate_spec = next((spec for spec in SPECS if spec.name == candidate_name), None)
    if candidate_spec is None or not candidate_spec.verified:
        return False, "the selected daemon is not a supported build, so nothing was replaced."
    kept = specialisation_guard(installed, candidate_name, directory)
    if kept:
        return False, kept
    installed_version = embedded_version(installed)
    candidate_version = embedded_version(candidate)
    if installed_version and candidate_version and installed_version > candidate_version:
        return False, ("the installed amigentd reports a newer version, so it was left in place; "
                       "--force replaces it anyway.")
    if installed_version and candidate_version and installed_version == candidate_version:
        # the same number: the date of the build decides, so a rebuilt one gets in
        installed_date, candidate_date = embedded_date(installed), embedded_date(candidate)
        if installed_date and candidate_date and installed_date != candidate_date:
            if installed_date > candidate_date:
                return False, ("the installed amigentd reports the same version from a later date, "
                               "so it was left in place; --force replaces it anyway.")
            return True, ""
    if installed_spec is None and installed_version and candidate_version and installed_version == candidate_version:
        return False, ("the installed amigentd is not one of the builds in this package and "
                       "reports the same version, so it was left in place; --force replaces it anyway.")
    return True, ""
