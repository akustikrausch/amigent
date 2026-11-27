"""Viewer page JS must parse; a SyntaxError leaves every browser tab offline."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

VIEWER_JS = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web" / "viewer.js"


def test_viewer_js_parses_with_node():
    node = shutil.which("node")
    if not node:
        text = VIEWER_JS.read_text(encoding="utf-8")
        assert text.count("function connect()") == 1
        assert text.count("const cpuLine") == 1
        assert "showUpdate" in text
        return
    proc = subprocess.run([node, "--check", str(VIEWER_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
