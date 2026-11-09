"""What kind of Amiga this is: the OS from Version, the CPU from CPU, and PowerPC software."""

from __future__ import annotations

import re

OS_NAMES = {47: "AmigaOS 3.2", 46: "AmigaOS 3.1.4", 45: "AmigaOS 3.9", 44: "AmigaOS 3.5",
            40: "AmigaOS 3.1", 39: "AmigaOS 3.0", 37: "AmigaOS 2.04", 36: "AmigaOS 2.0", 34: "AmigaOS 1.3"}

# PowerPC boards on a 68k Amiga bring one of these; Version FILE reads the version string
# without opening the library, which without a board could stop at a requester.
PPC_LIBRARIES = (("LIBS:powerpc.library", "WarpOS"), ("LIBS:ppc.library", "PowerUP"))

EMULATED_NOTE = ("amigentd is a 68k program and runs here under the system's 68k emulation. "
                 "Commands, files, mouse and keys should work; screen text and other parts that "
                 "hook into the system may not. Not tested on this system.")


def describe_system(version: str, cpu: str) -> dict:
    """Turn the output of the Version and CPU commands into a few named facts."""
    kick = re.search(r"Kickstart (\d+)\.(\d+)", version)
    bench = re.search(r"Workbench (\d+\.\d+)", version)
    major = int(kick.group(1)) if kick else 0
    morphos = re.search(r"MorphOS(?: version)? ?(\d+\.\d+)?", version)
    os4 = re.search(r"AmigaOS 4(?:\.\d+)*", version)
    if morphos:
        os_name = "MorphOS" + (f" {morphos.group(1)}" if morphos.group(1) else "")
    elif os4:
        os_name = os4.group(0)
    elif major >= 50:  # 50 and later are AmigaOS 4 (MorphOS says its name)
        os_name = "AmigaOS 4"
    else:
        os_name = OS_NAMES.get(major, f"Kickstart {major}" if major else "")
    chips = re.findall(r"\b(680[0-9]0|6888[12])\b", cpu)
    ppc = re.search(r"\b(?:PowerPC|PPC)[ \w./-]*", cpu)
    return {
        "os": os_name,
        "kickstart": f"{kick.group(1)}.{kick.group(2)}" if kick else "",
        "workbench": bench.group(1) if bench else "",
        "cpu": ppc.group(0).strip() if ppc else next((c for c in chips if not c.startswith("688")), ""),
        "fpu": next((c for c in chips if c.startswith("688")), ""),
    }


def native_ppc(facts: dict) -> bool:
    """AmigaOS 4 and MorphOS run on PowerPC; amigentd is emulated there."""
    return facts.get("os", "").startswith(("AmigaOS 4", "MorphOS"))


def ppc_software(run) -> str:
    """'WarpOS powerpc.library 17.10' or the like on a 68k Amiga with a PowerPC board, else ''.

    run(command) gives (rc, output). Only reads files; nothing is opened or started.
    """
    found = []
    for path, name in PPC_LIBRARIES:
        rc, out = run(f"Version FILE {path}")
        line = out.strip().splitlines()[-1] if out.strip() else ""
        if rc == 0 and line:
            found.append(f"{name} {line}")
    return ", ".join(found)
