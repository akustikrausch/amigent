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

    @property
    def verified(self) -> bool:
        return self.status == SUPPORTED


# vbcc -cpu=68000, vbcc -cpu=68020, and the historical vbcc -cpu=68080 build
# (retired from packages). See docs/PROTOCOL.md.
SPECS: tuple[Spec, ...] = (
    Spec("amigentd.000", CPU_68000, 0, SUPPORTED, "68000", "68000", "68000"),
    Spec("amigentd", CPU_68020, 1, SUPPORTED, "general", "68020", "general"),
    Spec("amigentd.080", CPU_68080, 0, EXPERIMENTAL, "68080", "68080", "68080 experimental"),
)


@dataclass(frozen=True)
class Machine:
    cpu: int | None


def fits(spec: Spec, machine: Machine) -> bool:
    """True when this build's instructions can run on the machine."""
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
    """CPU CHECK results: 0 means this CPU or better, 5 means less, anything else stops.

    ``run`` returns the exit code. An unrecognised first answer is an unknown CPU.
    """
    code = run("CPU CHECK 68020")
    if code == 5:
        return CPU_68010 if run("CPU CHECK 68010") == 0 else CPU_68000
    if code != 0:
        return None
    found = CPU_68020
    for cpu in (CPU_68030, CPU_68040, CPU_68060):
        got = run(f"CPU CHECK {cpu}")
        if got != 0:
            break
        found = cpu
    return found


def identify(data: bytes, directory: Path) -> Spec | None:
    """The catalogue entry whose packaged bytes are exactly ``data``."""
    for spec in SPECS:
        path = directory / spec.name
        if path.is_file() and path.read_bytes() == data:
            return spec
    return None


def embedded_version(data: bytes) -> tuple[int, ...] | None:
    """The ``$VER: amigentd`` number in a binary, when it has one."""
    mark = b"$VER: amigentd "
    at = data.find(mark)
    if at < 0:
        return None
    raw = data[at + len(mark):at + len(mark) + 24].split(b" ", 1)[0].split(b"\x00", 1)[0]
    parts: list[int] = []
    for piece in raw.decode("ascii", "replace").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) if parts else None


def consider_replacement(installed: bytes | None, candidate: bytes, candidate_name: str,
                         directory: Path) -> tuple[bool, str]:
    """Whether an update may copy ``candidate`` over the installed file.

    Fresh installation does not call this. True means replace. False keeps the
    file and the string says why. An experimental build never ranks above a
    supported candidate solely by its CPU target.
    """
    if installed is None:
        return False, "C:amigentd could not be read, so the installed daemon was left in place."
    if installed == candidate:
        return False, "already the same executable"
    installed_spec = identify(installed, directory)
    candidate_spec = next((spec for spec in SPECS if spec.name == candidate_name), None)
    if candidate_spec is None or not candidate_spec.verified:
        return False, "the selected daemon is not a supported build, so nothing was replaced."
    if (installed_spec and installed_spec.verified
            and installed_spec.specialisation > candidate_spec.specialisation):
        return False, (f"the installed {installed_spec.label} daemon was left in place. "
                       f"{candidate_spec.label} would replace it with a less specialised build.")
    installed_version = embedded_version(installed)
    candidate_version = embedded_version(candidate)
    if installed_version and candidate_version and installed_version > candidate_version:
        return False, "the installed amigentd reports a newer version, so it was left in place."
    if installed_spec is None and installed_version and candidate_version and installed_version == candidate_version:
        return False, ("the installed amigentd is not one of the builds in this package and "
                       "reports the same version, so it was left in place.")
    return True, ""
