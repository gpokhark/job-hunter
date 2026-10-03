import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import render_applications  # noqa: E402
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


def _open(browser, path, color_scheme="light", init_script=None, reduced_motion=None, viewport=None):
    options = {"color_scheme": color_scheme}
    if reduced_motion:
        options["reduced_motion"] = reduced_motion
    if viewport:
        options["viewport"] = viewport
    context = browser.new_context(**options)
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


FIRE = ("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
        "{detail:{status:'offer'}}))")


def _win_on(page):
    return "on" in (page.get_attribute("#mf-win", "class") or "")


def test_an_offer_event_shows_the_banner_then_the_moment(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        page.evaluate(FIRE)
        page.wait_for_selector("#mf-toast.in")
        assert not _win_on(page), "the banner comes first"
        page.wait_for_selector("#mf-win.on", timeout=6000)
        page.wait_for_selector("#mf-toast", state="detached", timeout=3000)
        page.click("#mf-win")
        page.evaluate("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
                      "{detail:{status:'applied'}}))")
        page.wait_for_timeout(300)
        assert page.locator("#mf-toast").count() == 0 and not _win_on(page)
    finally:
        context.close()


def test_offer_mode_off_shows_neither_banner_nor_moment(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        page.click("#mf-toggle")
        page.evaluate(FIRE)
        page.wait_for_timeout(2600)
        assert page.locator("#mf-toast").count() == 0 and not _win_on(page)
    finally:
        context.close()


def test_accept_offer_celebrates_at_once_without_a_banner(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest-dawn"))
    try:
        page.click(".mf-offer")
        page.click(".mm-accept")
        assert _win_on(page)
        assert page.locator("#mf-toast").count() == 0
    finally:
        context.close()


def test_the_mail_card_expands_and_collapses_by_click_and_keyboard(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        card = page.locator(".mf-offer")
        assert card.get_attribute("aria-expanded") == "false"
        assert not page.is_visible(".mm-body")
        assert "Hiring Team" in card.inner_text() and "Your future employer" in card.inner_text()
        card.click()
        assert card.get_attribute("aria-expanded") == "true" and page.is_visible(".mm-body")
        assert "Dear Jane," in page.inner_text(".mm-body")
        card.focus()
        page.keyboard.press("Enter")
        assert card.get_attribute("aria-expanded") == "false"
    finally:
        context.close()


def test_expanded_mail_card_clears_the_stats_block(browser, tmp_path):
    context, page, _ = _open(
        browser, _page_file(tmp_path, "forest-dawn"), reduced_motion="reduce",
        viewport={"width": 1440, "height": 900},
    )
    try:
        page.click(".mf-offer")
        card = page.evaluate("(() => { const r = document.querySelector('.mf-offer').getBoundingClientRect();"
                             " return {bottom: r.bottom, right: r.right}; })()")
        stats = page.evaluate("(() => { const g = document.querySelector('.stats-group').getBoundingClientRect(),"
                              " s = document.querySelector('.stats').getBoundingClientRect();"
                              " return {top: g.top, right: s.right}; })()")
        gap = stats["top"] - card["bottom"]
        assert 31 <= gap <= 34, f"expected the page's 32px rhythm below the card, got {gap}"
        assert abs(card["right"] - stats["right"]) < 1, "card must stay right-aligned with the stats box"
        page.click(".mf-offer")
        assert page.evaluate("document.querySelector('.mf-mail-gap').offsetHeight") == 0
    finally:
        context.close()


def test_a_narrow_layout_reserves_no_gap(browser, tmp_path):
    context, page, _ = _open(
        browser, _page_file(tmp_path, "forest"), reduced_motion="reduce",
        viewport={"width": 430, "height": 900},
    )
    try:
        page.click(".mf-offer")
        assert page.evaluate("document.querySelector('.mf-mail-gap').offsetHeight") == 0
        assert page.evaluate("getComputedStyle(document.querySelector('.mf-offer')).position") == "static"
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
        page.click(".mf-offer")
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


def test_the_masthead_card_steps_aside_while_the_banner_shows(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest-dawn"))
    try:
        visibility = "getComputedStyle(document.querySelector('.mf-offer')).visibility"
        page.evaluate(FIRE)
        page.wait_for_selector("#mf-toast.in")
        assert page.evaluate(visibility) == "hidden", "two identical emails would be on screen"
        page.wait_for_selector("#mf-toast", state="detached", timeout=6000)
        assert page.evaluate(visibility) == "visible"
    finally:
        context.close()


def test_floating_buttons_do_not_collide_on_a_phone(browser, tmp_path):
    context, page, _ = _open(
        browser, _page_file(tmp_path, "forest"), reduced_motion="reduce",
        viewport={"width": 430, "height": 900},
    )
    try:
        boxes = page.evaluate(
            "['#mf-toggle', '#mf-preview'].map(s => { const r = document.querySelector(s)"
            ".getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; })"
        )
        (al, at, ar, ab), (bl, bt, br, bb) = boxes
        assert ar <= bl or br <= al or ab <= bt or bb <= at, boxes
    finally:
        context.close()


def _app_row(job_id, status, title=None, company="Acme"):
    return {
        "source_key": "acme", "job_id": job_id, "status": status,
        "applied_at": None if status == "saved" else "2026-09-20", "notes": None,
        "company": company, "title": title or f"Role {job_id}", "url": f"https://example.com/{job_id}",
        "location": "Detroit, MI", "posted_at": "2026-09-01T00:00:00Z", "score": 82,
        "salary_evidence": None, "created_at": "2026-09-20T10:00:00+00:00",
        "updated_at": "2026-09-21T10:00:00+00:00",
    }


def _apps_file(tmp_path, theme_name, statuses):
    profile = CandidateProfile(contact=ContactInfo(name="Jane Doe", email="jane@real-mail.test"))
    page = render_applications.render_applications_page(
        applications=[_app_row(str(i), s) for i, s in enumerate(statuses, 1)], job_states={},
        versions={"applications": "v1"}, today=date(2026, 9, 26), archive_name="a.json",
        theme=load_theme(theme_name, profile, templates_dir=THEMES_DIR),
    )
    out = tmp_path / f"apps-{theme_name}-{len(statuses)}.html"
    out.write_text(page, encoding="utf-8")
    return out


@pytest.mark.parametrize("theme", ["forest", "forest-dawn"])
def test_applications_page_becomes_the_inbox(browser, tmp_path, theme):
    context, page, errors = _open(browser, _apps_file(tmp_path, theme, ["offer", "applied", "rejected"]))
    try:
        assert page.inner_text("h1") == "Inbox: Offers Incoming"
        assert "Every row is a reply on its way." in page.inner_text("main")
        offer_chip = page.inner_text('#app-counts [data-count-status="offer"]')
        assert offer_chip.startswith("Yes") and offer_chip.strip().endswith("1")
        assert page.inner_text('#app-counts [data-count-status="all"]').startswith("In inbox")
        assert page.inner_text('.app-row[data-status="applied"] .mf-badge') == "Sent, and already loved"
        assert page.inner_text('.app-row[data-status="rejected"] .mf-badge') == "Redirected"
        assert page.locator('.app-row[data-status="offer"] option[value="offer"]').text_content() == "Offer"
        assert errors == []
    finally:
        context.close()


def test_offer_rows_get_a_ribbon_and_a_compare_button(browser, tmp_path):
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest", ["offer", "applied"]))
    try:
        assert page.locator('.app-row[data-status="offer"] .mf-ribbon').count() == 1
        assert page.locator('.app-row[data-status="applied"] .mf-ribbon').count() == 0
        assert page.locator('.app-row[data-status="applied"] .mf-compare').count() == 0
        page.click('.app-row[data-status="offer"] .mf-compare')
        page.wait_for_function("document.getElementById('live-notice').textContent.length > 0")
        notice = page.inner_text("#live-notice")
        assert "salary-compare" in notice
    finally:
        context.close()


def test_a_blocked_clipboard_still_shows_the_compare_prompt(browser, tmp_path):
    blocker = "Object.defineProperty(navigator, 'clipboard', {get(){ return undefined; }});"
    context, page, _ = _open(
        browser, _apps_file(tmp_path, "forest", ["offer"]), init_script=blocker
    )
    try:
        page.click(".mf-compare")
        page.wait_for_function("document.getElementById('live-notice').textContent.length > 0")
        assert "Use the salary-compare skill. I have an offer for Role 1 at Acme." in page.inner_text("#live-notice")
    finally:
        context.close()


def test_a_status_change_updates_badge_ribbon_and_button_live(browser, tmp_path):
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest", ["applied", "applied"]))
    try:
        row = page.locator(".app-row").first
        row.locator(".app-status").select_option("offer")
        page.wait_for_selector('.app-row[data-status="offer"] .mf-ribbon')
        assert row.locator(".mf-badge").inner_text() == "Yes. Obviously."
        assert row.locator(".mf-compare").count() == 1
        row.locator(".app-status").select_option("applied")
        page.wait_for_function("document.querySelectorAll('.mf-ribbon').length === 0")
        assert row.locator(".mf-badge").inner_text() == "Sent, and already loved"
        assert row.locator(".mf-compare").count() == 0
    finally:
        context.close()


def test_offer_mode_off_restores_the_plain_applications_page(browser, tmp_path):
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest", ["offer", "applied"]))
    try:
        page.click("#mf-toggle")
        assert page.inner_text("h1") == "Applications"
        chip = page.inner_text('#app-counts [data-count-status="offer"]')
        assert chip.startswith("Offer") and chip.strip().endswith("1"), chip
        assert page.inner_text('#app-counts [data-count-status="all"]').startswith("Total")
        assert not page.is_visible(".mf-badge") and not page.is_visible(".mf-ribbon")
        assert not page.is_visible(".mf-compare") and not page.is_visible(".mf-tag")
        page.click("#mf-toggle")
        assert page.inner_text("h1") == "Inbox: Offers Incoming"
        assert page.inner_text('#app-counts [data-count-status="offer"]').startswith("Yes")
    finally:
        context.close()


def test_the_empty_inbox_is_witty_and_restorable(browser, tmp_path):
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest-dawn", []))
    try:
        assert "the first yes needs somewhere to land" in page.inner_text("#app-empty")
        assert "press Track on a job" in page.inner_text("#app-empty")
        page.click("#mf-toggle")
        text = page.inner_text("#app-empty")
        assert text.startswith("No tracked applications yet")
        assert page.locator("#app-empty b").inner_text() == "Track"
    finally:
        context.close()


def _rise(page):
    return float(page.evaluate("document.documentElement.style.getPropertyValue('--mf-rise')"))


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [([], 0.1), (["rejected", "withdrawn"], 0.1), (["saved"], 0.25), (["saved", "applied"], 0.5),
     (["applied", "interviewing"], 0.75), (["interviewing", "offer", "rejected"], 1.0)],
)
def test_the_sky_rises_with_the_furthest_stage(browser, tmp_path, statuses, expected):
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest-dawn", statuses))
    try:
        assert _rise(page) == expected
    finally:
        context.close()


def test_the_sun_is_higher_after_an_offer_and_the_sky_follows_live_changes(browser, tmp_path):
    context, page, _ = _open(
        browser, _apps_file(tmp_path, "forest-dawn", ["applied", "applied"]), reduced_motion="reduce",
    )
    try:
        sun_bottom = "parseFloat(getComputedStyle(document.querySelector('.mf-sun')).bottom)"
        before = page.evaluate(sun_bottom)
        assert _rise(page) == 0.5
        page.locator(".app-row").first.locator(".app-status").select_option("offer")
        page.wait_for_function("document.documentElement.style.getPropertyValue('--mf-rise') === '1'")
        assert page.evaluate(sun_bottom) > before
    finally:
        context.close()


def test_the_night_theme_has_no_progress_sky(browser, tmp_path):
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest", ["offer"]))
    try:
        assert page.evaluate("document.querySelector('.mf-dusk')") is None
        assert page.evaluate("document.documentElement.classList.contains('mf-dawn')") is False
    finally:
        context.close()


def test_the_glow_follows_progress_after_the_intro_animation(browser, tmp_path):
    glow = "parseFloat(getComputedStyle(document.querySelector('.mf-skyglow')).opacity)"
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest-dawn", []))
    try:
        page.wait_for_timeout(4000)
        assert page.evaluate(glow) < 0.5
    finally:
        context.close()
    context, page, _ = _open(browser, _apps_file(tmp_path, "forest-dawn", ["offer"]))
    try:
        page.wait_for_timeout(4000)
        assert page.evaluate(glow) > 0.9
    finally:
        context.close()
