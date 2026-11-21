"""Packaged daemon catalogue for identifying a connected executable by its bytes.

This module is the identity half of the catalogue: ``identify`` matches file
bytes against packaged builds, and ``describe_connected`` reports what the
Viewer About dialog can show. It does not select installs or decide updates;
those remain in setup (and upstream PR #38 when that lands).

Family and CPU target come only from a byte-identical packaged file. Matching
a filename on the Amiga is not enough: after an in-place update the named path
may already hold the replacement while the connected process is still the old
image, so the UI labels a match as a named file rather than a proven running
binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CPU_68000, CPU_68020, CPU_68080 = 68000, 68020, 68080
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


# Catalogue entries used only to recognise packaged bytes. ``amigentd.080`` and
# ``amigentd.apollo`` identify when present in the package; neither is required
# for this Viewer feature, and this module does not install or select them.
SPECS: tuple[Spec, ...] = (
    Spec("amigentd.000", CPU_68000, 0, SUPPORTED, "68000", "68000", "68000"),
    Spec("amigentd", CPU_68020, 1, SUPPORTED, "general", "68020", "general"),
    Spec("amigentd.080", CPU_68080, 0, EXPERIMENTAL, "68080", "68080", "68080 experimental"),
    Spec("amigentd.apollo", CPU_68080, 2, SUPPORTED, "apollo", "68080", "Apollo"),
)


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
    the replacement, so the UI must not treat them as the guaranteed running
    image. The machine's OS and CPU are never used for the family.
    """
    spec = identify(file_bytes, directory) if file_bytes else None
    capabilities = []
    if info.get("audio") == "1":
        capabilities.append("AUDIO")
    if info.get("loaded") == "1":
        capabilities.append("loaded")
    if info.get("probe") == "1":
        capabilities.append("probe")
    if info.get("poke") == "1":
        capabilities.append("POKE")
    if info.get("trace") == "1":
        capabilities.append("TRACE")
    reported = (version or "").strip()
    if file_bytes is None:
        identity = "Not reported"
        family = "Not reported"
        target = "Not reported"
    else:
        identity = "named file"
        family = spec.label if spec else "Not reported"
        target = spec.target if spec else "Not reported"
    return {
        "daemon_protocol": protocol if protocol else None,
        "daemon_version": reported or "Not reported",
        "daemon_family": family,
        "daemon_target": target,
        "daemon_identity": identity,
        "daemon_capabilities": capabilities,
    }
