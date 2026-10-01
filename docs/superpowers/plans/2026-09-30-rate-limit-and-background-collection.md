# Rate-limit-tolerant and background collection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A rate-limited or timed-out source keeps the jobs it fetched, the pipeline continues, the radar names each affected company and why, and collection can run in the background while filter/review/render run independently.

**Architecture:** Adapters register their listing accumulators with the `JobAdapter` base (`begin_listing()` / `keep()`); the collector salvages `adapter.kept` when `fetch_summaries()` raises or times out and records a `warning` `SourceHealth` carrying `failure_kind`. The radar reads those fields from the archive. A new `background.py` runs `Collector.search()` detached, writing a progress state file; `snapshot` materializes an archive from SQLite plus that state on demand.

**Tech Stack:** Python >= 3.11 (`asyncio.timeout`), httpx, pydantic, SQLite (WAL), pytest + pytest-asyncio + respx, ruff, `uv`.

**Spec:** `docs/superpowers/specs/2026-09-30-rate-limit-and-background-collection-design.md`

## Spec adjustments made while planning (read first)

These refine the approved spec after reading the code; each is the smaller/safer mechanism for the same behavior.

1. **Accumulator API.** The spec's `keep(batch)` per page is kept (used by Apple), but 14 adapters use a one-line `jobs = self.begin_listing()` instead of a `keep()` call per page. It returns a list the base registers, so the adapter's existing `.append`/`.extend` calls feed `adapter.kept` with no loop surgery. Each `fetch_summaries()` call gets its own list (required: `html_paginated` tests `if not jobs:` per call and `html_multi_index` calls it once per index).
2. **Timeout disable.** `collection.source_timeout_seconds` is `float | None` with `gt=0`; `None` disables (no `0`).
3. **No `pipeline --collect-state` flag.** `job-hunter snapshot` writes the archive and prints the exact `pipeline --no-scrape --search <path>` command to run next.
4. **`source-status` unchanged.** `failure_kind` is not stored in SQLite (spec §6: reporting fields live in the archive/state file); `collect status` and the radar show it.
5. **Pipeline status** is derived in the radar stage from the radar's `--result-json` (`rate_limited_sources`), so it works for live and `--no-scrape` runs alike.
6. **Adapter tests.** Behavior tests ("a 429 on page N keeps pages 1..N-1") are written for html_paginated, html_multi_index, oracle_hcm, smartrecruiters, adp_recruiting, eightfold, bosch, successfactors_rmk_v2 and ultipro-style recipes where an existing pagination test supplies the fixture shape; csod, dayforce, icims_attract, phenom, paycom and workday have no existing pagination fixture, so they get the static registration guard plus the shared contract test only.
7. **Stop semantics.** `collect stop` is cooperative: sources not yet started are skipped, in-flight sources finish and persist.

## Global Constraints

- Python `>=3.11`; run everything through `uv run`.
- **Never overwrite real data in tests:** tests use `tmp_path` and `monkeypatch.chdir(tmp_path)`; never write the real `data/searches/`, `data/radar/`, `data/collect/`, `data/locks/`, or `data/jobs.sqlite3`. Manual smoke runs use `--out-dir $(mktemp -d)`/temp projects (`--project`).
- Division of responsibility: Python owns networking, persistence, health, filtering; the skill owns scoring. Do not move scoring into Python.
- Adapters fail loudly (`SchemaError`/`AdapterError`); no credentials/session replay; only `stealth_html` may drive a browser.
- **Prefer false negatives:** a partial or failed run must never call `mark_missing` (it only runs for `HealthStatus.OK`) and must never advance a source's stored non-zero count baseline.
- `HealthStatus` enum is unchanged (`ok|warning|failed|unsupported`); a partial run is `warning` + `failure_kind`; `failed` only when nothing was kept.
- Assessment cache keys on `content_hash` only (untouched).
- New `SourceHealth` fields are optional so old archives load unchanged.
- Defaults: `collection.source_timeout_seconds = 1200`; `collection.background.max_concurrent_sources = 1`, `collection.background.source_delay_seconds = 30`; lock name `"collector"`; state file `data/collect/state.json`; log `data/collect/collector.log`.
- Any content change to a `skills/*/SKILL.md` must bump its frontmatter `version` (minor for new/changed capability).
- No employer names or personal data in tests/docs/skills; use fake values (`Acme`, `jane@example.com`). `tests/test_skills_portable.py` enforces this for skills.
- **Commits:** commit steps below run only if the user has asked for commits in this session; otherwise skip them and leave the working tree. End commit messages with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

1. An adapter raises having registered nothing → source must be `failed` (not an empty `warning`); duplicate registrations collapse by `job_id`. Pinned in Tasks 2 and 1.
2. Test/third-party adapters with no `kept` attribute (the existing `_FakeAdapter` in `tests/test_collector.py`) must not crash the collector's salvage path. Pinned in Task 2.
3. `Retry-After` as an HTTP-date, garbage, `nan`, or `inf` must yield `None`/finite seconds, never crash or emit non-JSON `Infinity` into the archive. Pinned in Task 1.
4. An old archive whose `source_health` rows lack the new keys must render exactly as before (labels "Warning"/"Failed"), and a count-drop `warning` (no `failure_kind`) must still NOT trigger the fallback merge. Pinned in Task 3.
5. `collect start` with a stale `running` state whose PID is dead must be allowed (abandoned); with a live PID it must be refused; `collect stop` in a test must never signal the real test process. Pinned in Task 10.
6. An adapter that raises `SchemaError` after partial pages becomes `warning` whose message includes the exception text (loud, per the project's fail-loudly rule), not a silent success. Pinned in Task 2. (Tension with CLAUDE.md "never silently return partial data" is deliberate and approved in the spec; the message keeps it visible.)

---

## Task 0: Baseline

**Files:** none modified.

- [ ] **Step 1: Confirm the starting state**

Run: `git status --short && git branch --show-current`
Expected: branch `dev`. The earlier Apple empty-page guard / `last_nonzero_job_count` migration / docs edits may still be uncommitted (`M CLAUDE.md docs/SPEC.md src/job_hunter/adapters/apple.py src/job_hunter/storage.py tests/test_adapters.py tests/test_storage.py`); the spec and plan files are untracked.

- [ ] **Step 2: Run the full suite as the baseline**

Run: `uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all pass (760 at the time of writing), ruff clean. If anything fails, stop and report; do not proceed on a red baseline.

- [ ] **Step 3: Commit the earlier fix separately (only if the user approved commits)**

```bash
git add CLAUDE.md docs/SPEC.md src/job_hunter/adapters/apple.py src/job_hunter/storage.py tests/test_adapters.py tests/test_storage.py
git commit -m "fix: fail loudly on empty Apple page; keep non-zero count baseline across zero-job runs" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

# Phase 1 — rate-limit / partial-results core

## Task 1: Error model, health fields, listing accumulator

**Files:**
- Modify: `src/job_hunter/adapters/base.py` (classes near top; `JobAdapter.__init__`; `request()` terminal branch; helper next to `_retry_delay`)
- Modify: `src/job_hunter/models.py` (`SourceHealth`, ~line 203)
- Create: `tests/test_rate_limit.py`

**Interfaces:**
- Produces (used by Tasks 2, 5, 6, 8):
  - `class RateLimitError(AdapterError)` with attrs `http_status: int | None`, `url: str`, `retry_after_seconds: float | None`; constructor `RateLimitError(message, *, http_status=None, url="", retry_after_seconds=None)`.
  - `class ListingTimeout(AdapterError)`.
  - `JobAdapter.begin_listing(self) -> list[JobSummary]`, `JobAdapter.keep(self, batch: Iterable[JobSummary]) -> None`, `JobAdapter.kept` property -> `list[JobSummary]` (flattened across listings, de-duplicated by `job_id`, first occurrence wins, insertion order).
  - `SourceHealth.failure_kind: Literal["rate_limited", "timeout"] | None`, `.http_status: int | None`, `.retry_after_seconds: float | None` (all default `None`).

- [ ] **Step 1: Check nothing catches the old 429 exception type**

Run: `grep -rn "HTTPStatusError" src scripts tests | grep -v "\.pyc"`
Expected: no code depends on a 429 raising `httpx.HTTPStatusError` (the only matches should be unrelated). If one does, adapt it to also accept `RateLimitError` in this task.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_rate_limit.py`:

```python
import httpx
import pytest
import respx
from pydantic import ValidationError

from job_hunter.adapters.base import JobAdapter, ListingTimeout, RateLimitError, _parse_retry_after
from job_hunter.config import CollectionConfig, CompanyConfig
from job_hunter.models import HealthStatus, JobSummary, SourceHealth


class _Adapter(JobAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        return []


def _company() -> CompanyConfig:
    return CompanyConfig(key="acme", company="Acme", adapter="apple", config={})


def _summary(job_id: str) -> JobSummary:
    return JobSummary(
        source_key="acme", source_platform="x", company="Acme", job_id=job_id,
        title=f"Role {job_id}", url=f"https://example.com/{job_id}",
    )


@pytest.mark.asyncio
@respx.mock
async def test_terminal_429_raises_rate_limit_error_with_retry_after():
    respx.get("https://example.com/list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(_company(), client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError) as info:
            await adapter.request("GET", "https://example.com/list")
    assert info.value.http_status == 429
    assert info.value.retry_after_seconds == 120.0
    assert info.value.url == "https://example.com/list"
    assert "HTTP 429" in str(info.value)


@pytest.mark.asyncio
@respx.mock
async def test_terminal_waf_challenge_raises_rate_limit_error_keeping_its_message():
    respx.get("https://example.com/list").mock(
        return_value=httpx.Response(202, headers={"x-amzn-waf-action": "challenge"})
    )
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(_company(), client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError, match="WAF challenge not cleared") as info:
            await adapter.request("GET", "https://example.com/list")
    assert info.value.http_status == 202


@pytest.mark.asyncio
@respx.mock
async def test_terminal_500_is_still_a_plain_http_status_error():
    respx.get("https://example.com/list").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(_company(), client, CollectionConfig(max_retries=0))
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.request("GET", "https://example.com/list")


@pytest.mark.parametrize("value", [None, "", "soon", "nan", "inf", "-inf"])
def test_unusable_retry_after_values_report_none_or_zero_never_non_finite(value):
    result = _parse_retry_after(value)
    assert result is None or result == 0.0


def test_retry_after_numeric_and_http_date():
    assert _parse_retry_after("90") == 90.0
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # long in the past


def test_kept_flattens_listings_and_dedupes_by_job_id_first_wins():
    adapter = _Adapter(_company(), None, CollectionConfig())
    first = adapter.begin_listing()
    first.append(_summary("a"))
    first.append(_summary("b"))
    second = adapter.begin_listing()
    second.append(_summary("b"))
    second.append(_summary("c"))
    adapter.keep([_summary("c"), _summary("d")])
    assert [job.job_id for job in adapter.kept] == ["a", "b", "c", "d"]


def test_kept_is_empty_until_something_is_registered():
    adapter = _Adapter(_company(), None, CollectionConfig())
    assert adapter.kept == []


def test_listing_timeout_is_an_adapter_error():
    from job_hunter.adapters.base import AdapterError

    assert issubclass(ListingTimeout, AdapterError)


def test_source_health_failure_fields_default_to_none_and_validate_kind():
    health = SourceHealth(source_key="a", company="A", status=HealthStatus.OK)
    assert (health.failure_kind, health.http_status, health.retry_after_seconds) == (None, None, None)
    limited = SourceHealth(
        source_key="a", company="A", status=HealthStatus.WARNING,
        failure_kind="rate_limited", http_status=429, retry_after_seconds=90.0,
    )
    assert limited.failure_kind == "rate_limited"
    with pytest.raises(ValidationError):
        SourceHealth(source_key="a", company="A", status=HealthStatus.WARNING, failure_kind="bogus")
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_rate_limit.py -q`
Expected: collection error / FAIL (`cannot import name 'ListingTimeout'` etc.).

- [ ] **Step 4: Implement in `src/job_hunter/adapters/base.py`**

Add `import math` and `from collections.abc import Iterable` to the imports. Replace the `SchemaError` block area with:

```python
class AdapterError(RuntimeError):
    pass


class SchemaError(AdapterError):
    pass


class RateLimitError(AdapterError):
    """A request was still rate-limited (HTTP 429, or a WAF challenge) after every retry."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        url: str = "",
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.url = url
        self.retry_after_seconds = retry_after_seconds


class ListingTimeout(AdapterError):
    """The listing phase exceeded `collection.source_timeout_seconds`."""
```

In `JobAdapter.__init__`, after `self._last_request_at = None`, add:

```python
        # Listings registered via begin_listing()/keep(): what a failing fetch_summaries()
        # had already collected, so the collector can salvage it (see `kept`).
        self._listings: list[list[JobSummary]] = []
        self._kept_extra: list[JobSummary] = []
```

Add these members to `JobAdapter` (after `source_key`):

```python
    def begin_listing(self) -> list[JobSummary]:
        """Returns a fresh accumulator the adapter fills in place (`.append`/`.extend`) and
        eventually returns; the base registers it so a later exception still leaves its
        contents readable via `kept`. One list per fetch_summaries() call."""
        listing: list[JobSummary] = []
        self._listings.append(listing)
        return listing

    def keep(self, batch: Iterable[JobSummary]) -> None:
        """For adapters that can't hand out a live list: register a finished batch."""
        self._kept_extra.extend(batch)

    @property
    def kept(self) -> list[JobSummary]:
        """Everything registered so far, de-duplicated by job_id (first occurrence wins)."""
        seen: set[str] = set()
        result: list[JobSummary] = []
        for listing in (*self._listings, self._kept_extra):
            for job in listing:
                if job.job_id in seen:
                    continue
                seen.add(job.job_id)
                result.append(job)
        return result
```

In `request()`, replace the terminal branch:

```python
                if attempt == max_retries:
                    if is_waf_challenge:
                        raise RateLimitError(
                            f"WAF challenge not cleared after {attempt + 1} attempts: {url}",
                            http_status=response.status_code,
                            url=url,
                        )
                    if response.status_code == 429:
                        raise RateLimitError(
                            f"rate limited (HTTP 429) after {attempt + 1} attempts: {url}",
                            http_status=429,
                            url=url,
                            retry_after_seconds=_parse_retry_after(
                                response.headers.get("Retry-After")
                            ),
                        )
                    response.raise_for_status()
```

Add next to `_retry_delay`:

```python
def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After in seconds for *reporting* (uncapped, unlike `_retry_delay`, which caps what
    we sleep). Non-finite or unparseable values report None so they never reach the archive."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = (target - datetime.now(target.tzinfo)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)
```

- [ ] **Step 5: Implement in `src/job_hunter/models.py`**

In `SourceHealth`, after `error_type`:

```python
    failure_kind: Literal["rate_limited", "timeout"] | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
```

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/test_rate_limit.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS (the old WAF test still passes because `RateLimitError` is an `AdapterError`).

- [ ] **Step 7: Commit (if approved)**

```bash
git add src/job_hunter/adapters/base.py src/job_hunter/models.py tests/test_rate_limit.py
git commit -m "feat: RateLimitError, health failure fields, adapter listing accumulator" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 2: Collector salvages partial listings; per-source timeout; baseline rule

**Files:**
- Modify: `src/job_hunter/config.py` (`CollectionConfig`)
- Modify: `src/job_hunter/collector.py` (imports; `_collect_source` ~lines 150-190 and its `except` ~line 295; new helpers)
- Modify: `src/job_hunter/storage.py` (`update_health` baseline)
- Test: `tests/test_collector.py`, `tests/test_storage.py`

**Interfaces:**
- Consumes (Task 1): `RateLimitError`, `ListingTimeout`, `JobAdapter.kept`, `SourceHealth.failure_kind/http_status/retry_after_seconds`.
- Produces: `CollectionConfig.source_timeout_seconds: float | None = 1200`; collector behavior: partial → `WARNING` + `failure_kind`; module helpers `_failure_fields(exc) -> dict[str, Any]` and `_partial_message(exc, kept: int) -> str` (Task 3's tests assert on the message text format).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collector.py` (after the existing imports add `import asyncio` and `from job_hunter.adapters.base import RateLimitError`):

```python
def _partial_summary(job_id: str) -> JobSummary:
    return JobSummary(
        source_key="fake", source_platform="fake", company="Acme", job_id=job_id,
        title="New Role", url=f"https://example.com/{job_id}", country="US",
        posted_at=datetime.now(UTC) - timedelta(days=5),
    )


class _PartialAdapter:
    """Registers one job, then fails the way a rate-limited paginating adapter does."""

    error: Exception = RateLimitError(
        "rate limited (HTTP 429) after 1 attempts: https://x",
        http_status=429, url="https://x", retry_after_seconds=90.0,
    )
    register = True

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company
        self.kept: list[JobSummary] = []

    async def fetch_summaries(self) -> list[JobSummary]:
        if type(self).register:
            self.kept = [_partial_summary("p1")]
        raise type(self).error

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail(description=f"Description for {summary.job_id}")

    async def aclose(self) -> None:
        pass


class _NoKeptAdapter:
    """Like the older fakes/third-party adapters: no `kept` attribute at all."""

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company

    async def fetch_summaries(self) -> list[JobSummary]:
        raise RuntimeError("boom")

    async def aclose(self) -> None:
        pass


def _fake_company() -> CompanyConfig:
    return CompanyConfig(key="fake", company="Acme", adapter="fake", config={})


async def _search(tmp_path, adapter_cls, **collection):
    settings = Settings(
        database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig(**collection)
    )
    with patch("job_hunter.collector.adapter_class", return_value=adapter_cls):
        return await Collector(settings, [_fake_company()], CandidateProfile()).search(
            include_seen=True
        )


@pytest.mark.asyncio
async def test_rate_limited_listing_keeps_partial_jobs_as_a_warning(tmp_path):
    _PartialAdapter.register = True
    _PartialAdapter.error = RateLimitError(
        "rate limited (HTTP 429) after 1 attempts: https://x",
        http_status=429, url="https://x", retry_after_seconds=90.0,
    )
    result = await _search(tmp_path, _PartialAdapter)
    health = result.source_health[0]
    assert health.status.value == "warning"
    assert health.failure_kind == "rate_limited"
    assert health.http_status == 429
    assert health.retry_after_seconds == 90.0
    assert health.job_count == 1
    assert "rate limited (HTTP 429), Retry-After 90s" in health.message
    assert "kept 1 job(s)" in health.message
    assert result.summary.sources_succeeded == 1
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_job("fake", "p1") is not None


@pytest.mark.asyncio
async def test_partial_listing_never_marks_unseen_jobs_missing(tmp_path):
    # A first, complete run stores stale-1 and fresh-1.
    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
        await Collector(settings, [_fake_company()], CandidateProfile()).search(include_seen=True)
    _PartialAdapter.register = True
    await _search(tmp_path, _PartialAdapter)  # partial run only saw p1
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        row = storage.get_job("fake", "fresh-1")
        assert row["status"] == "active"
        assert row["missing_count"] == 0  # OK-only mark_missing never ran


@pytest.mark.asyncio
async def test_partial_run_does_not_replace_the_count_baseline(tmp_path):
    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
        await Collector(settings, [_fake_company()], CandidateProfile()).search(include_seen=True)
    _PartialAdapter.register = True
    await _search(tmp_path, _PartialAdapter)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.previous_job_count("fake") == 2  # not the partial run's 1


@pytest.mark.asyncio
async def test_rate_limited_listing_with_nothing_kept_is_failed_with_failure_kind(tmp_path):
    _PartialAdapter.register = False
    result = await _search(tmp_path, _PartialAdapter)
    health = result.source_health[0]
    assert health.status.value == "failed"
    assert health.failure_kind == "rate_limited"
    assert health.http_status == 429
    assert health.job_count == 0


@pytest.mark.asyncio
async def test_other_exception_after_partial_pages_is_a_loud_warning(tmp_path):
    _PartialAdapter.register = True
    _PartialAdapter.error = ValueError("job card missing required link/title")
    try:
        result = await _search(tmp_path, _PartialAdapter)
    finally:
        _PartialAdapter.error = RateLimitError("x", http_status=429, url="u")
    health = result.source_health[0]
    assert health.status.value == "warning"
    assert health.failure_kind is None
    assert "ValueError: job card missing required link/title" in health.message
    assert health.error_type == "ValueError"


@pytest.mark.asyncio
async def test_adapter_without_kept_attribute_still_fails_cleanly(tmp_path):
    result = await _search(tmp_path, _NoKeptAdapter)
    health = result.source_health[0]
    assert health.status.value == "failed"
    assert health.message == "boom"


class _SlowAdapter(_PartialAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        self.kept = [_partial_summary("slow-1")]
        await asyncio.sleep(10)
        return self.kept


@pytest.mark.asyncio
async def test_listing_timeout_keeps_partial_jobs_and_reports_timeout(tmp_path):
    result = await _search(tmp_path, _SlowAdapter, source_timeout_seconds=0.05)
    health = result.source_health[0]
    assert health.status.value == "warning"
    assert health.failure_kind == "timeout"
    assert health.job_count == 1
    assert "listing timed out" in health.message


def test_source_timeout_default_and_disable():
    assert CollectionConfig().source_timeout_seconds == 1200
    assert CollectionConfig(source_timeout_seconds=None).source_timeout_seconds is None
```

Append to `tests/test_storage.py`:

```python
def test_a_partial_warning_run_does_not_replace_the_baseline(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _record(storage, 1320)
        _record(storage, 340, HealthStatus.WARNING)
        assert storage.previous_job_count("apple") == 1320
        _record(storage, 1400)
        assert storage.previous_job_count("apple") == 1400
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_collector.py tests/test_storage.py -q`
Expected: the new tests FAIL (collector raises/records `failed`; `source_timeout_seconds` unknown; baseline replaced by 340).

- [ ] **Step 3: Implement — `config.py`**

In `CollectionConfig` add:

```python
    # Wall-clock bound on one source's *listing* phase (fetch_summaries); on expiry the jobs
    # the adapter had already registered are kept. None disables. Detail fetches are already
    # per-job fail-soft and are not bounded by this.
    source_timeout_seconds: float | None = Field(1200, gt=0)
```

- [ ] **Step 4: Implement — `storage.py` `update_health`**

Replace the `baseline` expression added earlier with:

```python
        # Only a clean (`ok`) run that found something advances the baseline; a zero-job run
        # or a partial/failed run never does (see the v5 migration and docs/SPEC.md §8.3).
        baseline = (
            health.job_count
            if health.status == HealthStatus.OK and health.job_count > 0
            else (prior["last_nonzero_job_count"] if prior else None)
        )
```

- [ ] **Step 5: Implement — `collector.py`**

Add imports: `from typing import Any` and `from .adapters.base import ListingTimeout, RateLimitError`. Add module-level helpers above `class Collector`:

```python
def _failure_fields(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, RateLimitError):
        return {
            "failure_kind": "rate_limited",
            "http_status": exc.http_status,
            "retry_after_seconds": exc.retry_after_seconds,
        }
    if isinstance(exc, ListingTimeout):
        return {"failure_kind": "timeout"}
    return {}


def _partial_message(exc: BaseException, kept: int) -> str:
    if isinstance(exc, RateLimitError):
        reason = f"rate limited (HTTP {exc.http_status})" if exc.http_status else "rate limited"
        if exc.retry_after_seconds is not None:
            reason += f", Retry-After {exc.retry_after_seconds:g}s"
    elif isinstance(exc, ListingTimeout):
        reason = "listing timed out"
    else:
        reason = f"{type(exc).__name__}: {exc}"
    return (
        f"{reason}; kept {kept} job(s) fetched before it stopped; "
        "remainder not collected, will retry next run"
    )
```

Add a method to `Collector`:

```python
    async def _fetch_listing(self, adapter) -> list[JobSummary]:
        limit = self.settings.collection.source_timeout_seconds
        if limit is None:
            return await adapter.fetch_summaries()
        timer = asyncio.timeout(limit)
        try:
            async with timer:
                return await adapter.fetch_summaries()
        except TimeoutError as exc:
            if not timer.expired():
                raise  # a socket-level TimeoutError, not our bound
            raise ListingTimeout(f"listing timed out after {limit:g}s") from exc
```

(Add `JobSummary` to the `.models` import.) In `_collect_source`, replace

```python
                summaries = await adapter.fetch_summaries()
                health = SourceHealth(
                    source_key=company.key,
                    company=company.company,
                    status=HealthStatus.OK,
                    job_count=len(summaries),
                )
                health = detect_count_anomaly(
                    health, previous_count, int(company.config.get("anomaly_minimum_previous", 20))
                )
```

with

```python
                listing_error: Exception | None = None
                try:
                    summaries = await self._fetch_listing(adapter)
                except Exception as exc:
                    summaries = list(getattr(adapter, "kept", []))
                    if not summaries:
                        raise  # nothing to salvage: the normal FAILED path below
                    listing_error = exc
                    LOGGER.warning(
                        "Source %s stopped early; keeping %d job(s) fetched so far",
                        company.key, len(summaries), exc_info=True,
                    )
                if listing_error is None:
                    health = SourceHealth(
                        source_key=company.key,
                        company=company.company,
                        status=HealthStatus.OK,
                        job_count=len(summaries),
                    )
                    health = detect_count_anomaly(
                        health, previous_count,
                        int(company.config.get("anomaly_minimum_previous", 20)),
                    )
                else:
                    health = SourceHealth(
                        source_key=company.key,
                        company=company.company,
                        status=HealthStatus.WARNING,
                        job_count=len(summaries),
                        message=_partial_message(listing_error, len(summaries)),
                        error_type=type(listing_error).__name__,
                        **_failure_fields(listing_error),
                    )
```

In the outer `except Exception as exc:` block, add `**_failure_fields(exc),` to the `SourceHealth(...)` call. (`mark_missing` already runs only for `HealthStatus.OK`, so a partial run closes nothing.)

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/test_collector.py tests/test_storage.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 7: Commit (if approved)**

```bash
git add src/job_hunter/config.py src/job_hunter/collector.py src/job_hunter/storage.py tests/test_collector.py tests/test_storage.py
git commit -m "feat: keep partial listings on rate limit/timeout; per-source listing timeout" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 3: Radar — badge, widened fallback, `rate_limited_sources`

**Files:**
- Modify: `scripts/render_radar.py` (`_SOURCE_ISSUE_LABEL`/`_source_issue_row_html` ~504-517; `_apply_collection_fallback` ~525-620; stats dict ~939-946; `main()` result-json ~1085)
- Modify: `scripts/templates/radar_template.html` (CSS near line 586)
- Test: `tests/test_render_radar.py`

**Interfaces:**
- Consumes (Task 2): archive `source_health` rows may carry `failure_kind`, `http_status`, `retry_after_seconds`, `job_count`, message text.
- Produces: `build(...)` stats key `rate_limited_sources: list[dict]` (keys `source_key, company, status, failure_kind, http_status, retry_after_seconds, jobs_kept`); `--result-json` carries it; Task 4 consumes it.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_render_radar.py`; reuse its `_search_json`, `_make_stored_job`, `build`, `Storage`, `CandidateProfile` already imported/defined there)

```python
_RATE_LIMITED_ROW = {
    "source_key": "waymo", "company": "Waymo", "status": "warning", "job_count": 12,
    "failure_kind": "rate_limited", "http_status": 429, "retry_after_seconds": 90.0,
    "message": (
        "rate limited (HTTP 429), Retry-After 90s; kept 12 job(s) fetched before it stopped; "
        "remainder not collected, will retry next run"
    ),
}


def _build_with_health(tmp_path, rows, *, stored_jobs=()):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        for job in stored_jobs:
            storage.upsert_job(job)
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_json([], source_health=rows)))
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([]))
    output_path = tmp_path / "out.html"
    stats = build(
        search_path=search_path, assessments_path=assessments_path, output_path=output_path,
        title="Test Radar", keyword_label=None, new_days=10, now=now,
        database_path=db_path, profile=CandidateProfile(target_domains=["perception"]),
        max_age_days=30,
    )
    return stats, output_path.read_text()


def test_rate_limited_partial_source_shows_badge_reason_and_merges_earlier_jobs(tmp_path):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    earlier = _make_stored_job(
        source_key="waymo", company="Waymo", job_id="earlier",
        title="AV Perception Engineer", posted_at=now - timedelta(days=5),
    )
    stats, html = _build_with_health(tmp_path, [_RATE_LIMITED_ROW], stored_jobs=[earlier])
    assert 'class="tag tag-source-ratelimited"' in html
    assert "Rate limited" in html
    assert "kept 12 job(s) fetched before it stopped" in html
    assert "Collection stopped early today — showing 1 earlier job(s)" in html
    assert "AV Perception Engineer" in html
    assert stats["rate_limited_sources"] == [
        {
            "source_key": "waymo", "company": "Waymo", "status": "warning",
            "failure_kind": "rate_limited", "http_status": 429,
            "retry_after_seconds": 90.0, "jobs_kept": 12,
        }
    ]
    assert stats["stale_source_fallback"] == [
        {"source_key": "waymo", "merged_count": 1, "last_success_at": None}
    ]


def test_timed_out_source_with_nothing_kept_is_labelled_timed_out(tmp_path):
    row = {
        "source_key": "waymo", "company": "Waymo", "status": "failed", "job_count": 0,
        "failure_kind": "timeout", "message": "listing timed out after 1200s",
    }
    stats, html = _build_with_health(tmp_path, [row])
    assert 'class="tag tag-source-timeout"' in html
    assert "Timed out" in html
    assert stats["rate_limited_sources"][0]["failure_kind"] == "timeout"
    assert stats["rate_limited_sources"][0]["jobs_kept"] == 0


def test_legacy_health_rows_without_failure_fields_render_as_before(tmp_path):
    rows = [
        {"source_key": "a", "company": "A", "status": "warning", "message": "Job count dropped 80%."},
        {"source_key": "b", "company": "B", "status": "failed", "message": "Connection reset."},
    ]
    stats, html = _build_with_health(tmp_path, rows)
    assert 'class="tag tag-source-warning"' in html
    assert 'class="tag tag-source-failed"' in html
    assert "Rate limited" not in html and "Timed out" not in html
    assert stats["rate_limited_sources"] == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_render_radar.py -q -k "rate_limited or timed_out or legacy_health"`
Expected: FAIL (no badge class, no stats key).

- [ ] **Step 3: Implement — `scripts/render_radar.py`**

Replace `_source_issue_row_html` with:

```python
_FAILURE_KIND_BADGE = {
    "rate_limited": ("Rate limited", "ratelimited"),
    "timeout": ("Timed out", "timeout"),
}


def _source_issue_badge(health: dict[str, Any]) -> tuple[str, str]:
    """(label, css suffix): a recorded `failure_kind` wins over the bare status."""
    badge = _FAILURE_KIND_BADGE.get(health.get("failure_kind") or "")
    if badge:
        return badge
    status = health.get("status", "failed")
    return _SOURCE_ISSUE_LABEL.get(status, status.title()), status


def _source_issue_row_html(health: dict[str, Any]) -> str:
    status = health.get("status", "failed")
    label, badge_css = _source_issue_badge(health)
    message = health.get("message") or "No error message recorded."
    return f'''
    <div class="source-issue source-issue-{_attr(status)}">
      <span class="tag tag-source-{_attr(badge_css)}">{_e(label)}</span>
      <span class="source-issue-company">{_e(health.get("company") or health.get("source_key"))}</span>
      <span class="source-issue-message">{_e(message)}</span>
    </div>'''


def _rate_limited_sources(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_key": h.get("source_key"),
            "company": h.get("company"),
            "status": h.get("status"),
            "failure_kind": h["failure_kind"],
            "http_status": h.get("http_status"),
            "retry_after_seconds": h.get("retry_after_seconds"),
            "jobs_kept": h.get("job_count") or 0,
        }
        for h in entries
        if h.get("failure_kind")
    ]
```

In `_apply_collection_fallback`:
- Add above it: 

```python
def _wants_fallback(health: dict[str, Any]) -> bool:
    """`failed`, or a `warning` that stopped early on a recorded failure_kind (rate limit /
    timeout). A bare count-drop warning has no failure_kind and still gets no merge."""
    status = health.get("status")
    return status == "failed" or (status == "warning" and bool(health.get("failure_kind")))
```
- Replace `failed_keys = [h.get("source_key") for h in source_issues if h.get("status") == "failed"]` with `failed_keys = [h.get("source_key") for h in source_issues if _wants_fallback(h)]`.
- Replace `if health.get("status") != "failed":` with `if not _wants_fallback(health):`.
- Replace the `if not last_success_at:` / `else:` block with:

```python
        if health.get("status") == "warning":
            # A partial (rate-limited/timed-out) run already stored its own jobs; add the
            # earlier ones this run didn't re-collect. Independent of last_success_at, which a
            # partial run itself just advanced.
            for job in _pool_source_jobs(
                database_path, source_key, profile, max_age_days, keywords=keywords, now=now
            ):
                key = (job.source_key, job.job_id)
                if key in candidates:
                    continue
                candidates[key] = json.loads(job.model_dump_json())
                merged += 1
            note = (
                f"Collection stopped early today — showing {merged} earlier job(s) "
                "this run did not re-collect."
            )
        elif not last_success_at:
            note = "Failed to scrape — no prior successful data available for this source."
        else:
            fallback_jobs = _pool_source_jobs(
                database_path, source_key, profile, max_age_days, keywords=keywords, now=now
            )
            for job in fallback_jobs:
                key = (job.source_key, job.job_id)
                if key in candidates:
                    continue
                candidates[key] = json.loads(job.model_dump_json())
                merged += 1
            note = (
                f"Failed to scrape today — showing {merged} job(s) from the last successful "
                f"scrape on {_fmt_local_date(last_success_at)}."
            )
```
- Update the docstring paragraph "Deliberately scoped to `status == "failed"` only…" to say: scoped to `failed` and to `warning`s carrying a `failure_kind`; count-drop warnings and `unsupported` are still excluded, for the reasons already given.

In `build()`'s returned stats dict, next to `"stale_source_fallback": fallback_provenance,` add `"rate_limited_sources": _rate_limited_sources(source_issues),`. In `main()`'s `--result-json` payload next to `"stale_source_fallback": stats["stale_source_fallback"],` add `"rate_limited_sources": stats["rate_limited_sources"],` and mention it in the `--result-json` help text.

- [ ] **Step 4: Implement — template CSS**

In `scripts/templates/radar_template.html`, after the `.tag-source-warning` rule add:

```css
  .tag-source-ratelimited, .tag-source-timeout { background: var(--status-warning-soft); color: var(--status-warning); }
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_render_radar.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS, including the existing `test_collection_fallback_never_triggers_for_warning_or_unsupported_sources` (its warning has no `failure_kind`).

- [ ] **Step 6: Commit (if approved)**

```bash
git add scripts/render_radar.py scripts/templates/radar_template.html tests/test_render_radar.py
git commit -m "feat(radar): rate-limited/timed-out badges, widened fallback, rate_limited_sources" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 4: Pipeline records rate-limited sources and ends `partial`

**Files:**
- Modify: `src/job_hunter/models.py` (`PipelineManifest`)
- Modify: `src/job_hunter/pipeline.py` (`_run_radar_stage`, after `manifest.radar = result.get("report_path")`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes (Task 3): radar `--result-json` key `rate_limited_sources`.
- Produces: `PipelineManifest.rate_limited_sources: list[dict[str, Any]] | None = None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pipeline.py`; reuses `_write_archive`, `_write_result_json_for`, `_fake_proc`, `_fake_popen`, `run_pipeline`, `Settings`, `CandidateProfile`, `PipelineStatus`)

```python
async def _run_with_radar_payload(tmp_path, monkeypatch, radar_payload):
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=5)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(
                cmd, {"gained": 0, "lost": 0, "diff_report": None, "refiltered_at": "2026-09-17T12:00:00+00:00"}
            )
        elif "scripts/review_with_lm_studio.py" in cmd:
            _write_result_json_for(cmd, {"reviewed": 3, "skipped_cached": 0, "failed": 0})
        elif "scripts/render_radar.py" in cmd:
            _write_result_json_for(cmd, radar_payload)
        else:
            raise AssertionError(f"unexpected subprocess call: {cmd}")
        return _fake_proc()

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))
    return await run_pipeline(Settings(), tmp_path, no_scrape=True, review=True)


async def test_rate_limited_sources_are_recorded_and_downgrade_complete_to_partial(tmp_path, monkeypatch):
    limited = [
        {"source_key": "acme", "company": "Acme", "status": "warning",
         "failure_kind": "rate_limited", "http_status": 429,
         "retry_after_seconds": None, "jobs_kept": 12}
    ]
    manifest = await _run_with_radar_payload(
        tmp_path, monkeypatch,
        {"report_path": "data/radar/default_2026-09-17.html", "rate_limited_sources": limited},
    )
    assert manifest.rate_limited_sources == limited
    assert manifest.status == PipelineStatus.PARTIAL


async def test_no_rate_limited_sources_leaves_a_clean_run_complete(tmp_path, monkeypatch):
    manifest = await _run_with_radar_payload(
        tmp_path, monkeypatch,
        {"report_path": "data/radar/default_2026-09-17.html", "rate_limited_sources": []},
    )
    assert manifest.rate_limited_sources is None
    assert manifest.status == PipelineStatus.COMPLETE
```

(If the file's other async tests carry a `@pytest.mark.asyncio` marker rather than relying on `asyncio_mode = auto`, add the same marker; check with `grep -n "asyncio" pyproject.toml tests/test_pipeline.py | head`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -q -k "rate_limited_sources"`
Expected: FAIL (`PipelineManifest` has no `rate_limited_sources`).

- [ ] **Step 3: Implement**

In `PipelineManifest` (models.py), after `candidates`:

```python
    # Sources the radar reported as rate limited / timed out this run (its `--result-json`
    # `rate_limited_sources`): provenance for a caller polling the manifest, never a gate.
    rate_limited_sources: list[dict[str, Any]] | None = None
```

In `_run_radar_stage` (pipeline.py), right after `manifest.radar = result.get("report_path")`:

```python
    limited = result.get("rate_limited_sources") or None
    manifest.rate_limited_sources = limited
    if limited and manifest.status == PipelineStatus.COMPLETE:
        manifest.status = PipelineStatus.PARTIAL
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_pipeline.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit (if approved)**

```bash
git add src/job_hunter/models.py src/job_hunter/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): record rate-limited sources; finish partial" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 5: Convert Apple (`keep()`) and add the registration contract

**Files:**
- Modify: `src/job_hunter/adapters/apple.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes (Task 1): `JobAdapter.keep`, `.kept`, `RateLimitError`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_adapters.py`; add `from job_hunter.adapters.base import RateLimitError, SchemaError` to the existing base import)

```python
@pytest.mark.asyncio
@respx.mock
async def test_apple_keeps_earlier_pages_when_a_later_batch_is_rate_limited():
    now = datetime.now(UTC)

    def _respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page"))
        if page == 1:
            data = {"totalRecords": 60, "searchResults": [_apple_result("p1", now - timedelta(days=1))]}
            return httpx.Response(200, text=_hydration_html({"loaderData": {"search": data}}))
        return httpx.Response(429, headers={"Retry-After": "60"})

    respx.get(url__regex=r"https://jobs\.apple\.com/en-us/search.*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="apple", company="Apple", adapter="apple",
        config={
            "list_url": "https://jobs.apple.com/en-us/search?location=united-states-USA",
            "page_size": 20, "max_concurrent_pages": 1,
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = AppleAdapter(company, client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError):
            await adapter.fetch_summaries()
    assert [job.job_id for job in adapter.kept] == ["p1"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_adapters.py -q -k "apple_keeps_earlier_pages"`
Expected: FAIL (`adapter.kept == []`).

- [ ] **Step 3: Implement — restructure `apple.py`**

Extract the per-item body of the final loop into a method and register summaries as pages arrive. Add to `AppleAdapter`:

```python
    def _to_summary(self, item: dict, base_url: str) -> JobSummary:
        req_id = str(item.get("reqId") or "").strip()
        title = str(item.get("postingTitle") or "").strip()
        if not req_id or not title:
            raise SchemaError("Apple job entry missing reqId/postingTitle")
        locations = item.get("locations") or []
        city, state, country = _location_fields(locations[0] if locations else {})
        location_raw = ", ".join(part for part in (city, state, country) if part) or None
        slug = item.get("transformedPostingTitle") or ""
        return JobSummary(
            source_key=self.source_key,
            source_platform=self.company.platform or "apple",
            company=self.company.company,
            job_id=req_id,
            title=title,
            url=urljoin(base_url, f"/en-us/details/{req_id}/{slug}"),
            location_raw=location_raw,
            city=city,
            state=state,
            country=country,
            posted_at=parse_flexible_date(item.get("postDateInGMT")),
            raw={"jobSummary": item.get("jobSummary")},
        )
```

In `fetch_summaries`: replace `all_results` handling with

```python
        first = await _fetch_page(1)
        total = int(first.get("totalRecords", 0))
        if not first["searchResults"]:
            # (keep the existing explanatory comment about empty first pages here)
            raise SchemaError(f"Apple search returned no results (totalRecords={total})")
        self.keep(self._to_summary(item, base_url) for item in first["searchResults"])
```

In the batch loop replace `all_results.extend(results)` with `self.keep(self._to_summary(item, base_url) for item in results)` (keep the `last_posted` computation), and replace the whole trailing "jobs: list[JobSummary] = [] … return jobs" block with `return self.kept`. Remove the now-unused `seen_ids` logic (`kept` de-duplicates by `job_id`).

- [ ] **Step 4: Run to verify it passes and nothing regressed**

Run: `uv run pytest tests/test_adapters.py -q -k apple && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: PASS (existing Apple pagination/stale/empty-page tests unchanged).

- [ ] **Step 5: Commit (if approved)**

```bash
git add src/job_hunter/adapters/apple.py tests/test_adapters.py
git commit -m "feat(apple): register fetched pages so a rate limit keeps them" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 6: Convert the paginating adapters (`begin_listing()`)

**Files (each a one-line change inside `fetch_summaries`):**
- Modify: `src/job_hunter/adapters/{adp_recruiting,bosch,csod,dayforce,eightfold,html_paginated,icims_attract,oracle_hcm,paycom,phenom,smartrecruiters,successfactors_rmk_v2,workday}.py` — replace `jobs: list[JobSummary] = []` with `jobs = self.begin_listing()`.
- Modify: `src/job_hunter/adapters/ultipro.py` — replace `summaries: list[JobSummary] = []` with `summaries = self.begin_listing()`.
- Not changed (single request, nothing to salvage): `json_api.py`, `paylocity.py`, `brose.py`, `zf.py` (inherits `html_paginated`), `html_multi_index.py` (calls `html_paginated` once per index; each call registers its own list, so `kept` already spans indexes), `stealth_html.py`.
- Test: `tests/test_adapters.py`

**Interfaces:** Consumes (Task 1) `JobAdapter.begin_listing()` / `.kept`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_adapters.py`; add `from pathlib import Path` already present; add `import job_hunter.adapters as adapters_pkg`)

```python
CONVERTED_ADAPTERS = [
    "adp_recruiting", "bosch", "csod", "dayforce", "eightfold", "html_paginated",
    "icims_attract", "oracle_hcm", "paycom", "phenom", "smartrecruiters",
    "successfactors_rmk_v2", "ultipro", "workday",
]


@pytest.mark.parametrize("name", CONVERTED_ADAPTERS)
def test_paginating_adapter_registers_its_listing_for_salvage(name):
    source = (Path(adapters_pkg.__file__).parent / f"{name}.py").read_text()
    assert "self.begin_listing()" in source, f"{name} must register its accumulator"


@pytest.mark.asyncio
@respx.mock
async def test_html_paginated_keeps_earlier_pages_when_a_later_page_is_rate_limited():
    page1 = f"<main>{''.join(_card(f'P{i}') for i in range(2))}</main>"

    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, text=page1)

    respx.get(url__regex=r".*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="test", company="Test", adapter="html_paginated",
        config={
            "list_url": "https://jobs.example/search", "card_selector": ".job",
            "title_selector": ".title", "link_selector": ".title",
            "location_selector": ".location", "page_number_parameter": "page", "page_size": 2,
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = HtmlPaginatedAdapter(company, client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError):
            await adapter.fetch_summaries()
    assert [job.job_id for job in adapter.kept] == ["P0", "P1"]


@pytest.mark.asyncio
@respx.mock
async def test_html_multi_index_keeps_the_indexes_it_finished_before_a_rate_limit():
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/b"):
            return httpx.Response(429)
        return httpx.Response(200, text=f"<main>{_card('A0')}</main>")

    respx.get(url__regex=r".*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="test", company="Test", adapter="html_multi_index",
        config={
            "index_urls": ["https://jobs.example/a", "https://jobs.example/b"],
            "card_selector": ".job", "title_selector": ".title", "link_selector": ".title",
            "location_selector": ".location",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = HtmlMultiIndexAdapter(company, client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError):
            await adapter.fetch_summaries()
    assert [job.job_id for job in adapter.kept] == ["A0"]


@pytest.mark.asyncio
@respx.mock
async def test_oracle_hcm_keeps_earlier_pages_when_a_later_page_is_rate_limited():
    base = "https://jobs.example/reqs?onlyData=true&finder=findReqs;offset=0"

    def _respond(request: httpx.Request) -> httpx.Response:
        if "offset=0" in str(request.url):
            return httpx.Response(200, json=_oracle_page(["1", "2"], total=4))
        return httpx.Response(429)

    respx.get(url__regex=r".*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="ford", company="Ford", adapter="oracle_hcm",
        config={
            "paginate": True, "list_url": base,
            "items_path": "items.0.requisitionList", "total_path": "items.0.TotalJobsCount",
            "fields": {
                "id": "Id", "title": "Title", "url": "Id",
                "location": "PrimaryLocation", "posted_at": "PostedDate",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = OracleHcmAdapter(company, client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError):
            await adapter.fetch_summaries()
    assert [job.job_id for job in adapter.kept] == ["1", "2"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_adapters.py -q -k "registers_its_listing or keeps_earlier or keeps_the_indexes"`
Expected: FAIL (no adapter registers a listing yet).

- [ ] **Step 3: Apply the one-line change to each adapter**

Run:

```bash
cd src/job_hunter/adapters
for f in adp_recruiting bosch csod dayforce eightfold html_paginated icims_attract oracle_hcm paycom phenom smartrecruiters successfactors_rmk_v2 workday; do
  python3 - "$f.py" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read()
new, n = re.subn(r"^(\s+)jobs: list\[JobSummary\] = \[\]$", r"\1jobs = self.begin_listing()", s, count=1, flags=re.M)
assert n == 1, f"{p}: expected exactly one jobs accumulator declaration"
open(p, "w").write(new)
PY
done
python3 - ultipro.py <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read()
new, n = re.subn(r"^(\s+)summaries: list\[JobSummary\] = \[\]$", r"\1summaries = self.begin_listing()", s, count=1, flags=re.M)
assert n == 1
open(p, "w").write(new)
PY
cd - >/dev/null
git diff --stat src/job_hunter/adapters
```

Expected: 14 files changed, 1 insertion/1 deletion each. If an assertion fires, open that adapter, find the list its `fetch_summaries` returns, and make the equivalent one-line change by hand.

- [ ] **Step 4: Verify every adapter still returns the same list it built**

Run: `for f in adp_recruiting bosch csod dayforce eightfold html_paginated icims_attract oracle_hcm paycom phenom smartrecruiters successfactors_rmk_v2 workday ultipro; do echo "== $f"; grep -n "begin_listing\|^\s*return \(jobs\|summaries\)\|jobs = \(list\|\[\)\|summaries = \(list\|\[\)" src/job_hunter/adapters/$f.py; done`
Expected: each file shows `begin_listing` and a `return jobs`/`return summaries`. If any adapter reassigns `jobs`/`summaries` to a new list *before* mutating it further (e.g. `jobs = [...]` mid-function), that adapter loses salvage after the reassignment: change the reassignment to an in-place update (`jobs[:] = ...`).

- [ ] **Step 5: Run to verify tests pass and nothing regressed**

Run: `uv run pytest tests/test_adapters.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 6: Commit (if approved)**

```bash
git add src/job_hunter/adapters tests/test_adapters.py
git commit -m "feat(adapters): register listings so rate-limited pagination keeps earlier pages" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 7: Phase 1 docs, config comments, skills, live smoke check

**Files:**
- Modify: `docs/SPEC.md` (collection failure semantics near §12; §8.3 health row), `CLAUDE.md` (Collector, health.py, render_radar fallback paragraphs, adapters note), `README.md`, `config/settings.yaml` (comment for `source_timeout_seconds`), `skills/job-hunter/SKILL.md` and `skills/job-radar/SKILL.md` (document the new `partial` cause / radar badge), plus the spec itself (apply the "Spec adjustments" list at the top of this plan).

- [ ] **Step 1: `config/settings.yaml`** — under `collection:` add:

```yaml
  # Wall-clock limit (seconds) on one source's listing phase. When it expires, the jobs the
  # adapter had already fetched are kept (reported as a "Timed out" warning) and the run
  # moves on. Remove the line or set `null` to disable.
  source_timeout_seconds: 1200
```

- [ ] **Step 2: `docs/SPEC.md`** — in the collection section, add a short subsection "Partial collection and rate limits" stating: terminal 429/WAF → `RateLimitError`; adapters register listings (`begin_listing()`/`keep()`); the collector keeps `adapter.kept` on any listing failure (→ `warning` + `failure_kind`, no `mark_missing`, baseline not advanced); `failed` only when nothing was kept; `source_timeout_seconds` bounds the listing phase; the radar badge/fallback widening and `rate_limited_sources` in the radar `--result-json`/pipeline manifest. Also add the three optional `SourceHealth` fields to the archive schema description.

- [ ] **Step 3: `CLAUDE.md`** — update the `collector.py`, `health.py`, `render_radar.py` (stale-source fallback paragraph: now `failed` OR `warning` with `failure_kind`), and adapters paragraphs with one or two sentences each, matching their existing density. Add to the adapters paragraph: "paginating adapters register their accumulator with `begin_listing()` so a rate limit keeps earlier pages".

- [ ] **Step 4: `README.md`** — find where `pipeline-status`/the radar are described (`grep -n "pipeline-status\|Collection Issues" README.md`) and add a sentence: a rate-limited source keeps what it fetched and is named in the radar's Collection Issues with the reason.

- [ ] **Step 5: skills** — `grep -n "^version:" skills/job-hunter/SKILL.md skills/job-radar/SKILL.md`; in each, add one short paragraph (generic wording, no employer names) that a `partial` status can mean a source was rate limited/timed out, listed under `rate_limited_sources` in the manifest, and shown in the radar's Collection Issues. Bump each file's `version` minor component.

- [ ] **Step 6: Apply the spec adjustments** — edit `docs/superpowers/specs/2026-09-30-rate-limit-and-background-collection-design.md` to match this plan's "Spec adjustments" list (§4.1 `begin_listing()`, §4.2 `None` disables, §4.3 `source-status` unchanged, §5.2 `snapshot` prints the next command instead of a `pipeline --collect-state` flag, §5.1 cooperative stop, §4.4 adapter test coverage).

- [ ] **Step 7: Run all checks**

Run: `uv run pytest -q -m "not live" && uv run ruff check . && uv run pytest tests/test_skills_portable.py -q`
Expected: all PASS.

- [ ] **Step 8: Live smoke check (read-only against the network, temp output only)**

Run: `D=$(mktemp -d) && uv run job-hunter source-test apple`
Expected: `status: ok` and a large `job_count`. (This only exercises the unchanged happy path of the real adapter; the rate-limit paths are covered by mocked tests.)

- [ ] **Step 9: Commit (if approved)**

```bash
git add -A docs CLAUDE.md README.md config/settings.yaml skills
git commit -m "docs: partial collection and rate-limit reporting (phase 1)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

# Phase 2 — background collector

## Task 8: Collector hooks — progress callback, stop event, inter-source delay

**Files:**
- Modify: `src/job_hunter/collector.py` (`Collector.__init__`, `search` signature and task creation, `_collect_source` entry)
- Test: `tests/test_collector.py`

**Interfaces:**
- Produces (used by Task 9):
  - `Collector(settings, companies, profile, source_delay_seconds: float = 0.0)`
  - `Collector.search(..., progress: Callable[[str, str, SourceHealth | None], None] | None = None, stop_event: asyncio.Event | None = None)`; `progress(event, source_key, health)` is called with `event` in `{"start", "skipped", "done"}`; `health` is `None` except for `"done"`. Sources skipped because `stop_event` was set are omitted from the returned `source_health`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_collector.py`)

```python
def _named_company(key: str) -> CompanyConfig:
    return CompanyConfig(key=key, company=key.title(), adapter="fake", config={})


class _OkAdapter:
    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company
        self.kept: list[JobSummary] = []

    async def fetch_summaries(self) -> list[JobSummary]:
        return [
            JobSummary(
                source_key=self.company.key, source_platform="fake", company=self.company.company,
                job_id="j1", title="Role", url=f"https://example.com/{self.company.key}",
                country="US", posted_at=datetime.now(UTC) - timedelta(days=1),
            )
        ]

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail(description="d")

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_progress_reports_start_and_done_per_source(tmp_path):
    events: list[tuple[str, str]] = []
    settings = Settings(
        database_path=tmp_path / "jobs.sqlite3",
        collection=CollectionConfig(max_concurrent_sources=1),
    )
    with patch("job_hunter.collector.adapter_class", return_value=_OkAdapter):
        await Collector(
            settings, [_named_company("a"), _named_company("b")], CandidateProfile()
        ).search(progress=lambda event, key, health: events.append((event, key)))
    assert events == [("start", "a"), ("done", "a"), ("start", "b"), ("done", "b")]


@pytest.mark.asyncio
async def test_stop_event_skips_sources_that_have_not_started(tmp_path):
    stop = asyncio.Event()
    events: list[tuple[str, str]] = []

    def _progress(event, key, health):
        events.append((event, key))
        if (event, key) == ("done", "a"):
            stop.set()

    settings = Settings(
        database_path=tmp_path / "jobs.sqlite3",
        collection=CollectionConfig(max_concurrent_sources=1),
    )
    companies = [_named_company(k) for k in ("a", "b", "c")]
    with patch("job_hunter.collector.adapter_class", return_value=_OkAdapter):
        result = await Collector(settings, companies, CandidateProfile()).search(
            progress=_progress, stop_event=stop
        )
    assert [h.source_key for h in result.source_health] == ["a"]
    assert ("skipped", "b") in events and ("skipped", "c") in events
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_job("b", "j1") is None


@pytest.mark.asyncio
async def test_source_delay_waits_between_sources_but_not_before_the_first(tmp_path, monkeypatch):
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds, *args, **kwargs):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("job_hunter.collector.asyncio.sleep", _record_sleep)
    settings = Settings(
        database_path=tmp_path / "jobs.sqlite3",
        collection=CollectionConfig(max_concurrent_sources=1),
    )
    companies = [_named_company(k) for k in ("a", "b", "c")]
    with patch("job_hunter.collector.adapter_class", return_value=_OkAdapter):
        await Collector(settings, companies, CandidateProfile(), source_delay_seconds=7).search()
    assert sleeps.count(7) == 2


@pytest.mark.asyncio
async def test_search_without_hooks_is_unchanged(tmp_path):
    settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
    with patch("job_hunter.collector.adapter_class", return_value=_OkAdapter):
        result = await Collector(settings, [_named_company("a")], CandidateProfile()).search()
    assert [h.source_key for h in result.source_health] == ["a"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_collector.py -q -k "progress_reports or stop_event or source_delay or without_hooks"`
Expected: FAIL (`unexpected keyword argument 'progress'`).

- [ ] **Step 3: Implement**

`Collector.__init__`: add parameter `source_delay_seconds: float = 0.0` and set `self.source_delay_seconds = source_delay_seconds`, `self._sources_started = 0`. Add `from collections.abc import Callable` and the type alias near the top:

```python
ProgressCallback = Callable[[str, str, "SourceHealth | None"], None]
```

`search(...)`: add keyword parameters `progress: ProgressCallback | None = None, stop_event: asyncio.Event | None = None`. Replace the task list with:

```python
                async def _tracked(company: CompanyConfig):
                    outcome = await self._collect_source(
                        company, client, storage, semaphore, refresh_details,
                        progress=progress, stop_event=stop_event,
                    )
                    if progress is not None and outcome[0] is not None:
                        progress("done", company.key, outcome[0])
                    return outcome

                tasks = [_tracked(company) for company in self.companies]
                results = [r for r in await asyncio.gather(*tasks) if r[0] is not None]
```

(delete the original `results = await asyncio.gather(*tasks)` line).

`_collect_source`: add parameters `progress: ProgressCallback | None = None, stop_event: asyncio.Event | None = None` and change its return annotation to `tuple[SourceHealth | None, list[Job]]`. Immediately after `async with semaphore:` insert:

```python
            if stop_event is not None and stop_event.is_set():
                if progress is not None:
                    progress("skipped", company.key, None)
                return None, []
            if self.source_delay_seconds and self._sources_started:
                await asyncio.sleep(self.source_delay_seconds)
                if stop_event is not None and stop_event.is_set():
                    if progress is not None:
                        progress("skipped", company.key, None)
                    return None, []
            self._sources_started += 1
            if progress is not None:
                progress("start", company.key, None)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_collector.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit (if approved)**

```bash
git add src/job_hunter/collector.py tests/test_collector.py
git commit -m "feat(collector): progress callback, cooperative stop, inter-source delay" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 9: `background.py` — state model, runner, slow-mode config

**Files:**
- Modify: `src/job_hunter/config.py` (`BackgroundConfig`, `CollectionConfig.background`)
- Create: `src/job_hunter/background.py`
- Modify: `config/settings.yaml` (comment block)
- Create: `tests/test_background.py`

**Interfaces:**
- Consumes (Task 8): `Collector(..., source_delay_seconds)`, `search(progress=, stop_event=)`; (existing) `runlock.run_lock/pid_alive/process_start_time`, `atomic.atomic_write_text`, `search_archive.archive_path`.
- Produces (used by Tasks 10-11):
  - `STATE_PATH = Path("data/collect/state.json")`, `LOG_PATH = Path("data/collect/collector.log")`
  - `class SourceProgress(BaseModel)`, `class CollectState(BaseModel)` (fields below)
  - `read_state(path: Path = STATE_PATH) -> CollectState | None`, `write_state(state, path=STATE_PATH) -> None`
  - `is_live(state: CollectState) -> bool`, `effective_status(state: CollectState) -> str` (`"abandoned"` when `running` but not live)
  - `async run_collection(settings, companies, profile, *, slow: bool, companies_filter: str | None = None, state_path: Path = STATE_PATH, stop_event: asyncio.Event | None = None) -> CollectState` (raises `RunLockHeld` if another collector holds `"collector"`)
  - `CollectState` fields: `run_id: str`, `pid: int`, `pid_start_time: str | None`, `status: Literal["running","complete","stopped","failed"]`, `slow: bool`, `companies_filter: str | None`, `started_at/updated_at: datetime`, `completed_at: datetime | None`, `archive: str | None`, `error: str | None`, `sources: list[SourceProgress]`; `SourceProgress`: `source_key`, `company`, `status` (`pending|running|ok|warning|failed|unsupported|skipped`), `job_count`, `message`, `error_type`, `failure_kind`, `http_status`, `retry_after_seconds`, `attempted_at`, `finished_at`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_background.py`:

```python
import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from job_hunter import background
from job_hunter.adapters.base import RateLimitError
from job_hunter.background import (
    CollectState, SourceProgress, effective_status, read_state, run_collection, write_state,
)
from job_hunter.config import BackgroundConfig, CandidateProfile, CollectionConfig, CompanyConfig, Settings
from job_hunter.models import JobDetail, JobSummary
from job_hunter.runlock import RunLockHeld, run_lock


def _company(key: str) -> CompanyConfig:
    return CompanyConfig(key=key, company=key.title(), adapter="fake", config={})


def _summary(key: str, job_id: str = "j1") -> JobSummary:
    return JobSummary(
        source_key=key, source_platform="fake", company=key.title(), job_id=job_id,
        title="Role", url=f"https://example.com/{key}/{job_id}", country="US",
        posted_at=datetime.now(UTC) - timedelta(days=1),
    )


class _Ok:
    on_fetch = None  # optional callable(company_key)

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company
        self.kept: list[JobSummary] = []

    async def fetch_summaries(self):
        if type(self).on_fetch:
            type(self).on_fetch(self.company.key)
        return [_summary(self.company.key)]

    async def fetch_detail(self, summary):
        return JobDetail(description="d")

    async def aclose(self):
        pass


class _Limited(_Ok):
    async def fetch_summaries(self):
        self.kept = [_summary(self.company.key)]
        raise RateLimitError("rate limited (HTTP 429) after 1 attempts: https://x", http_status=429, url="https://x")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _Ok.on_fetch = None
    return tmp_path


def _settings(tmp_path, **collection) -> Settings:
    return Settings(
        database_path=tmp_path / "jobs.sqlite3",
        collection=CollectionConfig(max_concurrent_sources=1, **collection),
    )


@pytest.mark.asyncio
async def test_full_run_writes_state_final_archive_and_releases_the_lock(project):
    state_path = project / "state.json"
    with patch("job_hunter.collector.adapter_class", return_value=_Ok):
        state = await run_collection(
            _settings(project), [_company("a"), _company("b")], CandidateProfile(),
            slow=False, state_path=state_path,
        )
    assert state.status == "complete"
    assert [s.status for s in state.sources] == ["ok", "ok"]
    assert all(s.job_count == 1 for s in state.sources)
    assert state.archive and (project / state.archive).exists()
    assert read_state(state_path) == state
    assert not (project / "data" / "locks" / "collector.lock").exists()


@pytest.mark.asyncio
async def test_rate_limited_source_is_recorded_with_its_reason(project):
    with patch("job_hunter.collector.adapter_class", return_value=_Limited):
        state = await run_collection(
            _settings(project), [_company("a")], CandidateProfile(),
            slow=False, state_path=project / "state.json",
        )
    source = state.sources[0]
    assert (source.status, source.failure_kind, source.http_status, source.job_count) == (
        "warning", "rate_limited", 429, 1,
    )
    assert "kept 1 job(s)" in source.message


@pytest.mark.asyncio
async def test_stop_marks_pending_sources_skipped_and_writes_no_archive(project):
    stop = asyncio.Event()
    _Ok.on_fetch = lambda key: stop.set() if key == "a" else None
    with patch("job_hunter.collector.adapter_class", return_value=_Ok):
        state = await run_collection(
            _settings(project), [_company("a"), _company("b"), _company("c")], CandidateProfile(),
            slow=False, state_path=project / "state.json", stop_event=stop,
        )
    assert state.status == "stopped"
    assert [s.status for s in state.sources] == ["ok", "skipped", "skipped"]
    assert state.archive is None


@pytest.mark.asyncio
async def test_second_collector_is_refused_while_the_lock_is_held(project):
    with run_lock("collector"):
        with pytest.raises(RunLockHeld):
            await run_collection(
                _settings(project), [_company("a")], CandidateProfile(),
                slow=False, state_path=project / "state.json",
            )


def test_effective_status_reports_abandoned_for_a_dead_running_state():
    now = datetime.now(UTC)
    dead = CollectState(
        run_id="r", pid=999999999, status="running", slow=False, started_at=now, updated_at=now, sources=[],
    )
    live = dead.model_copy(update={"pid": os.getpid()})
    done = dead.model_copy(update={"status": "complete"})
    assert effective_status(dead) == "abandoned"
    assert effective_status(live) == "running"
    assert effective_status(done) == "complete"


def test_state_round_trips_and_missing_file_is_none(tmp_path):
    path = tmp_path / "x" / "state.json"
    assert read_state(path) is None
    now = datetime.now(UTC)
    state = CollectState(
        run_id="r", pid=1, status="running", slow=True, started_at=now, updated_at=now,
        sources=[SourceProgress(source_key="a", company="A")],
    )
    write_state(state, path)
    loaded = read_state(path)
    assert loaded.run_id == "r" and loaded.sources[0].status == "pending"


def test_background_defaults():
    config = BackgroundConfig()
    assert (config.max_concurrent_sources, config.source_delay_seconds) == (1, 30.0)
    assert CollectionConfig().background == config


@pytest.mark.asyncio
async def test_slow_mode_applies_background_concurrency_and_delay(project):
    seen = {}
    real = background.Collector

    class _Spy(real):
        def __init__(self, settings, companies, profile, source_delay_seconds=0.0):
            seen["concurrency"] = settings.collection.max_concurrent_sources
            seen["delay"] = source_delay_seconds
            super().__init__(settings, companies, profile, source_delay_seconds=0.0)

    settings = Settings(
        database_path=project / "jobs.sqlite3",
        collection=CollectionConfig(
            max_concurrent_sources=5,
            background=BackgroundConfig(max_concurrent_sources=1, source_delay_seconds=12),
        ),
    )
    with patch("job_hunter.background.Collector", _Spy), patch(
        "job_hunter.collector.adapter_class", return_value=_Ok
    ):
        await run_collection(
            settings, [_company("a")], CandidateProfile(), slow=True, state_path=project / "s.json"
        )
    assert seen == {"concurrency": 1, "delay": 12}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_background.py -q`
Expected: collection error (`cannot import name 'BackgroundConfig'` / no `job_hunter.background`).

- [ ] **Step 3: Implement — `config.py`**

Above `CollectionConfig`:

```python
class BackgroundConfig(BaseModel):
    """`job-hunter collect start --slow`: a gentler pace for long background collections."""

    max_concurrent_sources: int = Field(1, ge=1, le=20)
    source_delay_seconds: float = Field(30.0, ge=0)
```

and in `CollectionConfig`: `background: BackgroundConfig = BackgroundConfig()`.

`config/settings.yaml` under `collection:` add a commented example block:

```yaml
  # `job-hunter collect start --slow` pace (background collection):
  # background:
  #   max_concurrent_sources: 1
  #   source_delay_seconds: 30
```

- [ ] **Step 4: Implement — `src/job_hunter/background.py`**

```python
"""Background collection: `job-hunter collect start|status|stop` and `job-hunter snapshot`.

A detached process runs `Collector.search()` while the user filters/reviews/renders against
whatever SQLite already holds. The runner writes a progress state file after every source and
never writes an archive mid-run (so it cannot clobber `refilter_archive.py`'s in-place rewrite);
`snapshot` builds an archive on demand from that state plus SQLite. The runner holds only the
`collector` lock, never the shared `job-hunter` lock. See
docs/superpowers/specs/2026-09-30-rate-limit-and-background-collection-design.md section 5.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from .atomic import atomic_write_text
from .collector import Collector, select_companies
from .config import CandidateProfile, CompanyConfig, Settings
from .models import SourceHealth
from .runlock import pid_alive, process_start_time, run_lock
from .search_archive import archive_path

STATE_PATH = Path("data/collect/state.json")
LOG_PATH = Path("data/collect/collector.log")

# Aliases so tests can patch them without touching the shared `subprocess`/`os` modules.
_popen = subprocess.Popen
_kill = os.kill

SourceStatus = Literal["pending", "running", "ok", "warning", "failed", "unsupported", "skipped"]
RunStatus = Literal["running", "complete", "stopped", "failed"]
_FINISHED: frozenset[str] = frozenset({"ok", "warning", "failed", "unsupported"})


class SourceProgress(BaseModel):
    source_key: str
    company: str
    status: SourceStatus = "pending"
    job_count: int = 0
    message: str | None = None
    error_type: str | None = None
    failure_kind: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    attempted_at: datetime | None = None
    finished_at: datetime | None = None


class CollectState(BaseModel):
    run_id: str
    pid: int
    pid_start_time: str | None = None
    status: RunStatus = "running"
    slow: bool = False
    companies_filter: str | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    archive: str | None = None
    error: str | None = None
    sources: list[SourceProgress]


class CollectorRunning(RuntimeError):
    pass


def read_state(path: Path = STATE_PATH) -> CollectState | None:
    try:
        return CollectState.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def write_state(state: CollectState, path: Path = STATE_PATH) -> None:
    state.updated_at = datetime.now(UTC)
    atomic_write_text(path, state.model_dump_json(indent=2) + "\n")


def is_live(state: CollectState) -> bool:
    """PID alive AND still the same process (guards against PID reuse), like `pipeline-status`."""
    if not pid_alive(state.pid):
        return False
    if state.pid_start_time is not None:
        current = process_start_time(state.pid)
        if current is not None and current != state.pid_start_time:
            return False
    return True


def effective_status(state: CollectState) -> str:
    if state.status == "running" and not is_live(state):
        return "abandoned"
    return state.status


def _apply_health(source: SourceProgress, health: SourceHealth) -> None:
    source.status = health.status.value  # type: ignore[assignment]
    source.job_count = health.job_count
    source.message = health.message
    source.error_type = health.error_type
    source.failure_kind = health.failure_kind
    source.http_status = health.http_status
    source.retry_after_seconds = health.retry_after_seconds
    source.attempted_at = health.attempted_at
    source.finished_at = datetime.now(UTC)


async def run_collection(
    settings: Settings,
    companies: list[CompanyConfig],
    profile: CandidateProfile,
    *,
    slow: bool,
    companies_filter: str | None = None,
    state_path: Path = STATE_PATH,
    stop_event: asyncio.Event | None = None,
) -> CollectState:
    with run_lock("collector"):
        now = datetime.now(UTC)
        state = CollectState(
            run_id=str(uuid4()), pid=os.getpid(), pid_start_time=process_start_time(os.getpid()),
            slow=slow, companies_filter=companies_filter, started_at=now, updated_at=now,
            sources=[SourceProgress(source_key=c.key, company=c.company) for c in companies],
        )
        write_state(state, state_path)
        by_key = {s.source_key: s for s in state.sources}

        stop = stop_event or asyncio.Event()
        if stop_event is None:
            try:
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # not the main thread / unsupported platform: stop via stop_event only

        def progress(event: str, key: str, health: SourceHealth | None) -> None:
            source = by_key[key]
            if event == "start":
                source.status = "running"
                source.attempted_at = datetime.now(UTC)
            elif event == "skipped":
                source.status = "skipped"
            elif event == "done" and health is not None:
                _apply_health(source, health)
            write_state(state, state_path)

        delay = 0.0
        if slow:
            background = settings.collection.background
            settings = settings.model_copy(
                update={
                    "collection": settings.collection.model_copy(
                        update={"max_concurrent_sources": background.max_concurrent_sources}
                    )
                }
            )
            delay = background.source_delay_seconds

        try:
            result = await Collector(
                settings, companies, profile, source_delay_seconds=delay
            ).search(progress=progress, stop_event=stop)
        except Exception as exc:  # recorded, not swallowed: the state file is the report
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            state.completed_at = datetime.now(UTC)
            write_state(state, state_path)
            raise

        if stop.is_set():
            state.status = "stopped"
            for source in state.sources:
                if source.status in {"pending", "running"}:
                    source.status = "skipped"
        else:
            path = archive_path(None, companies=companies_filter)
            atomic_write_text(
                path,
                json.dumps(result.model_dump(mode="json"), indent=2, default=str, ensure_ascii=False)
                + "\n",
            )
            state.archive = str(path)
            state.status = "complete"
        state.completed_at = datetime.now(UTC)
        write_state(state, state_path)
        return state


def main(argv: list[str] | None = None) -> int:
    """Entry point of the detached process: `python -m job_hunter.background`."""
    from .config import load_companies, load_profile, load_settings
    from .rootutil import add_project_argument, chdir_to_project_root

    parser = argparse.ArgumentParser(prog="job_hunter.background")
    parser.add_argument("--companies", default=None)
    parser.add_argument("--slow", action="store_true")
    add_project_argument(parser)
    args = parser.parse_args(argv)
    chdir_to_project_root(args.project)
    settings = load_settings()
    companies = select_companies(load_companies(), args.companies)
    state = asyncio.run(
        run_collection(
            settings, companies, load_profile(),
            slow=args.slow, companies_filter=args.companies,
        )
    )
    return 0 if state.status in {"complete", "stopped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
```

Before relying on the imports in `main()`, verify them: `grep -n "^def load_settings\|^def load_companies\|^def load_profile" src/job_hunter/config.py` (if a name differs, e.g. the loaders live in `cli.py`, import from where they actually live; the test suite does not exercise `main()`, Task 10's smoke step does).

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_background.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS. (`_Spy` resets the delay to 0 in the real collector so the test is instant; it only records what `run_collection` passed.)

- [ ] **Step 6: Commit (if approved)**

```bash
git add src/job_hunter/config.py src/job_hunter/background.py config/settings.yaml tests/test_background.py
git commit -m "feat: background collection runner, state file, slow-mode config" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 10: `collect start|status|stop` CLI

**Files:**
- Modify: `src/job_hunter/background.py` (add `start_background`, `request_stop`, `format_status`, `cli_collect`)
- Modify: `src/job_hunter/cli.py` (parser; dispatch)
- Test: `tests/test_background.py`

**Interfaces:**
- Consumes (Task 9): `read_state`, `is_live`, `effective_status`, `CollectState`, `STATE_PATH`, `LOG_PATH`, `_popen`, `_kill`.
- Produces: `start_background(*, project_root: Path, companies: str | None, slow: bool, state_path: Path = STATE_PATH) -> int` (returns child PID; raises `CollectorRunning`); `request_stop(state_path: Path = STATE_PATH) -> bool`; `format_status(state: CollectState) -> list[str]`; `cli_collect(args, state_path: Path = STATE_PATH) -> int` where `args` has `collect_command` in `{start,status,stop}`, plus `companies`, `slow`, `json`, `project_root` attributes as relevant.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_background.py`; add imports `from types import SimpleNamespace` and `from job_hunter.background import CollectorRunning, cli_collect, format_status, request_stop, start_background`)

```python
def _state(status="running", pid=None, **extra) -> CollectState:
    now = datetime.now(UTC)
    return CollectState(
        run_id="run-1", pid=pid if pid is not None else os.getpid(), status=status, slow=False,
        started_at=now, updated_at=now, sources=extra.pop("sources", []), **extra,
    )


def test_start_is_refused_while_a_live_collector_is_running(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    write_state(_state(), state_path)  # pid == this test process: live
    monkeypatch.setattr("job_hunter.background._popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(CollectorRunning, match="already running"):
        start_background(project_root=tmp_path, companies=None, slow=False, state_path=state_path)


def test_start_is_allowed_when_the_recorded_run_is_abandoned(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    write_state(_state(pid=999999999), state_path)  # dead pid, still marked running
    spawned = {}

    class _Proc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr("job_hunter.background._popen", _fake_popen)
    pid = start_background(project_root=tmp_path, companies="a,b", slow=True, state_path=state_path)
    assert pid == 4242
    assert spawned["cmd"][1:3] == ["-m", "job_hunter.background"]
    assert "--companies" in spawned["cmd"] and "a,b" in spawned["cmd"] and "--slow" in spawned["cmd"]
    assert spawned["kwargs"]["start_new_session"] is True


def test_stop_signals_only_a_live_collector(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr("job_hunter.background._kill", lambda pid, sig: calls.append((pid, sig)))
    assert request_stop(state_path) is False  # no state file
    write_state(_state(pid=999999999), state_path)
    assert request_stop(state_path) is False  # abandoned
    write_state(_state(), state_path)
    assert request_stop(state_path) is True
    assert calls == [(os.getpid(), signal.SIGTERM)]


def test_format_status_lists_progress_and_rate_limited_sources():
    sources = [
        SourceProgress(source_key="a", company="Acme", status="ok", job_count=5),
        SourceProgress(
            source_key="b", company="Beta", status="warning", job_count=12,
            failure_kind="rate_limited", http_status=429,
        ),
        SourceProgress(source_key="c", company="Gamma"),
    ]
    lines = format_status(_state(sources=sources))
    text = "\n".join(lines)
    assert "2/3 sources finished" in text
    assert "Beta" in text and "rate limited" in text.lower() and "12" in text


def test_cli_status_json_reports_effective_status(tmp_path, capsys):
    state_path = tmp_path / "state.json"
    write_state(_state(pid=999999999), state_path)
    code = cli_collect(SimpleNamespace(collect_command="status", json=True), state_path=state_path)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "abandoned" and out["run_id"] == "run-1"


def test_cli_status_without_state_exits_2(tmp_path, capsys):
    code = cli_collect(
        SimpleNamespace(collect_command="status", json=False), state_path=tmp_path / "nope.json"
    )
    assert code == 2 and "no collection" in capsys.readouterr().err.lower()
```

(Add `import json` and `import signal` at the top of the test file.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_background.py -q -k "start_is or stop_signals or format_status or cli_status"`
Expected: FAIL (names not defined).

- [ ] **Step 3: Implement — add to `background.py`**

```python
def start_background(
    *, project_root: Path, companies: str | None, slow: bool, state_path: Path = STATE_PATH
) -> int:
    existing = read_state(state_path)
    if existing is not None and existing.status == "running" and is_live(existing):
        raise CollectorRunning(
            f"a collector is already running (pid {existing.pid}, run {existing.run_id}); "
            "use `job-hunter collect status` or `collect stop`"
        )
    log_path = Path(project_root) / LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "job_hunter.background", "--project", str(project_root)]
    if companies:
        cmd += ["--companies", companies]
    if slow:
        cmd.append("--slow")
    with log_path.open("ab") as handle:
        proc = _popen(
            cmd, cwd=project_root, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True,
        )
    return proc.pid


def request_stop(state_path: Path = STATE_PATH) -> bool:
    """Cooperative stop: SIGTERM sets the runner's stop event; in-flight sources finish."""
    state = read_state(state_path)
    if state is None or state.status != "running" or not is_live(state):
        return False
    _kill(state.pid, signal.SIGTERM)
    return True


def format_status(state: CollectState) -> list[str]:
    finished = sum(1 for s in state.sources if s.status in _FINISHED)
    lines = [
        f"collector run {state.run_id}: {effective_status(state)} (pid {state.pid}), "
        f"{finished}/{len(state.sources)} sources finished"
    ]
    for source in state.sources:
        if source.failure_kind:
            reason = source.failure_kind.replace("_", " ")
            if source.http_status:
                reason += f" (HTTP {source.http_status})"
            lines.append(f"  {reason}: {source.company} — kept {source.job_count} job(s)")
        elif source.status == "failed":
            lines.append(f"  failed: {source.company} — {source.message or 'no message'}")
    if state.archive:
        lines.append(f"archive: {state.archive}")
    return lines


def cli_collect(args, state_path: Path = STATE_PATH) -> int:
    command = args.collect_command
    if command == "start":
        try:
            pid = start_background(
                project_root=Path.cwd(), companies=args.companies, slow=args.slow,
                state_path=state_path,
            )
        except CollectorRunning as exc:
            print(f"job-hunter: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"started": True, "pid": pid, "state": str(state_path), "log": str(LOG_PATH)}))
        return 0
    if command == "stop":
        if request_stop(state_path):
            print("stop requested; in-flight sources will finish, the rest are skipped")
            return 0
        print("job-hunter: no live collector to stop", file=sys.stderr)
        return 2
    state = read_state(state_path)
    if state is None:
        print("job-hunter: no collection has been started in this project", file=sys.stderr)
        return 2
    if args.json:
        payload = state.model_dump(mode="json")
        payload["status"] = effective_status(state)
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(format_status(state)))
    return 0
```

- [ ] **Step 4: Implement — `cli.py`**

After the existing `sub.add_parser("source-status")` line, add:

```python
    collect = sub.add_parser("collect", help="run the collector in the background")
    collect_sub = collect.add_subparsers(dest="collect_command", required=True)
    collect_start = collect_sub.add_parser("start")
    collect_start.add_argument("--companies", default=None)
    collect_start.add_argument(
        "--slow", action="store_true",
        help="use settings collection.background (1 source at a time, delay between sources)",
    )
    collect_status = collect_sub.add_parser("status")
    collect_status.add_argument("--json", action="store_true")
    collect_sub.add_parser("stop")
    for collect_parser in collect_sub.choices.values():
        add_project_argument(collect_parser, suppress_default=True)
```

In `main()`, right after `settings = load_settings()` add:

```python
        if args.command == "collect":
            from .background import cli_collect

            return cli_collect(args)
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_background.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 6: Manual smoke test in a throwaway project (never the real data)**

```bash
P=$(mktemp -d) && cp -R config "$P/config" && mkdir -p "$P/data" \
&& uv run job-hunter collect start --project "$P" --companies apple --slow \
&& sleep 5 && uv run job-hunter collect status --project "$P" \
&& uv run job-hunter collect stop --project "$P"; uv run job-hunter collect status --project "$P"
```

Expected: `started: true`, status shows `running` with `0/1` or `1/1` sources, `collect stop` reports a stop request (or "no live collector" if it already finished), the final status shows `stopped`/`complete`. Note: the temp project has its own `data/` so nothing in the real project is written; the Apple fetch is a real network read.

- [ ] **Step 7: Commit (if approved)**

```bash
git add src/job_hunter/background.py src/job_hunter/cli.py tests/test_background.py
git commit -m "feat: collect start/status/stop CLI" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 11: `snapshot` — archive on demand from SQLite + state

**Files:**
- Modify: `src/job_hunter/background.py` (add `snapshot_archive`, `write_snapshot`, `refilter_snapshot`, `cli_snapshot`)
- Modify: `src/job_hunter/cli.py` (parser + dispatch)
- Test: `tests/test_background.py`

**Interfaces:**
- Consumes (Task 9): `CollectState`, `read_state`, `_FINISHED`; existing `search_archive.archive_path`, `scripts/refilter_archive.py --search PATH --no-report`.
- Produces: `snapshot_archive(state: CollectState, *, now: datetime | None = None) -> dict` (a minimal archive: `run`, `summary`, `source_health` for finished sources only, empty `candidates`); `write_snapshot(state, *, now=None) -> Path`; `refilter_snapshot(path: Path, project_root: Path) -> int` (runs `scripts/refilter_archive.py`, returns its exit code); `cli_snapshot(state_path=STATE_PATH) -> int`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_background.py`; add `from job_hunter.background import snapshot_archive, write_snapshot, cli_snapshot`)

```python
def _mixed_state() -> CollectState:
    sources = [
        SourceProgress(source_key="a", company="Acme", status="ok", job_count=5),
        SourceProgress(
            source_key="b", company="Beta", status="warning", job_count=12,
            failure_kind="rate_limited", http_status=429, retry_after_seconds=90.0,
            message="rate limited (HTTP 429); kept 12 job(s) fetched before it stopped",
        ),
        SourceProgress(source_key="c", company="Gamma", status="failed", message="boom", error_type="X"),
        SourceProgress(source_key="d", company="Delta", status="running"),
        SourceProgress(source_key="e", company="Eps"),
        SourceProgress(source_key="f", company="Zeta", status="skipped"),
    ]
    return _state(sources=sources)


def test_snapshot_archive_contains_only_finished_sources_with_their_reasons():
    data = snapshot_archive(_mixed_state(), now=datetime(2026, 9, 30, tzinfo=UTC))
    keys = [h["source_key"] for h in data["source_health"]]
    assert keys == ["a", "b", "c"]  # running/pending/skipped are not in scope yet
    limited = data["source_health"][1]
    assert limited["status"] == "warning" and limited["failure_kind"] == "rate_limited"
    assert limited["http_status"] == 429 and limited["job_count"] == 12
    assert data["candidates"] == []
    summary = data["summary"]
    assert (summary["sources_attempted"], summary["sources_succeeded"], summary["sources_failed"]) == (3, 2, 1)
    assert summary["partial_failure"] is True


def test_write_snapshot_writes_a_resolvable_archive(project):
    path = write_snapshot(_mixed_state(), now=datetime(2026, 9, 30, tzinfo=UTC))
    assert path.exists() and path.parent.name == "searches"
    assert json.loads(path.read_text())["source_health"][0]["source_key"] == "a"


def test_cli_snapshot_runs_the_refilter_and_prints_the_next_command(project, monkeypatch, capsys):
    write_state(_mixed_state(), project / "data" / "collect" / "state.json")
    ran = {}

    def _fake_refilter(path, project_root):
        ran["path"] = path
        return 0

    monkeypatch.setattr("job_hunter.background.refilter_snapshot", _fake_refilter)
    code = cli_snapshot(state_path=project / "data" / "collect" / "state.json")
    out = capsys.readouterr().out
    assert code == 0
    assert str(ran["path"]) in out
    assert "pipeline --no-scrape --search" in out


def test_cli_snapshot_without_state_exits_2(project, capsys):
    assert cli_snapshot(state_path=project / "nope.json") == 2
    assert "collect start" in capsys.readouterr().err


def test_cli_snapshot_with_nothing_finished_yet_exits_2(project, capsys):
    write_state(_state(sources=[SourceProgress(source_key="a", company="A")]), project / "s.json")
    assert cli_snapshot(state_path=project / "s.json") == 2
    assert "no source has finished" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_background.py -q -k "snapshot"`
Expected: FAIL (names not defined).

- [ ] **Step 3: Implement — add to `background.py`**

```python
def snapshot_archive(state: CollectState, *, now: datetime | None = None) -> dict:
    """A minimal archive for refilter: `source_health` holds only sources that have *finished*
    (so refilter's `_successful_source_scope` and the radar's Collection Issues reflect exactly
    what has been collected so far); `candidates` is empty — refilter rebuilds it from SQLite."""
    now = now or datetime.now(UTC)
    health = [
        {
            "source_key": s.source_key, "company": s.company, "status": s.status,
            "job_count": s.job_count, "message": s.message, "error_type": s.error_type,
            "failure_kind": s.failure_kind, "http_status": s.http_status,
            "retry_after_seconds": s.retry_after_seconds,
            "attempted_at": (s.attempted_at or now).isoformat(),
        }
        for s in state.sources
        if s.status in _FINISHED
    ]
    succeeded = sum(1 for h in health if h["status"] in {"ok", "warning"})
    return {
        "run": {
            "run_id": state.run_id,
            "started_at": state.started_at.isoformat(),
            "completed_at": now.isoformat(),
        },
        "summary": {
            "sources_attempted": len(health),
            "sources_succeeded": succeeded,
            "sources_failed": len(health) - succeeded,
            "jobs_observed": sum(h["job_count"] for h in health),
            "us_eligible": 0,
            "prefilter_candidates": 0,
            "stale_excluded": 0,
            "partial_failure": 0 < succeeded < len(health),
        },
        "source_health": health,
        "candidates": [],
    }


def write_snapshot(state: CollectState, *, now: datetime | None = None) -> Path:
    path = archive_path(None, companies=state.companies_filter)
    atomic_write_text(
        path, json.dumps(snapshot_archive(state, now=now), indent=2, ensure_ascii=False) + "\n"
    )
    return path


def refilter_snapshot(path: Path, project_root: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "scripts/refilter_archive.py", "--project", str(project_root),
         "--search", str(path), "--no-report"],
        cwd=project_root, check=False,
    )
    return proc.returncode


def cli_snapshot(state_path: Path = STATE_PATH) -> int:
    state = read_state(state_path)
    if state is None:
        print(
            "job-hunter: no collection state found; run `job-hunter collect start` first",
            file=sys.stderr,
        )
        return 2
    if not any(s.status in _FINISHED for s in state.sources):
        print("job-hunter: no source has finished yet; try again shortly", file=sys.stderr)
        return 2
    path = write_snapshot(state)
    code = refilter_snapshot(path, Path.cwd())
    if code != 0:
        print(f"job-hunter: refilter of {path} failed (exit {code})", file=sys.stderr)
        return code
    print(path)
    print(f"next: job-hunter pipeline --no-scrape --search {path}   (add --review to score new jobs)")
    return 0
```

- [ ] **Step 4: Implement — `cli.py`**

Next to the `collect` parser: `sub.add_parser("snapshot", help="build an archive from what the collector has stored so far")`. In `main()`, next to the `collect` dispatch:

```python
        if args.command == "snapshot":
            from .background import cli_snapshot

            return cli_snapshot()
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_background.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 6: Commit (if approved)**

```bash
git add src/job_hunter/background.py src/job_hunter/cli.py tests/test_background.py
git commit -m "feat: snapshot an archive from the background collector's progress" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 12: `cleanup --apply` refuses while the collector runs

**Files:**
- Modify: `src/job_hunter/cleanup.py` (~line 213)
- Test: `tests/test_cleanup.py` (create if absent: `ls tests/test_cleanup.py`; otherwise append)

**Interfaces:** Consumes `runlock.run_lock("collector")`, `RunLockHeld`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from job_hunter.cleanup import run_cleanup
from job_hunter.config import Settings
from job_hunter.runlock import RunLockHeld, run_lock


def test_cleanup_apply_is_refused_while_the_collector_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(database_path=tmp_path / "jobs.sqlite3")
    with run_lock("collector"):
        with pytest.raises(RunLockHeld):
            run_cleanup(settings, apply=True, write_export=False)


def test_cleanup_dry_run_ignores_the_collector_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(database_path=tmp_path / "jobs.sqlite3")
    with run_lock("collector"):
        run_cleanup(settings, apply=False, write_export=False)  # must not raise
```

(Before writing it, read `run_cleanup`'s signature: `grep -n "def run_cleanup" -A12 src/job_hunter/cleanup.py`, and add any required keyword arguments to both calls.)

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_cleanup.py -q -k collector_lock`
Expected: `test_cleanup_apply_is_refused...` FAILS (`DID NOT RAISE`).

- [ ] **Step 3: Implement**

In `cleanup.py` add above `run_cleanup`:

```python
@contextlib.contextmanager
def _apply_locks():
    """Cleanup deletes rows a running collector may be writing: hold both the shared
    pipeline lock and the background collector's lock (see background.py)."""
    with run_lock("job-hunter"), run_lock("collector"):
        yield
```

and change `with run_lock("job-hunter") if apply else contextlib.nullcontext():` to `with _apply_locks() if apply else contextlib.nullcontext():`. Update the docstring sentence to mention the collector lock.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_cleanup.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit (if approved)**

```bash
git add src/job_hunter/cleanup.py tests/test_cleanup.py
git commit -m "feat(cleanup): refuse --apply while the background collector runs" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 13: Phase 2 docs, skills, final verification

**Files:**
- Modify: `docs/SPEC.md` (new "Background collector" section), `CLAUDE.md` (architecture bullet for `background.py`; commands list), `README.md` (commands), `skills/job-scout/SKILL.md` (new "Background collection" subsection; bump `version` minor), `skills/job-hunter/SKILL.md` (one line pointing to it; bump `version` patch/minor as the change warrants).

- [ ] **Step 1: `CLAUDE.md`** — add `collect start|status|stop|snapshot` to the Commands block:

```bash
uv run job-hunter collect start --slow          # background collection (state: data/collect/state.json)
uv run job-hunter collect status [--json]
uv run job-hunter collect stop                  # cooperative: in-flight sources finish
uv run job-hunter snapshot                      # archive from what's stored so far; then: pipeline --no-scrape --search <path>
```

and add a `src/job_hunter/background.py` architecture bullet: detached `Collector.search()` runner; holds only `run_lock("collector")` (never `"job-hunter"`); writes `data/collect/state.json` per source; never writes an archive mid-run; `snapshot` builds one from SQLite + state for refilter; `cleanup --apply` refuses while the collector lock is held; `--slow` uses `collection.background`.

- [ ] **Step 2: `docs/SPEC.md`** — add "Background collector" (runner, state file schema, locks, snapshot, stop semantics, slow mode, non-goals: no scheduling).

- [ ] **Step 3: `README.md`** — add the four commands with one-line descriptions.

- [ ] **Step 4: `skills/job-scout/SKILL.md`** — `grep -n "^version:" skills/job-scout/SKILL.md`; add a short "Background collection" subsection (generic wording): start with `collect start [--slow]`, poll with `collect status`, build an archive any time with `snapshot`, then `pipeline --no-scrape --search <path> [--review]`; do not run `cleanup --apply` while a collector is running. Bump the `version` minor component. In `skills/job-hunter/SKILL.md` add one sentence referencing it and bump `version`.

- [ ] **Step 5: Full verification**

Run: `uv run pytest -q -m "not live" && uv run ruff check . && uv run pytest tests/test_skills_portable.py tests/test_no_owner_pii_in_shareable_files.py -q`
Expected: everything PASS (the PII test skips if there is no real profile).

- [ ] **Step 6: End-to-end smoke in a throwaway project** (never the real data): repeat Task 10's smoke test, then `uv run job-hunter snapshot --project "$P"` and confirm it prints an archive path under `$P/data/searches/` and the `next:` command; then `uv run job-hunter pipeline --no-scrape --search <that path> --project "$P" --skip-radar` is *not* required (it would need a profile/resume); confirm only that the archive's `source_health` lists the finished source.

- [ ] **Step 7: Commit (if approved)**

```bash
git add -A docs CLAUDE.md README.md skills
git commit -m "docs: background collector (phase 2)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review (completed)

**Spec coverage:** §4.1 error model → Task 1; §4.2 collector, timeout, baseline → Task 2; §4.3 radar/pipeline reporting → Tasks 3-4; §4.4 adapter rollout → Tasks 5-6; docs/skills → Tasks 7 and 13; §5.1 runner/state → Tasks 9-10; §5.2 snapshot → Task 11; §5.3 locks → Tasks 9 (collector lock) and 12 (cleanup); §5.4 slow mode → Task 9 (`BackgroundConfig`) with `--slow` wired in Task 10; §6 compat (optional fields, no DB change beyond v5) → Tasks 1-3; §7 testing → in each task; §8 risks documented in the spec. Gaps accepted and listed under "Spec adjustments" (adapter test coverage, `source-status`, no `--collect-state` flag).

**Placeholder scan:** none; every code step contains the code. Two steps intentionally say "read X first" (loader function names in Task 9, `run_cleanup` signature in Task 12, asyncio test marker in Task 4) because they depend on exact existing signatures and name the command that answers them.

**Type consistency:** `begin_listing`/`keep`/`kept` (Task 1) used in Tasks 2, 5, 6; `_failure_fields`/`_partial_message` (Task 2) message format asserted in Tasks 3, 9, 10; `progress(event, key, health)` and `stop_event` (Task 8) used in Task 9; `CollectState`/`SourceProgress`/`read_state`/`write_state`/`is_live`/`effective_status`/`_FINISHED`/`_popen`/`_kill` (Task 9) used in Tasks 10-11; `rate_limited_sources` keys (Task 3) consumed by Task 4.
