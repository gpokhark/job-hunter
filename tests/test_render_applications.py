import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from render_applications import (  # noqa: E402
    STATUS_ORDER,
    days_since,
    posting_state,
    render_applications_page,
)

TODAY = date(2026, 9, 26)
VERSIONS = {"applications": "v1"}


def _app(job_id="1", status="applied", applied_at="2026-09-20", notes=None, title="ADAS Engineer",
         updated_at="2026-09-21T10:00:00+00:00", url="https://example.com/1", **over):
    row = {
        "source_key": "acme", "job_id": job_id, "status": status, "applied_at": applied_at,
        "notes": notes, "company": "Acme", "title": title, "url": url, "location": "Detroit, MI",
        "posted_at": "2026-09-01T00:00:00Z", "score": 82, "salary_evidence": None,
        "created_at": "2026-09-20T10:00:00+00:00", "updated_at": updated_at,
    }
    row.update(over)
    return row


def _page(apps, states=None):
    return render_applications_page(
        applications=apps, job_states=states if states is not None else {}, versions=VERSIONS,
        today=TODAY, archive_name="default_2026-09-26.json",
    )


def test_status_order_and_helpers():
    assert STATUS_ORDER == ("offer", "interviewing", "applied", "saved", "rejected", "withdrawn")
    assert days_since("2026-09-20", TODAY) == 6
    assert days_since("2026-09-26", TODAY) == 0
    assert days_since(None, TODAY) is None
    assert posting_state("active") == "open"
    assert posting_state("closed") == "closed"
    assert posting_state(None) == "removed"


def test_rows_sort_by_status_order_then_applied_date_newest_first():
    apps = [
        _app("1", "saved", None, updated_at="2026-09-21T00:00:00+00:00"),
        _app("2", "applied", "2026-09-10"),
        _app("3", "offer", "2026-09-01"),
        _app("4", "applied", "2026-09-20"),
        _app("5", "withdrawn", "2026-09-05"),
    ]
    html = _page(apps)
    order = re.findall(r'class="app-row"[^>]*data-job-id="(\d+)"', html)
    assert order == ["3", "4", "2", "1", "5"]


def test_counts_days_since_and_posting_state():
    apps = [_app("1", "applied", "2026-09-20"), _app("2", "saved", None), _app("3", "rejected", "2026-09-26")]
    html = _page(apps, {"acme|1": "active", "acme|2": "closed"})  # job 3 is gone
    assert 'data-count-status="all">Total <b>3</b>' in html
    assert 'data-count-status="applied">Applied <b>1</b>' in html
    assert 'data-count-status="offer">Offer <b>0</b>' in html
    assert "Applied 6 days ago" in html and "Applied today" in html
    assert 'data-posting="open"' in html and 'data-posting="closed"' in html and 'data-posting="removed"' in html
    assert "Posting removed" in html


def test_empty_state_is_visible_only_without_applications():
    assert '<p id="app-empty" class="app-empty">' in _page([])
    assert '<p id="app-empty" class="app-empty" hidden>' in _page([_app()])


def test_hostile_text_is_escaped_and_unsafe_urls_are_neutralised():
    apps = [_app(notes="</script><img src=x onerror=alert(1)>", title='<script>alert("t")</script>',
                 url="javascript:alert(1)")]
    html = _page(apps)
    assert "<img src=x" not in html and "<script>alert" not in html
    assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in html
    assert 'href="javascript:' not in html
    assert 'class="app-title" href="#"' in html
    good = _page([_app(url="https://example.com/x?a=1&b=2")])
    assert 'href="https://example.com/x?a=1&amp;b=2"' in good


def test_page_embeds_versions_scripts_and_never_descriptions_or_db_paths():
    html = _page([_app()])
    assert 'window.__APPS_LIVE__ = {"versions": {"applications": "v1"}}' in html
    assert (
        html.index("root.RadarLive = api")
        < html.index("root.RadarLiveSync = api")
        < html.index("var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__APPS_LIVE__")
    )
    for needle in ("jobs.sqlite3", "description"):
        assert needle not in html
    assert 'href="/"' in html  # link back to the radar
    assert 'id="live-status"' in html and 'id="live-reload"' in html and 'id="app-rows"' in html


def test_status_select_lists_the_six_statuses_with_the_current_one_selected():
    html = _page([_app(status="offer")])
    select = re.search(r'<select class="app-status">(.*?)</select>', html, re.S).group(1)
    assert re.findall(r'<option value="(\w+)"', select) == ["saved", "applied", "interviewing", "offer", "rejected", "withdrawn"]
    assert '<option value="offer" selected>' in select


def test_saved_row_has_a_disabled_date_and_no_days_text():
    html = _page([_app(status="saved", applied_at=None)])
    assert re.search(r'<input type="date" class="app-date"[^>]*disabled', html)
    assert '<span class="app-days"></span>' in html
