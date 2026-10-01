# Rate-limit-tolerant and background collection — design

Date: 2026-09-30. Status: awaiting review. Path: architectural (changes the adapter base
interface, collector failure semantics, the health model, radar reporting, and adds a new
collector process).

## 1. Problem

A rate-limited source costs the user its work and can stall the whole run:

1. **Partial work is discarded.** A terminal 429 or WAF challenge raised mid-pagination
   propagates out of `fetch_summaries()`; the pages already fetched never reach SQLite. Seen
   live: Waymo (`default_2026-09-15.json`, `default_2026-09-17.json`) failed with "WAF challenge
   not cleared after 7 attempts: …page=2", losing page 1.
2. **Rate limits are not reported as rate limits.** Collection Issues shows the raw exception
   text, with no rate-limit label, HTTP status, Retry-After, or count of jobs kept.
3. **A slow source can stall the run.** `collector.py` has no per-source wall-clock bound, and
   `cli.py` writes the archive only after every source finishes. A killed search leaves no
   archive and the later pipeline stages never run. About ten `data/runs/*/manifest.json` files
   are stuck at `stage: search, status: running`. It is **not proven** that a rate-limited source
   caused any of them; the timeout covers the case regardless.
4. **No way to collect slowly in the background** and filter/render independently at any time.

Per-source exception isolation already exists (`_collect_source` catches per source), so this is
not a rewrite of failure handling; it fixes what a failing source leaves behind and how it is
reported.

## 2. Goals and success criteria

- A rate-limited or timed-out source keeps every job it fetched before failing; those jobs are in
  SQLite, and the run continues to review and radar with pipeline status `partial`.
- The final radar names each rate-limited / timed-out company, the reason, and how many jobs were
  kept; the previously stored jobs for that source still appear.
- No partial run ever closes a job it merely did not reach.
- Collection can run in the background, slowly, while `refilter`/`review`/`render` run at any
  time against whatever has been stored so far.
- Unconverted adapters behave exactly as today.

Non-goals: scheduling (project rule: manual collector, no cron/daemon restart); a SIGTERM
handler that writes a partial archive when the whole process is killed (the per-source timeout
makes this much less likely; residual risk accepted); timing out the detail-fetch phase (detail
failures are already per-job and fail-soft); an end-of-run retry pass for rate-limited sources;
moving scoring into Python.

## 3. Constraints carried in

- Python owns networking, persistence, health, filtering; the skill owns scoring.
- Prefer false negatives: partial results must never cause `mark_missing` to close jobs.
- Adapters fail loudly rather than guess; no credentials/browser tricks added.
- Assessment cache keys on `content_hash` only (untouched).
- Safe testing: tests use fixtures and temp projects; never write the real archive/radar.

## 4. Phase 1 — rate-limit/partial-results core

### 4.1 Error model (`adapters/base.py`, `models.py`)

- `RateLimitError(AdapterError)` carrying `http_status: int | None`, `url: str`,
  `retry_after_seconds: float | None`. `JobAdapter.request()` raises it where it today raises on
  the final retry of a 429 or a WAF challenge. Other terminal 5xx/network errors are unchanged.
- `SourceHealth` gains optional `failure_kind: Literal["rate_limited", "timeout"] | None`,
  `http_status: int | None`, `retry_after_seconds: float | None`. `HealthStatus` is **unchanged**
  (decision: reuse `warning` rather than add a `partial` status; old archives stay valid).
- `JobAdapter` gains `self.kept: list[JobSummary]` and `keep(batch)`, which extends it (used by
  Apple, per page). Most paginating adapters instead call `jobs = self.begin_listing()`, which
  returns a list the base registers (a fresh one per `fetch_summaries()` call), so their existing
  `.append`/`.extend` calls feed `adapter.kept` with no loop surgery. Not registering is valid and
  means "nothing to salvage".

### 4.2 Collector (`collector.py`, `config.py`)

- `fetch_summaries()` runs under `asyncio.timeout(collection.source_timeout_seconds)` (new
  `CollectionConfig` field, default `1200`, `None` disables; `gt=0`, so no `0`). The bound covers the listing
  phase only.
- On `RateLimitError`, timeout, or any other exception, the collector takes `adapter.kept`:
  - **non-empty:** deduplicate by `job_id`, run the normal detail/location/sponsorship/salary/
    upsert path on them, record `status=warning` with `failure_kind` (`rate_limited` for
    `RateLimitError`, `timeout` for timeout; `None` for other exceptions, whose partials are
    still kept) and message `"<reason> after N jobs; remainder not collected, will retry next
    run"`. `mark_missing` is **not** called (it already only runs for `OK`).
  - **empty:** `status=failed` exactly as today, but `failure_kind`/`http_status`/
    `retry_after_seconds` are populated when known.
- A partial or failed run never updates a source's stored non-zero count baseline
  (`last_nonzero_job_count`, the guard against zero-job runs). Only `status=ok` runs with
  `job_count > 0` advance it, so a partial run cannot hide a later real drop. `update_health`
  is adjusted accordingly.
- `sources_succeeded` already counts `warning`, so `Collector.search()` returns
  `partial_failure` summaries and the CLI/pipeline continue to review and radar.

### 4.3 Reporting (`scripts/render_radar.py`, `pipeline.py`, `cli.py`)

- Collection Issues rows render a "Rate limited" / "Timed out" badge from `failure_kind`, the
  reason (HTTP status, Retry-After when present), and "kept N jobs".
- `_apply_collection_fallback` widens from `status == "failed"` to
  `failed` OR (`warning` AND `failure_kind` set). It merges the DB's earlier active/eligible jobs
  for the source (the partial run's own jobs already in the DB are included), and
  `fallback_provenance` records keep their structured shape. Count-drop `warning`s (no
  `failure_kind`) keep today's no-fallback behavior; the docstring's scope rationale is updated.
- The radar's `--result-json` lists sources with a `failure_kind` as `rate_limited_sources`; the
  pipeline derives the manifest field and `partial` status from it in the radar stage (live and
  `--no-scrape` alike). `source-status` is unchanged: `failure_kind` is not stored in SQLite (§6),
  so `collect status` and the radar show it.

### 4.4 Adapter rollout

Core ships first with no adapter converted (behavior unchanged except timeout/reporting). Then
convert the paginating adapters (Apple, `html_paginated`, `html_multi_index`, Workday, Oracle
HCM, Eightfold, Phenom, `json_api`, `adp_recruiting`, `csod`, `successfactors_rmk(_v2)`,
SmartRecruiters, UltiPro, Paycom, Paylocity, Dayforce, `icims_attract`, `zf`, `bosch`, `brose`)
by registering their listings (`begin_listing()` or one `keep(...)` per page). Behavior tests ("a 429
on page N keeps pages 1..N-1") cover html_paginated, html_multi_index, oracle_hcm, smartrecruiters,
adp_recruiting, eightfold, bosch, successfactors_rmk_v2 and ultipro-style recipes where an existing
pagination test supplies the fixture shape; csod, dayforce, icims_attract, phenom, paycom and
workday have no pagination fixture, so they get a static registration guard plus the shared
contract test only. `stealth_html` is last and optional.

## 5. Phase 2 — background collector

### 5.1 Runner (`src/job_hunter/background.py`, `cli.py`)

- `job-hunter collect start [--companies A,B] [--slow]` launches a detached process
  (`Popen(start_new_session=True)`, same pattern as `pipeline.py`) and returns immediately.
  `collect status [--json]` and `collect stop` read/signal it. Rejects starting when the
  `collector` lock is held (clear message naming the holder's PID).
- State file `data/collect/state.json`, rewritten atomically (`atomic_write_text`) after every
  source transition: `run_id`, `pid`, `pid_start_time`, `status`
  (`running|complete|stopped|failed|abandoned`), `started_at`, `updated_at`, `companies` scope,
  and per source `{status: pending|running|ok|warning|failed|unsupported, job_count,
  failure_kind, http_status, retry_after_seconds, message, finished_at}`. `abandoned` is derived
  by `collect status` using the same PID + start-time liveness check as `pipeline-status`.
- The runner reuses `Collector`'s per-source path (`_collect_source`), so Phase 1 behavior
  (partial keeping, timeouts, health) applies unchanged. `collect stop` is cooperative: sources not
  yet started are skipped, in-flight sources finish and persist, then the runner marks `stopped`
  and exits.

### 5.2 Independence from filter/render

- The runner **never writes an archive mid-run** (avoids it and `refilter_archive.py`'s
  in-place rewrite clobbering each other).
- `job-hunter snapshot` materializes
  a normal archive at `archive_path()` from SQLite: source scope and `source_health` come from
  `state.json`'s finished sources (ok/warning counted as scope, per
  `refilter_archive._successful_source_scope` semantics), candidates from the existing refilter
  logic. It prints the exact `pipeline --no-scrape --search <path>` command to run next; review and
  radar then run as today. It may be run at any time, repeatedly; each
  snapshot reflects whatever is stored then, and each rate-limited/timed-out source appears in
  its `source_health` with `failure_kind`.
- On completion the runner makes a final snapshot the same way.

### 5.3 Locking (`runlock.py`, `cleanup.py`)

- The runner holds `run_lock("collector")` for its lifetime; it does **not** hold
  `run_lock("job-hunter")`, so refilter/review/render remain available. SQLite WAL plus the
  existing 5 s `busy_timeout` cover concurrent reads during per-source writes.
- `cleanup --apply` refuses while the `collector` lock is held. Foreground `search` and
  `pipeline` are unchanged.

### 5.4 Slow mode (`config.py`, `settings.yaml`)

- `collection.background` block: `max_concurrent_sources` (default 1), `source_delay_seconds`
  (default 30, delay between starting consecutive sources). `--slow` selects it; without it the
  runner uses the normal `collection` settings. Documented in `config/settings.yaml` comments.

## 6. Data/compat

- `SourceHealth` new fields are optional: old archives load unchanged; old readers ignore them.
- No SQLite schema change in Phase 1 beyond the already-added `last_nonzero_job_count`; the
  reporting fields live in the archive/state file, not the DB.
- Docs updated: `docs/SPEC.md` (collection failure semantics, §12, new collector section),
  `CLAUDE.md` (collector, health, radar fallback, new commands), `README`. A `skills/*/SKILL.md`
  that documents `collect`/`snapshot` or changed pipeline output must bump `version`.

## 7. Testing (test-first)

Phase 1: `request()` raises `RateLimitError` on terminal 429 and WAF; collector keeps `kept`
jobs on `RateLimitError`/timeout/other exceptions, records `warning`+`failure_kind`, skips
`mark_missing`, leaves the baseline untouched; empty `kept` stays `failed` with `failure_kind`;
radar badge text, widened fallback (and the unchanged count-drop case); pipeline continues and
ends `partial`; per-adapter "429 on page N keeps pages 1..N-1" tests; `keep()` contract test.
Phase 2: runner state transitions with fake adapters; snapshot mid-run contains only finished
sources and survives a second snapshot; `collect start` refused under a held lock; `cleanup
--apply` refused under the collector lock; `collect stop`; abandoned detection; slow-mode
concurrency/delay. All with temp projects, fixtures, no network.

## 8. Risks and open points

- Partial listings are not whole listings: a kept page-1 set under-represents the source until
  the next full run (accepted; reported explicitly as "kept N jobs").
- A source that rate-limits on every run stays `warning` indefinitely; visible in Collection
  Issues and `collect status`, not auto-escalated.
- Because only `ok` runs advance the baseline (§4.2), a source that genuinely shrinks by more
  than 70% raises the count-drop warning on every run until its count recovers, because a
  flagged run is `warning` and so never becomes the new baseline. This is deliberate (a false
  alarm is cheaper than a hidden loss) and is the one behavior change to `update_health` outside
  the partial path. **Open point for review:** if permanent warnings for genuinely shrunk
  sources prove noisy, add an explicit acknowledge/reset command rather than silent decay.
- The stuck-run history is unexplained; Phase 1's timeout is a mitigation, not a diagnosis.
