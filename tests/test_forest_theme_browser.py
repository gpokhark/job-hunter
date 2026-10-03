import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import render_radar  # noqa: E402

from job_hunter.config import CandidateProfile, ContactInfo, VisionConfig  # noqa: E402
from job_hunter.theme import load_theme  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")

THEMES_DIR = render_radar._TEMPLATE_DIR / "themes"
FOREST_PAPER = "#08110d"


def _page_file(tmp_path, theme_name, name="Jane Doe", **vision):
    search = tmp_path / "search.json"
    search.write_text(json.dumps({
        "summary": {"sources_succeeded": 1, "sources_attempted": 1},
        "candidates": [{
            "source_key": "x", "job_id": "1", "posted_at": "2026-08-25T00:00:00Z",
            "first_seen_at": None, "location_raw": "Detroit, MI", "visa_sponsorship": "unmentioned",
            "sponsorship_evidence": None, "salary_evidence": None, "work_arrangement": "unknown",
            "company": "Acme", "title": "Scored Role", "url": "https://example.com/1",
        }],
        "source_health": [],
    }))
    assess = tmp_path / "assessments.json"
    assess.write_text(json.dumps([{
        "source_key": "x", "job_id": "1", "score": 88, "company": "Acme", "title": "Scored Role",
        "url": "https://example.com/1", "matches": ["m"], "gaps": ["g"],
    }]))
    profile = CandidateProfile(
        contact=ContactInfo(name=name, email="jane@real-mail.test"), vision=VisionConfig(**vision)
    )
    out = tmp_path / f"{theme_name}.html"
    render_radar.build(
        output_path=out, search_path=search, assessments_path=assess, title="Test Radar",
        keyword_label=None, new_days=10, now=datetime(2026, 8, 31, tzinfo=UTC),
        theme=load_theme(theme_name, profile, templates_dir=THEMES_DIR),
    )
    return out


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # chromium not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield instance
        instance.close()


def _open(browser, path, color_scheme="light", init_script=None):
    context = browser.new_context(color_scheme=color_scheme)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" and "Failed to load resource" not in m.text else None)
    page.route("**/fonts.googleapis.com/**", lambda route: route.abort())
    page.route("**/fonts.gstatic.com/**", lambda route: route.abort())
    if init_script:
        page.add_init_script(init_script)
    page.goto(path.as_uri(), wait_until="domcontentloaded")
    page.wait_for_selector("#mf-bg", state="attached")
    return context, page, errors


@pytest.mark.parametrize("theme", ["forest", "forest-dawn"])
def test_titles_swap_restore_and_celebrate(browser, tmp_path, theme):
    context, page, errors = _open(browser, _page_file(tmp_path, theme))
    try:
        assert page.inner_text("h1") == "Offer Season"
        assert "Dear Jane," in page.inner_text(".mf-offer")
        assert "The Yes List" in page.inner_text("summary.group-head >> nth=1")
        assert page.get_attribute("html", "data-theme") == theme
        page.evaluate("window.JobHunterTheme.celebrate()")
        assert "on" in page.get_attribute("#mf-win", "class")
        assert "jane" in page.inner_text("#mf-win h2").lower()  # CSS upper-cases the headline
        page.click("#mf-toggle")
        assert page.inner_text("h1") == "Test Radar"
        assert "Strong matches" in page.inner_text("summary.group-head >> nth=1")
        assert not page.is_visible(".mf-line")
        assert errors == []
    finally:
        context.close()


def test_celebration_rules_follow_offer_mode(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        fire = ("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
                "{detail:{status:'offer'}}))")
        page.evaluate(fire)
        assert "on" in page.get_attribute("#mf-win", "class")
        page.click("#mf-win")
        page.click("#mf-toggle")
        page.evaluate(fire)
        assert "on" not in (page.get_attribute("#mf-win", "class") or "")
        page.evaluate("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
                      "{detail:{status:'applied'}}))")
        assert "on" not in (page.get_attribute("#mf-win", "class") or "")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_forest_palette_wins_over_the_os_color_scheme(browser, tmp_path, scheme):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"), color_scheme=scheme)
    try:
        paper = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--paper').trim().toLowerCase()"
        )
        assert paper == FOREST_PAPER
    finally:
        context.close()


def test_hostile_vision_text_stays_text(browser, tmp_path):
    hostile = "<img src=x onerror=window.__pwned=1>"
    path = _page_file(tmp_path, "forest", name="Jane Doe", role=hostile, pay_note=hostile, start_note=hostile)
    context, page, errors = _open(browser, path)
    try:
        assert page.evaluate("document.querySelectorAll('.mf-offer img').length") == 0
        assert page.evaluate("window.__pwned === undefined")
        assert hostile in page.inner_text(".mf-offer")
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("name", ["Your Name", ""])
def test_no_usable_name_never_prints_junk(browser, tmp_path, name):
    context, page, errors = _open(browser, _page_file(tmp_path, "forest-dawn", name=name))
    try:
        text = page.inner_text("body")
        assert "Dear" not in text
        assert "undefined" not in text and "null" not in text
        page.evaluate("window.JobHunterTheme.celebrate()")
        assert page.inner_text("#mf-win h2").strip().lower() == "you got the offer."
        assert errors == []
    finally:
        context.close()


def test_a_throwing_localstorage_does_not_break_the_page(browser, tmp_path):
    blocker = (
        "Object.defineProperty(window, 'localStorage', {get(){ throw new Error('blocked'); }});"
    )
    context, page, errors = _open(browser, _page_file(tmp_path, "forest"), init_script=blocker)
    try:
        assert page.inner_text("h1") == "Offer Season"
        page.click("#mf-toggle")
        assert page.inner_text("h1") == "Test Radar"
    finally:
        context.close()
