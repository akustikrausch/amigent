"""Apollo SETUP probe policy: ApolloOS alone is not enough.

The 68000 probe exits 0 only when ApolloOS and AFF_FPGA are both present,
so SETUP does not install an Apollo daemon on ApolloOS-named builds without
the 68080 core bit. Under the upstream source-only binary policy no prebuilt
probe binary is packaged in src/amigent/amiga/bin/.
"""

from pathlib import Path

from amigent import setup
from amigent.paths import AMIGA

ROOT = Path(__file__).resolve().parent.parent
PROBE = (ROOT / "src/native/amiga/apollo/apollo_probe.c").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "src/native/amiga/Makefile.apollo").read_text(encoding="utf-8")


def test_probe_source_requires_apolloos_and_aff_fpga_for_success():
    assert "AFF_FPGA" in PROBE
    assert "apollo_os_here" in PROBE
    assert 'PutStr("ApolloOS\\n")' in PROBE
    assert 'PutStr("68080\\n")' in PROBE
    assert 'PutStr("AmigaOS\\n")' in PROBE
    # Success path: both ApolloOS and the FPGA attention bit.
    assert "if (SysBase->AttnFlags & AFF_FPGA)" in PROBE
    assert "return RETURN_OK" in PROBE
    assert "return RETURN_WARN" in PROBE
    # Probe itself stays 68000-clean (no 68080 codegen).
    assert "-m68000" in MAKEFILE.split("PROBE_CFLAGS :=", 1)[1].splitlines()[0]
    assert "m68080" not in PROBE


def test_setup_docstring_matches_strict_policy_and_absent_packaged_probe():
    doc = setup._apollo_by_probe.__doc__ or ""
    assert "FPGA" in doc and "ApolloOS" in doc
    assert not (AMIGA / "bin" / "amigent-apollo-probe").exists()


def test_setup_selects_apollo_only_when_probe_exit_is_zero(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    (binary / "amigentd").write_bytes(b"classic")
    (binary / "amigentd.apollo").write_bytes(b"apollo")
    (binary / "amigentd.000").write_bytes(b"000")
    (binary / "amigent-apollo-probe").write_bytes(b"probe")
    monkeypatch.setattr(setup, "AMIGA", package)

    class Link:
        def __init__(self, probe):
            self.probe = probe
            self.puts = []

        def run(self, command):
            if command.startswith("CPU"):
                return 0
            if "amigent-apollo-probe" in command and not command.startswith("Protect"):
                return self.probe
            return 0

        def put(self, name, data):
            self.puts.append(name)

    assert setup.amigentd_build(Link(0)) == "amigentd.apollo"
    assert setup.amigentd_build(Link(5)) == "amigentd"
    assert setup.monitor_file("amigentd.apollo") == "amigent-monitor"
    (binary / "amigent-monitor.apollo").write_bytes(b"monitor-apollo")
    assert setup.monitor_file("amigentd.apollo") == "amigent-monitor.apollo"
    assert setup.monitor_file("amigentd") == "amigent-monitor"
