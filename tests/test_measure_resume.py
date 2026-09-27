import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from measure_resume import PAGE_HEIGHT_PX, analyze  # noqa: E402


def test_one_page_target_ok_when_the_last_page_is_well_filled():
    result = analyze(900, 1, 1.0)
    assert result["status"] == "ok"
    assert result["last_page_fill_pct"] == 93.8
    assert result["pages"] == 1 and result["target_pages"] == 1.0


def test_one_page_target_underflow_reports_lines_to_add():
    result = analyze(500, 1, 1.0)
    assert result["status"] == "underflow"
    assert result["last_page_fill_pct"] == 52.1
    assert "Add ~" in result["guidance"]


def test_one_page_target_overflow_when_content_spills_to_a_second_page():
    result = analyze(1200, 2, 1.0)
    assert result["status"] == "overflow"
    assert "OVERFLOW" in result["guidance"] and result["delta_px"] == 1200 - PAGE_HEIGHT_PX


def test_half_page_target_uses_the_35_to_70_percent_band():
    assert analyze(PAGE_HEIGHT_PX + 480, 2, 1.5)["status"] == "ok"          # last page 50%
    assert analyze(PAGE_HEIGHT_PX + 100, 2, 1.5)["status"] == "underflow"   # ~10%
    assert analyze(PAGE_HEIGHT_PX + 800, 2, 1.5)["status"] == "overflow"    # ~83%
    assert analyze(2 * PAGE_HEIGHT_PX + 200, 3, 1.5)["status"] == "overflow"  # a third page


def test_two_page_target_needs_a_nearly_full_second_page():
    assert analyze(PAGE_HEIGHT_PX + 890, 2, 2.0)["status"] == "ok"          # last page ~92.7%
    assert analyze(PAGE_HEIGHT_PX + 600, 2, 2.0)["status"] == "underflow"   # ~62.5%
    assert analyze(700, 1, 2.0)["status"] == "underflow"                    # never reached page 2


def test_legacy_fill_pct_is_raw_content_over_one_page():
    assert analyze(1440, 2, 1.5)["fill_pct"] == 150.0


def test_a_real_render_when_playwright_and_chromium_are_available(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("pypdf")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch().close()
    except Exception:  # noqa: BLE001 - no browser installed on this machine
        pytest.skip("Chromium is not installed (run: uv run playwright install chromium)")
    from measure_resume import measure

    html = tmp_path / "r.html"
    html.write_text("<html><body><p>" + "word " * 50 + "</p></body></html>")
    out = tmp_path / "r.pdf"
    result = measure(html, out, 1.0)
    assert out.exists() and result["pages"] == 1
    assert result["status"] == "underflow"  # a tiny document is far under the 88% band
