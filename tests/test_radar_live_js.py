"""Runs the DOM-free client logic's `node --test` suite. Skipped where node isn't installed —
node is a dev convenience here, not a project dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_JS_TEST = Path(__file__).parent / "js" / "radar_live_core.test.js"


def test_radar_live_core_js_unit_tests_pass():
    result = subprocess.run(
        ["node", "--test", str(_JS_TEST)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
