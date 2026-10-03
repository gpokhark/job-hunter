import json
import re
import shutil
import subprocess

import pytest

from job_hunter.config import CandidateProfile, ContactInfo
from job_hunter.theme import DEFAULT_TEMPLATES_DIR, load_theme

FOREST = DEFAULT_TEMPLATES_DIR / "forest.css"
DAWN = DEFAULT_TEMPLATES_DIR / "forest-dawn.css"
JS = DEFAULT_TEMPLATES_DIR / "offer.js"


def test_asset_files_exist_and_are_ascii():
    for path in (FOREST, DAWN, JS):
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").isascii(), f"{path.name} must be ASCII only"


def test_css_outranks_the_page_dark_mode_block_and_defines_the_components():
    css = FOREST.read_text(encoding="utf-8")
    assert ":root:root:root" in css
    assert "[data-theme" not in css
    for selector in ("#mf-bg", "#mf-win", "#mf-toggle", "#mf-preview", ".mf-offer", ".mf-line"):
        assert selector in css
    assert "prefers-reduced-motion" in css
    dawn = DAWN.read_text(encoding="utf-8")
    assert ":root:root:root" in dawn and ".mf-sun" in dawn and ".mf-rays" in dawn


def test_js_is_safe_by_construction():
    js = JS.read_text(encoding="utf-8")
    assert "</script" not in js.lower()
    assert js.count("innerHTML") == 1, "only the numeric SVG tree markup may use innerHTML"
    assert "mf-config" in js and "textContent" in js
    assert "jobhunter:application-status" in js
    assert "job-hunter-offer-mode" in js
    assert "JobHunterTheme" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_parses():
    result = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_the_real_assets_load_for_both_themes():
    profile = CandidateProfile(contact=ContactInfo(name="Jane Doe", email="jane@real-mail.test"))
    forest = load_theme("forest", profile)
    dawn = load_theme("forest-dawn", profile)
    assert FOREST.read_text(encoding="utf-8") in forest.css
    assert dawn.css.startswith(forest.css) and ".mf-sun" in dawn.css
    config = json.loads(re.search(r'id="mf-config">(.*?)</script>', dawn.script_html, re.S).group(1))
    assert config["theme"] == "forest-dawn" and config["firstName"] == "Jane"
