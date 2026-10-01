from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from job_hunter.adapters.base import AdapterError, ListingTimeout
from job_hunter.adapters.stealth_html import StealthHtmlAdapter
from job_hunter.collector import Collector
from job_hunter.config import CandidateProfile, CollectionConfig, CompanyConfig, Settings
from job_hunter.storage import Storage


class PlaywrightTimeoutError(Exception):
    """Same class name shape Scrapling/Playwright raise for a navigation/selector timeout."""


class _FakeSession:
    """Stands in for Scrapling's session: `pages` maps the pagination parameter's value to
    the HTML served, `fail_on` maps it to an exception to raise instead."""

    def __init__(self, pages, *, fail_on=None, param="page", default=1):
        self.pages = pages
        self.fail_on = fail_on or {}
        self.param = param
        self.default = default
        self.requested: list[int] = []

    async def fetch(self, url, **kwargs):
        value = int((parse_qs(urlsplit(url).query).get(self.param) or [self.default])[0])
        self.requested.append(value)
        if value in self.fail_on:
            raise self.fail_on[value]
        return SimpleNamespace(html_content=self.pages[value])


def _list_card(job_id: str) -> str:
    return (
        f'<div class="job" data-job-id="{job_id}"><a class="t" href="/j/{job_id}">Role {job_id}</a>'
        f'<span class="loc">Detroit, MI</span></div>'
    )


def _detail_card(job_id: str) -> str:
    return (
        f'<div class="job"><a class="t" href="/j/{job_id}">Role {job_id}</a>'
        f'<span class="loc">Detroit, MI</span><span class="dep">Eng</span>'
        f'<span class="emp">Full</span><p class="desc">Text {job_id}</p></div>'
    )


def _page(cards: list[str]) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


def _config(skip_detail_fetch, **config):
    cfg = {
        "list_url": "https://careers.example/jobs",
        "card_selector": ".job",
        "title_selector": ".t",
        "link_selector": ".t",
        "location_selector": ".loc",
        "page_size": 2,
    }
    if "page_parameter" not in config and "next_selector" not in config:
        cfg["page_number_parameter"] = "page"
    if skip_detail_fetch:
        cfg.update(
            skip_detail_fetch=True,
            department_selector=".dep",
            employment_type_selector=".emp",
            description_selector=".desc",
        )
    cfg.update(config)
    return cfg


def _adapter(monkeypatch, session, *, skip_detail_fetch, **config):
    cfg = _config(skip_detail_fetch, **config)
    company = CompanyConfig(key="acme", company="Acme", adapter="stealth_html", config=cfg)
    adapter = StealthHtmlAdapter(company, None, CollectionConfig(max_retries=0))

    async def _fake_ensure_session():
        return session

    monkeypatch.setattr(adapter, "_ensure_session", _fake_ensure_session)
    return adapter


# Both listing loops (the card/detail-page one and the no-details one) must behave the same.
both_loops = pytest.mark.parametrize("skip_detail_fetch", [False, True], ids=["details", "no-details"])


def _cards(skip_detail_fetch: bool, *ids: str) -> list[str]:
    make = _detail_card if skip_detail_fetch else _list_card
    return [make(job_id) for job_id in ids]


def _titles(jobs) -> list[str]:
    return [job.title for job in jobs]


@pytest.mark.asyncio
async def test_request_raises_listing_timeout_for_timeout_class_failures(monkeypatch):
    session = _FakeSession({}, fail_on={1: TimeoutError("WAF challenge not cleared")})
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=False)
    with pytest.raises(ListingTimeout, match="WAF challenge not cleared") as info:
        await adapter.request("GET", "https://careers.example/jobs")
    assert "https://careers.example/jobs" in str(info.value)


@pytest.mark.asyncio
async def test_request_treats_a_playwright_style_timeout_as_a_timeout(monkeypatch):
    session = _FakeSession({}, fail_on={1: PlaywrightTimeoutError("selector never appeared")})
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=False)
    with pytest.raises(ListingTimeout, match="selector never appeared"):
        await adapter.request("GET", "https://careers.example/jobs")


@pytest.mark.asyncio
async def test_request_raises_adapter_error_naming_the_cause_for_other_failures(monkeypatch):
    session = _FakeSession({}, fail_on={1: RuntimeError("browser crashed")})
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=False)
    with pytest.raises(AdapterError, match=r"RuntimeError.*browser crashed") as info:
        await adapter.request("GET", "https://careers.example/jobs")
    assert not isinstance(info.value, ListingTimeout)


@both_loops
@pytest.mark.asyncio
async def test_a_failed_later_page_raises_and_keeps_the_earlier_pages(monkeypatch, skip_detail_fetch):
    session = _FakeSession(
        {1: _page(_cards(skip_detail_fetch, "A", "B"))}, fail_on={2: RuntimeError("blocked")}
    )
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=skip_detail_fetch)
    with pytest.raises(AdapterError, match="blocked"):
        await adapter.fetch_summaries()
    assert _titles(adapter.kept) == ["Role A", "Role B"]


@both_loops
@pytest.mark.asyncio
async def test_an_empty_page_after_a_full_page_is_not_treated_as_the_end(monkeypatch, skip_detail_fetch):
    session = _FakeSession(
        {1: _page(_cards(skip_detail_fetch, "A", "B")), 2: "<html><body>challenge</body></html>"}
    )
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=skip_detail_fetch)
    with pytest.raises(AdapterError, match="no job cards"):
        await adapter.fetch_summaries()
    assert _titles(adapter.kept) == ["Role A", "Role B"]


@both_loops
@pytest.mark.asyncio
async def test_a_failed_first_page_raises_with_the_real_cause_and_keeps_nothing(monkeypatch, skip_detail_fetch):
    session = _FakeSession({}, fail_on={1: RuntimeError("boom")})
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=skip_detail_fetch)
    with pytest.raises(AdapterError, match="boom"):
        await adapter.fetch_summaries()
    assert adapter.kept == []


@both_loops
@pytest.mark.asyncio
async def test_a_short_final_page_ends_the_listing_cleanly(monkeypatch, skip_detail_fetch):
    session = _FakeSession(
        {
            1: _page(_cards(skip_detail_fetch, "A", "B")),
            2: _page(_cards(skip_detail_fetch, "C", "D")),
            3: _page(_cards(skip_detail_fetch, "E")),
        }
    )
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=skip_detail_fetch)
    jobs = await adapter.fetch_summaries()
    assert _titles(jobs) == ["Role A", "Role B", "Role C", "Role D", "Role E"]
    assert session.requested == [1, 2, 3]


@both_loops
@pytest.mark.asyncio
async def test_hitting_max_pages_with_more_available_raises_instead_of_truncating(monkeypatch, skip_detail_fetch):
    session = _FakeSession(
        {
            1: _page(_cards(skip_detail_fetch, "A", "B")),
            2: _page(_cards(skip_detail_fetch, "C", "D")),
            3: _page(_cards(skip_detail_fetch, "E", "F")),
        }
    )
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=skip_detail_fetch, max_pages=2)
    with pytest.raises(AdapterError, match="max_pages"):
        await adapter.fetch_summaries()
    assert _titles(adapter.kept) == ["Role A", "Role B", "Role C", "Role D"]
    assert session.requested == [1, 2]


@both_loops
@pytest.mark.asyncio
async def test_a_next_link_back_to_an_already_fetched_page_raises_instead_of_ending_quietly(
    monkeypatch, skip_detail_fetch
):
    looping = _page(_cards(skip_detail_fetch, "A", "B")) + '<a class="next" href="/jobs">next</a>'
    session = _FakeSession({1: looping})
    adapter = _adapter(
        monkeypatch, session, skip_detail_fetch=skip_detail_fetch, next_selector="a.next"
    )
    with pytest.raises(AdapterError, match="pagination loop"):
        await adapter.fetch_summaries()
    assert _titles(adapter.kept) == ["Role A", "Role B"]
    assert session.requested == [1]


@pytest.mark.asyncio
async def test_no_details_loop_follows_a_row_offset_page_parameter(monkeypatch):
    session = _FakeSession(
        {
            0: _page(_cards(True, "A", "B")),
            2: _page(_cards(True, "C", "D")),
            4: _page(_cards(True, "E")),
        },
        param="start",
        default=0,
    )
    adapter = _adapter(monkeypatch, session, skip_detail_fetch=True, page_parameter="start")
    jobs = await adapter.fetch_summaries()
    assert _titles(jobs) == ["Role A", "Role B", "Role C", "Role D", "Role E"]
    assert session.requested == [0, 2, 4]


async def _two_runs(monkeypatch, tmp_path, skip_detail_fetch, failure):
    """Run 1 sees five jobs; in run 2 page 2 raises `failure`. Returns (results, rows)."""
    full = {
        1: _page(_cards(skip_detail_fetch, "A", "B")),
        2: _page(_cards(skip_detail_fetch, "C", "D")),
        3: _page(_cards(skip_detail_fetch, "E")),
    }
    sessions = iter([_FakeSession(full), _FakeSession(full, fail_on={2: failure})])
    current = {}

    async def _fake_ensure_session(self):
        return current["session"]

    monkeypatch.setattr(StealthHtmlAdapter, "_ensure_session", _fake_ensure_session)
    company = CompanyConfig(
        key="acme", company="Acme", adapter="stealth_html", config=_config(skip_detail_fetch)
    )
    settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())

    results = []
    for _ in range(2):
        current["session"] = next(sessions)
        results.append(await Collector(settings, [company], CandidateProfile()).search())
    with Storage(settings.database_path) as storage:
        rows = storage.connection.execute(
            "SELECT status, missing_count FROM jobs WHERE source_key='acme'"
        ).fetchall()
    return results, rows


@both_loops
@pytest.mark.asyncio
async def test_a_blocked_page_never_closes_jobs_the_run_did_not_reach(
    monkeypatch, tmp_path, skip_detail_fetch
):
    """The point of the change, end to end: run 1 sees five jobs; in run 2 the browser is
    blocked on page 2. The source must be a `warning` and no job may age toward closure."""
    results, rows = await _two_runs(
        monkeypatch, tmp_path, skip_detail_fetch, RuntimeError("blocked by WAF")
    )
    assert results[0].source_health[0].status.value == "ok"
    second = results[1].source_health[0]
    assert second.status.value == "warning"
    assert "blocked by WAF" in second.message
    assert len(rows) == 5
    assert {(row["status"], row["missing_count"]) for row in rows} == {("active", 0)}


@pytest.mark.asyncio
async def test_a_page_timeout_is_reported_as_a_timeout_with_its_url_and_cause(monkeypatch, tmp_path):
    results, rows = await _two_runs(
        monkeypatch, tmp_path, True, TimeoutError("WAF challenge not cleared")
    )
    second = results[1].source_health[0]
    assert second.status.value == "warning"
    assert second.failure_kind == "timeout"
    assert "WAF challenge not cleared" in second.message
    assert "page=2" in second.message
    assert {row["missing_count"] for row in rows} == {0}
