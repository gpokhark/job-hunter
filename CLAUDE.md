# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Job Hunter is a manual Python collector for employer career sites. It normalizes
postings, applies a strict U.S.-eligibility filter, persists history in SQLite, and emits a compact
JSON candidate bundle for an LLM agent (`skills/job-hunter/SKILL.md`) to score against a resume.
It does not schedule searches or apply to jobs — collection is config-driven HTTP/HTML fetching,
with one deliberate exception: the `stealth_html` adapter (`src/job_hunter/adapters/stealth_html.py`)
drives a real headless stealth browser (Scrapling) for sources with no plain anonymous endpoint —
either because the site actively bot-blocks (`astemo`: Cloudflare Turnstile) or because its
content simply doesn't exist until client-side JS renders it (`google`).
Before reaching for it against a bot-blocked site, check for an unprotected backend first — GM's
Cloudflare-protected front end looked exactly like Astemo's until a real job link revealed its
real system was a public, unauthenticated Workday API, and it dropped this adapter entirely.
Stellantis was the same story: its Angular front end (`careers.stellantis.com`) was scraped via
`stealth_html`, capped at its first ~10-job page since pagination is JS-`onclick`-only — until a
real job link revealed the actual backing system, ADP Recruiting Management, has a public
two-call handshake (`adp_recruiting.py`: an unauthenticated `myJobsToken` fetch, replayed as a
header on a paginated listing endpoint that returns full descriptions and a posting date inline)
and it dropped `stealth_html` entirely too. Apple was too, once inspecting its plain HTML directly
(not rendering it) showed jobs.apple.com's React Router SPA server-renders every page with a full
JSON snapshot (`apple.py`: `window.__staticRouterHydrationData = JSON.parse("...")`, a
double-encoded string needing one extra unescape before it's parseable JSON) including exact
posting dates and full descriptions — no browser needed despite the prior assumption that its
listing was only reachable as an escaped JSON blob. For
a source where the block genuinely is the only way in, using this adapter is an explicit,
disclosed choice to defeat that site's own anti-automation controls — real ToS exposure, not
solved by "it's just reading public data" — so don't reach for it by default; every other adapter
stays plain httpx, and a new source should too unless a plain endpoint genuinely doesn't exist.
See the README's "The `stealth_html` adapter" section.

The division of responsibility is intentional and load-bearing: **Python owns networking,
normalization, persistence, health, and location filtering; the agent skill owns evidence-based
resume scoring.** Don't move scoring logic into Python or retrieval logic into the skill.

## Commands

```bash
uv sync --dev                          # install runtime + dev deps
uv run job-hunter doctor               # environment/config sanity check
uv run job-hunter search                # run all enabled sources
uv run job-hunter search --json --output data/latest_search.json
uv run job-hunter search --companies honda,toyota --new-only
uv run job-hunter source-status         # per-source health from SQLite
uv run job-hunter source-test honda     # healthcheck one adapter live
uv run job-hunter db-stats
uv run job-hunter export --format json

uv run pytest                           # full suite (fixtures only, no network)
uv run pytest tests/test_adapters.py::test_name   # single test
uv run pytest -m "not live"             # skip tests marked live (network-dependent)
uv run ruff check .
```

Tests use saved response fixtures in `tests/fixtures/` and never hit the network unless marked
`live`. Setup requires `cp config/candidate_profile.example.yaml config/candidate_profile.yaml`
before most commands will find a profile (falls back to the example file otherwise).

## Architecture

**Pipeline:** `cli.py` → `Collector.search()` (`collector.py`) → per-company `JobAdapter` → normalize →
`location.evaluate_location()` → `prefilter.passes_prefilter()` → `Storage` upsert → ranked
`SearchResult` JSON.

- **`config.py`** — loads and validates three YAML files with pydantic models: `config/settings.yaml`
  (collection tuning, DB path), `config/companies.yaml` (one entry per employer, selecting an
  adapter), `config/candidate_profile.yaml` (title/domain terms, exclusions, resume path).
  An `unsupported` adapter entry *must* carry `unsupported_reason` — this is enforced by a model
  validator, not a convention. A company with no verified anonymous endpoint and no viable
  `stealth_html` path stays `unsupported` rather than faking data.

- **`adapters/`** — one class per ATS platform family (`workday.py`, `lever.py`, `oracle_hcm.py`,
  `phenom.py`, `successfactors_rmk.py`, `html_paginated.py`, `html_multi_index.py`,
  `discovered_api.py`, `stealth_html.py`, `adp_recruiting.py`, `apple.py`), registered in `adapters/__init__.py`'s `ADAPTERS` dict and selected by the
  `adapter` key in `companies.yaml`. All inherit `JobAdapter` (`adapters/base.py`), which supplies
  retry-with-backoff HTTP (`request()`, retries on 429/500/502/503/504 plus network/timeout errors,
  honors `Retry-After`) and a default `healthcheck()`. Adapters implement `fetch_summaries()`
  (required) and optionally `fetch_detail()` for per-job description/salary fetches.
  `json_api.py`'s `ConfigurableJsonAdapter` is a generic JSON-listing kernel driven entirely by
  `companies.yaml` config (`list_url`, `items_path`, `fields` mapping); several ATS-specific
  adapters are thin subclasses of it with sensible defaults — its per-item-to-`JobSummary` logic
  is factored into `_items_to_jobs()` specifically so a subclass can add its own pagination loop
  around it (`oracle_hcm.py` does this: `config: {paginate: true, total_path: ...}`, needed
  because Oracle's finder syntax embeds `offset`/`limit` inside one query value and silently caps
  page size well below some sites' full job count). HTML adapters use `selectolax` with
  CSS-selector config (`card_selector`, `link_selector`, etc.) instead of a schema path;
  `posted_at_selector` (parsed via `normalizer.parse_display_date`) covers a per-card visible
  date. `html_paginated.py`'s `fetch_detail` additionally always checks for a schema.org
  JobPosting JSON-LD block (`normalizer.extract_job_posting_ld`) regardless of config — a
  cross-platform SEO convention, not adapter-specific — and uses its `datePosted`/
  `employmentType` without overriding whatever `description_selector` already found.
  `html_multi_index.py` (HRI) adds one more fallback on top, tried only when JSON-LD found
  nothing: HRI's Liferay DDM pages carry a `publicationDate` inside an inline `JobOfferData`
  JS-object literal (`normalizer.parse_liferay_publication_date`), the site's own real publish
  date. This flows into `passes_recency` with no per-company exemption, by explicit choice —
  HRI's currently-listed postings are mostly well past the 30-day cutoff, so this makes most of
  its jobs filter out as stale rather than being kept for lack of a determinable date.
  Use `scripts/endpoint_probe.py` (or curl) to inspect a candidate endpoint before wiring up a new
  adapter config — never hand-invent an endpoint shape. When a site's real content or pagination
  only exists after client-side JS runs (or its query params are silently ignored — several
  "obvious" ones turned out to be, e.g. Honda's `start`/`num` vs. the real `from`/`s`), the
  established technique is to render it *once* with Scrapling (`stealth_html`'s
  `AsyncStealthySession`) to read the real DOM/links it generates, then hardcode whatever was
  discovered as static config — a browser is a one-time discovery tool here, essentially never a
  runtime dependency (see README's "Adding a new source").

- **`collector.py`** — orchestrates one search run: fetches all companies concurrently (bounded by
  `max_concurrent_sources` semaphore), fetches details only when needed (no prior record, prior has
  no description, or `--refresh-details`) — concurrently within a source, bounded by
  `max_concurrent_details` — re-evaluates location using detail data (a detail page can override an
  ambiguous/remote summary location), upserts into `Storage`, computes `SearchSummary`, and returns
  one `SearchResult`. One source's failure never aborts other sources — each source's exceptions are
  caught per-company and turned into a `FAILED`/`UNSUPPORTED` `SourceHealth` entry. The CLI only
  exits non-zero if *all* attempted sources failed. A job whose *listing-level* `posted_at` already
  proves it's older than `max_posting_age_days` skips its detail fetch entirely (`is_recent()`,
  `prefilter.py`) — `passes_recency` only ever looks at the date, so fetching a description for a
  job already known stale is pure waste; this is what actually made large full-catalog sources
  (Apple, Ford, Stellantis) slow. See README's "Performance" section for the full picture, including
  why Apple/Stellantis additionally stop *paginating* early (confirmed sorted newest-first) while
  Ford/DENSO's Oracle HCM listing does not (confirmed *not* reliably date-sorted, so it always
  fetches its full catalog), and the `storage.mark_missing(stale_before=...)` fix that keeps early
  pagination stop from falsely closing jobs it simply stopped looking for.

- **`location.py`** — the U.S.-eligibility gate (`evaluate_location`), returning a
  `LocationDecision` (`us_eligible`, `confidence`, human-readable `evidence` string). Precedence
  matters: structured country/state fields win first, then explicit "remote in the U.S." phrasing,
  then U.S. state name/abbreviation matches, then explicit "United States" text, then a recognized
  non-U.S. country/city list, then bare "remote" with no U.S. evidence is rejected as low-confidence.
  This ordering exists to prevent multi-location postings or ambiguous remote listings from being
  wrongly excluded or wrongly included — read the comments in `evaluate_location` before reordering
  the checks.

- **`prefilter.py`** — `passes_prefilter`'s positive-term gate (the profile's
  `target_title_terms`/`target_domains`, or a `keywords` override — see below) matches only
  against `job.title` + `job.department`, never the free-text `description`. This was a
  deliberate fix, not the original design: matching the full description let a company-wide
  "about us" boilerplate paragraph (e.g. "...from next-generation connectivity and autonomous
  driving technologies...", pasted into every posting regardless of role) or a long list of
  optional "preferred qualifications" bullets inject a target term into a posting with no real
  connection to it — confirmed directly against live postings (a GM RF hardware role passing
  purely because ADAS was one of several optional "or" bullets, and the company boilerplate
  mentioned "autonomous driving"). `department` is kept in the gate because it's curated,
  structured metadata some ATS platforms expose (e.g. Honda's "Autonomous Tech Dev Dep"), not
  marketing prose, so it doesn't share that failure mode and can still catch a genuinely relevant
  but generically-titled role. `exclude_terms` still scans the full `description` — over-excluding
  on a disqualifying phrase found anywhere is low-risk; the danger this fix addresses is only ever
  on the inclusion side. `relevance_score` (ordering only, no gating role) still uses the full
  haystack including description.

  `passes_prefilter` takes an optional `keywords` override (wired to `job-hunter search
  --keyword`): when given, it *replaces* the profile's `target_title_terms`/`target_domains` as
  the positive-match set for that one run, subject to the same title+department scope.
  `exclude_title_terms`/`exclude_terms`/U.S.-eligibility stay in force either way. `passes_recency`
  is a separate, fully deterministic date check (no LLM involved) against
  `settings.search.max_posting_age_days` (default 30) — a job with no discoverable `posted_at` is
  kept rather than excluded, since its age can't be determined.

  `Collector.search()` has no default candidate cap — `--max-candidates` remains available as an
  explicit opt-in one. There used to be an automatic `recommendation.max_results * 3` cap here,
  because the old full-description matching passed ~74% of all U.S.-eligible postings, so
  *something* had to bound what reached the free-but-sequential local-LLM review step; that cap
  then silently discarded most of what it admitted, using `relevance_score` (a coarse
  keyword-count heuristic) as the tiebreaker for what survived — which is how postings with zero
  real domain relevance (matching only on a bare seniority word like "senior"/"staff", or a
  generic word like "validation" picked up from boilerplate) could occupy review slots ahead of
  more relevant matches. Confirmed on live data: the old gate passed ~3,100+ U.S.-eligible jobs by
  default; the title+department-scoped gate passes ~50-150 depending on the day, small enough for
  a full sequential local-LLM review to finish in well under half an hour, so nothing needs to be
  discarded pre-review anymore. Candidates are sorted newest-first (not by `relevance_score`) so an
  interrupted review has already covered the freshest postings; final ranking is always the local
  LLM's own score, applied at the skill's compile step, never Python's.

- **`storage.py`** — SQLite (WAL mode) with four tables: `jobs` (one row per `(source_key,
  job_id)`, upserted with `is_new`/`is_changed` computed from prior content hash), `runs` (one row
  per search invocation), `source_health` (per-source rolling status, consecutive-failure count,
  last success time), and `assessments` (one row per `(source_key, job_id)`, a local model's
  fitness verdict — score, recommended, matches, gaps — written by `scripts/review_with_lm_studio.py`
  via `upsert_assessment()`/`get_valid_assessment()` directly, or manually via the
  `record-assessment` CLI command; never produced by Python itself, which only ever persists a
  verdict handed to it). A job is marked `closed` after 3 consecutive runs
  where it's missing from a healthy source's listing (`mark_missing`); it stays `active` otherwise,
  which is why the tool surfaces previously-seen jobs by default (see `--new-only` vs default
  behavior below). `mark_missing`'s `stale_before` parameter excludes jobs already older than a
  source's own early-pagination-stop cutoff from this accounting — without it, a source that
  deliberately stops looking for postings past the recency window would falsely close every job
  that ages past that window, since it would never see them in its listing again regardless of
  their real status. `Collector.search()` joins `assessments` back onto each candidate as
  `Job.prior_assessment`, but only when the row's stored `content_hash` still matches the job's
  current one — a job whose posting changed since it was assessed is treated as unassessed again,
  never silently served a stale verdict. This is what lets the `job-hunter` skill's per-job
  sub-agent review (see below) skip a job it already scored in a previous run at zero token cost.

- **`health.py`** — `detect_count_anomaly` flags (but does not fail) a source whose job count drops
  more than 70% from its last known count, guarding against adapters that "succeed" against a
  changed page structure while silently returning far fewer/no jobs.

- **`models.py`** — pydantic schema shared across the pipeline: `JobSummary` (listing-page data) →
  `Job` (summary + detail + location decision + dedup metadata) is the full record; `SearchResult`
  is the CLI/skill-facing output envelope.

- **`skills/job-hunter/`** — the agent-facing half of the system. `SKILL.md` is the canonical
  procedure (run the collector, read only `candidates`, never recommend `us_eligible=false`, never
  invent salary/sponsorship/qualifications). Scoring itself is delegated entirely to
  `scripts/review_with_lm_studio.py` — a deterministic script, not a sub-agent — which sends each
  not-yet-assessed candidate to a **local** model via LM Studio's OpenAI-compatible API
  (`config/lm_studio.yaml`, one job at a time, strictly sequential), so no Claude/cloud tokens are
  spent scoring anything; the calling agent's job is just to run it, then read
  `data/assessments.json`/`export-assessments` and present the results. `references/scoring.md`
  defines the rubric embedded into that script's prompt. Install/copy `SKILL.md` for other agent
  runtimes via `scripts/install_skill.sh`.

## Working in this repo

- Adapters and location logic fail loudly (raise `SchemaError`/`AdapterError`) rather than
  guessing or silently returning partial data — preserve that when touching adapter code.
- Don't add credentials or session/CSRF replay for collection. Browser-based stealth fetching is
  allowed *only* via the existing `stealth_html` adapter for a source with no other viable
  anonymous endpoint — it's a deliberate, disclosed exception (see README), not a default; every
  other adapter stays plain httpx, and a new source should too unless one genuinely doesn't exist.
- `--new-only` filters *output*, not collection — collection always observes and persists every
  job returned by a source regardless of CLI flags.
- When adding a company to `companies.yaml`, prefer reusing `ConfigurableJsonAdapter`/`json_api`
  via config over writing a new adapter class unless the platform truly needs bespoke parsing.
