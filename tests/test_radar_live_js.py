"""Runs the DOM-free client logic's `node --test` suites. Skipped where node isn't installed —
node is a dev convenience here, not a project dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_JS_DIR = Path(__file__).parent / "js"


@pytest.mark.parametrize("test_file", sorted(_JS_DIR.glob("*.test.js")), ids=lambda p: p.name)
def test_js_unit_tests_pass(test_file):
    result = subprocess.run(
        ["node", "--test", str(test_file)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
