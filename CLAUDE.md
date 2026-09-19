# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Job Hunter is a manual Python collector for employer career sites. It normalizes
postings, applies a strict U.S.-eligibility filter, persists history in SQLite, and emits a compact
JSON candidate bundle for an LLM agent (`skills/job-scout`/`job-reviewer`/`job-radar`, or the
`skills/job-hunter` orchestrator that runs all three) to score against a resume.
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
listing was only reachable as an escaped JSON blob. OpenAI is the same lesson at a platform level:
`openai.com/careers/search` returns a genuine Cloudflare-managed challenge (`cf-mitigated:
challenge`) on a plain request — but that's only the marketing front end. The real backing ATS,
found the same way (a real job link, not a guess), is Ashby, reachable directly and unprotected at
`jobs.ashbyhq.com/openai` — Ashby's own documented public posting API
(`api.ashbyhq.com/posting-api/job-board/openai`) mirrors it exactly, zero-auth, no browser. Its
first guess wasn't free either: `jobs.ashbyhq.com/anthropic` looks like the obvious matching URL
for Anthropic but is a real Ashby "Page not found" — Anthropic isn't on Ashby at all, it's
Greenhouse (`anthropic.com/careers/jobs` links directly to `job-boards.greenhouse.io/anthropic/
jobs/<id>`), confirmed only once a real job link was actually followed. The check isn't only for a
*blocked* front end, either: Roche's `careers.roche.com` is a Phenom People site that works fine
unblocked over plain httpx, but each of its own eager-loaded job records carries an `applyUrl`
pointing at a public, unauthenticated Workday CXS API (`roche.wd3.myworkdayjobs.com`) with the
full global catalog, where the visible Phenom page only ever shows one category at a time — the
working front end was itself the tell that pointed at the better backend, not a reason to stop
looking. For
a source where the block genuinely is the only way in, using this adapter is an explicit,
disclosed choice to defeat that site's own anti-automation controls — real ToS exposure, not
solved by "it's just reading public data" — so don't reach for it by default; every other adapter
stays plain httpx, and a new source should too unless a plain endpoint genuinely doesn't exist.
See `docs/SPEC.md` §5.8 for the full tradeoffs.

A related trap those same public APIs walked straight into: **the endpoint that's good for
scraping is not always a page a human should be handed.** GM's Workday CXS API and Ford/DENSO's
Oracle HCM REST API are both public JSON endpoints — great for `fetch_summaries`/`fetch_detail` —
but opening either directly in a browser just shows a JSON dump, not a job posting. The `url`
field was originally set to that same API endpoint for both, since it happened to work for the
one thing being tested (scraping). Fixed by decoupling the two: `workday.py`'s `fetch_detail`
reconstructs the CXS API url independently (from `summary.raw["externalPath"]`) rather than
reusing `summary.url`, so `public_base_url` (GM/Toyota/Valeo/Nissan) can point at Workday's real
`.../en-US/{site}/` candidate-facing host instead; `json_api.py`'s `_items_to_jobs`/`fetch_detail`
gained an opt-in `public_url_template` (Ford/DENSO) for the same reason, pointed at Oracle's own
`.../hcmUI/CandidateExperience/en/sites/{site}/job/{id}` page — confirmed via curl to render the
correct job, not guessed. Any new JSON-API-backed adapter should ask this question explicitly: is
`url` something a human can actually open, or only something `fetch_detail` can `.json()`?

A 200 response with plausible-looking job cards is not proof a query parameter actually filtered
anything — confirmed the hard way onboarding Molex (koch.avature.net, a shared career portal for
every Koch Industries subsidiary): its default search page renders the same fixed handful of
results over plain httpx no matter what's tried (`?query=`, `?keyword=`, or a facet id passed as a
bare GET param), only ever revealing the real, differently-shaped param it actually reads
(`732_format=...` alongside the facet id) by driving the page once with Playwright and reading the
URL its own form submission redirects to. Once found, it's a plain httpx param again — but "it
returned 200 and the results look real" was never itself sufficient evidence; only two *different*
facet values producing two *different* result sets proved the filter was real. And the opposite
lesson matters too: not every Radancy/TalentBrew-branded site is a skin hiding a different real
backend the way GM's and Stellantis's were — Toro Company's TalentBrew site is genuinely plain,
unprotected, server-rendered HTML with no hidden system underneath, confirmed only by actually
finding real job cards in the raw response rather than assuming the brand name implied a skin.

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

- **`adapters/`** — one class per ATS platform family (`workday.py`, `lever.py`, `ashby.py`,
  `greenhouse.py`, `oracle_hcm.py`,
  `phenom.py`, `successfactors_rmk.py`, `html_paginated.py`, `html_multi_index.py`,
  `discovered_api.py`, `stealth_html.py`, `adp_recruiting.py`, `apple.py`, `eightfold.py`,
  `successfactors_rmk_v2.py`, `bosch.py`, `zf.py`, `csod.py`, `icims_attract.py`, `dayforce.py`,
  `smartrecruiters.py`, `paycom.py`, `paylocity.py`), registered in `adapters/__init__.py`'s `ADAPTERS` dict and selected by the
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
  page size well below some sites' full job count). `ashby.py` (OpenAI) is a bare one-line alias
  like `lever.py` — pure config, no bespoke code — since Ashby's public posting API returns every
  field clean and flat in one request (full `descriptionHtml` inline, no per-job detail fetch;
  structured `address.postalAddress.addressCountry/addressRegion` for the high-confidence branch
  of `evaluate_location`; `workplaceType` maps straight onto `work_arrangement` via
  `json_api.py`'s generic opt-in `fields.work_arrangement`, added for this — any
  `ConfigurableJsonAdapter` company can now set it the same way, not just Ashby).
  `greenhouse.py` (Anthropic) needed one thing Ashby didn't: its own `content` field comes back
  HTML-entity-double-encoded (confirmed live: literally `&lt;div class=&quot;...&quot;&gt;`, not
  `<div class="...">`) — `GreenhouseAdapter.fetch_detail` unescapes it once, centrally, the same
  double-encoding shape `apple.py` handles for an unrelated reason. HTML adapters use `selectolax` with
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
  runtime dependency (see `docs/SPEC.md` §5.20, "Adding a new source"). Two more lessons from
  onboarding BMW and Bosch: a "static public frontend key" — a `Bearer` token or similar embedded
  directly in a page's own plain HTML rather than fetched from any login/token endpoint — is the
  same category as Ashby's public posting API key (meant for exactly this client-side use, served
  to every visitor) and safe to reuse in an adapter the same way (`bosch.py`); and when a
  server-rendered detail page reuses one CSS class for several different fields' *values*,
  distinguished only by an adjacent label's text (BMW's `.rtltextaligneligible`, labeled by a
  sibling `.joblayouttoken-label`), match by that label text rather than by CSS position —
  `:nth-of-type` was tried and confirmed unreliable, since it counts a node among *all* siblings of
  its tag, not just siblings sharing its class (`successfactors_rmk_v2.py`'s
  `_parse_job_layout_tokens`). Also: the same underlying ATS platform can wear two unrelated-looking
  templates for different customers (BMW and Volkswagen-Group are both SuccessFactors RMK
  "Job2Web", one server-rendered, one a client-rendered web-component widget over a JSON API) —
  recognize the platform from shared static-asset hosts/paths, not from how the search page looks.

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
  (Apple, Ford, Stellantis) slow. See `docs/SPEC.md` §12 for the full picture, including
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

- **`sponsorship.py`** — `evaluate_sponsorship`, the same deterministic-detection pattern as
  `location.py` but for a posting's explicit stance on visa sponsorship (`Job.visa_sponsorship`:
  `available`/`not_available`/`unmentioned`, plus a `sponsorship_evidence` string). **Purely
  informational — never a filter.** `passes_prefilter` never calls it, and a `not_available`
  posting is scored, ranked, and shown exactly like any other job at its score, just carrying a
  different tag in `render_radar.py`'s output. This was an explicit design choice: a company's
  current sponsorship stance can change, so the pipeline should surface it, not gate on it. Uses a
  curated phrase list, not a bare `"sponsor"` substring match — confirmed directly against live
  postings that `"sponsor"` alone produces real false positives (PACCAR's "sponsor Key-Op program
  participants", Hyundai's "sponsors for training initiatives", Valeo's "our sponsored sports
  hall" and Polish "Sponsorowane... ubezpieczenie"), none visa-related. Descriptions are raw HTML,
  so text is stripped of tags before matching — Nissan's structured `<b>Sponsorship:</b> No` field
  would otherwise silently defeat a plain-text phrase match. Not-available phrases are checked
  before available ones, so a sentence like GM's extremely common "GM DOES NOT PROVIDE
  IMMIGRATION-RELATED SPONSORSHIP" can never be miscounted by a looser "provide sponsorship"
  positive pattern. Defaults to `unmentioned` whenever nothing matches — never guess, exactly
  `location.py`'s philosophy for ambiguous cases.

- **`salary.py`** — `evaluate_salary`, the same evidence-based, never-a-filter pattern as
  `sponsorship.py`, for an explicit pay figure (`Job.salary_min`/`salary_max`/`salary_currency`,
  plus a human-readable `salary_evidence` string shown on `render_radar.py`'s output only when
  present — no placeholder for the rest, same reasoning as sponsorship's "unmentioned carries no
  tag"). Unlike sponsorship's curated phrase list, one regex anchored on a real two-number range
  (`$X` then a `-`/`–`/`—`/`to` then a second number, `$` optional on that second number only) is
  enough — validated directly against live GM, Honda, Ford, and Torc Robotics postings, then
  against the full stored job pool before shipping (~9,500 of ~30,000 jobs matched with no false
  positive found beyond the one pattern below). A bare single dollar figure is deliberately never
  matched on its own — confirmed live as a real false-positive risk the same way bare `"sponsor"`
  was for sponsorship.py: Ford's own benefits boilerplate mentions "Life Insurance of $3,000" and
  "Accidental Death and Dismemberment of $1,500", neither a salary; every real mention found
  (including Ford's own compensation line, `"$72,480-121,440"` — note the second number carries no
  `$` at all) was already two-sided, so requiring a second number costs no real recall. One more
  live-confirmed trap: dozens of Caterpillar/Nissan Workday postings for hourly/union roles render
  an unfilled compensation-template field as a literal `"$0.00 - $0.00"` — not a real range, and
  explicitly excluded rather than surfaced as one. Also unescapes HTML entities before matching
  (`html.unescape`, on top of the usual tag-stripping) — confirmed live on Torc Robotics' Greenhouse
  postings, whose pay range renders as two `<span>` tags separated by a third holding a literal
  `"&mdash;"`, not a real "—" character, invisible to the separator alternatives otherwise.
  `storage.py`'s `reevaluate_salary()` (also `job-hunter reevaluate-salary`) mirrors
  `reevaluate_sponsorship()`: backfills every already-stored job's salary fields from its existing
  description, no network involved — needed the same way, since `upsert_job` only ever sets these
  columns on a fresh successful collection.

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

  `passes_prefilter` also supports two profile fields for click-through feedback-derived
  exclusions (`docs/feedback-exclusion-plan.md`): `soft_exclude_terms` behaves like
  `exclude_terms` but scoped to title+department only (never the description — confirmed on live
  data that description-wide matching here produces real false positives, e.g. Ford's EV
  "vehicle platform architectures" description text vs. Apple's chip-org "platform architecture"
  postings the term was meant to catch), and a match is *overridden* whenever title+department
  also contains one of the separately-curated `strong_relevance_terms` — deliberately not a reuse
  of `target_domains`, since that list already contains the broad terms (`validation`,
  `verification`, `simulation`) that caused the false positives `soft_exclude_terms` exists to
  catch. Both fields empty by default; populated only by human-approved suggestions from
  `scripts/suggest_exclusions.py`, never automatically.

  The actual gating logic lives in `evaluate_prefilter()`, which returns a `PrefilterDecision`
  (`passes`, `rule: PrefilterRule`, `term`, `rescued_by`) instead of a bare bool — mirroring
  `location.py`'s `LocationDecision`/`sponsorship.py`'s `SponsorshipDecision` pattern of a
  structured verdict plus evidence, not just true/false. `passes_prefilter` is now a thin wrapper
  (`.passes`) kept for every existing caller; `evaluate_prefilter` exists so `scripts/
  diff_profile.py` (`docs/profile-diff-plan.md`) can explain *why* a job's candidacy changed
  between two profiles, not just that it did. Short-circuit evaluation means `rule`/`term` name
  the *decisive* check in the fixed precedence order above, not an exhaustive list of every check
  that would also have failed.

- **`scripts/diff_profile.py`** — preview-only tool: compares two `CandidateProfile`s (either two
  saved YAML files via `--before`/`--after`, or the real on-disk profile plus an in-memory
  `--add field:term`/`--remove field:term` patch that's never written back) against every stored,
  `us_eligible`, recency-passing job in SQLite, using `evaluate_prefilter` directly — never an
  approximation of it. Reports four counts (retained/still-excluded/gained/lost), not one
  "unchanged" bucket that would hide which side it's mostly made of, plus a terminal summary and
  an HTML report (`data/profile-diff/{YYYY-MM-DD-T-HH-MM-SS}.html` by default, filename timestamp
  in the device's local timezone — `_report_timestamp`, via bare `.astimezone()` rather than a
  hardcoded zone, so it's correct on whatever machine runs it — for readability; `evaluated_at`
  stays UTC internally but is also converted to local time — via the same `_local()` helper —
  everywhere it's displayed, including inside the report itself and the terminal summary, since
  a raw UTC timestamp was confusing enough in practice to misdate a run by a full day for a
  late-evening U.S. run) with a per-job before/after
  reason and, for changed jobs, any existing assessment score or `job_feedback` label (a lost job
  someone already tagged `relevant`/`okay` is flagged loudly, not folded into the general list).
  Reads via a genuine read-only SQLite connection, not `Storage` (whose `__init__` always runs
  `CREATE TABLE IF NOT EXISTS`/`_migrate()`/`commit()`, harmless but not actually read-only), and
  explicitly maps the `jobs` table's `canonical_url` column to `Job.url` — the one column name
  that doesn't already match a `Job` field. `--keyword` here means exactly what it means in
  `job-hunter search --keyword` (a full replacement of `target_domains`/`target_title_terms`, not
  a narrowing of them) — testing an edit to either field while also passing `--keyword` will
  correctly show zero effect, and the tool says so explicitly rather than leaving that silent.
  Every mode's report also includes a "Profile terms" section — the actual before/after words for
  each filtering field (`+added`/`-removed`/unchanged), computed by `_field_term_diffs`, so a
  reviewer sees exactly which words are driving a Lost/Gained verdict without opening the YAML
  file separately; in the HTML report these render as tags (green `+term`, red struck-through
  `-term`). No `--apply` — see `docs/profile-diff-plan.md` section 7 for why that's out of scope
  for *edits*, not merely deferred — that reasoning is specifically about writing changes into the
  file, which is why it doesn't block the mechanism below (a verbatim file copy, never a
  parse/re-serialize).

  With none of `--before`/`--after`/`--add`/`--remove` given, it runs in **check mode** instead of
  erroring: it diffs the current on-disk profile against a tracked baseline,
  `data/candidate_profile.snapshot.yaml` — a plain-text copy of "the profile as of the last time a
  baseline was accepted," not a parsed/re-dumped one, so it can never be the thing that damages the
  real file's comments/formatting. This is what lets the tool answer "what changed since I last
  looked" regardless of *how* the file changed — a manual hand-edit and a skill-applied suggestion
  both flow through the identical on-disk file, so check mode doesn't need to (and can't) tell them
  apart. Check mode only ever *shows* the diff; it never advances the baseline on its own, by
  design (an earlier version auto-advanced after every run, but that meant a diff you didn't
  actually mean to accept could get silently baked in as the new normal before you'd fully looked
  at it) — advancing requires a separate, explicit `--accept-baseline` run, which keeps the
  snapshot it replaces at `data/candidate_profile.snapshot.prev.yaml` for exactly one level of
  undo via `--rollback-baseline` (a true swap — running it twice in a row is a no-op, not a double
  undo). The very first check-mode run has no snapshot yet, so it bootstraps one from the current
  profile with nothing to compare — there's no diff to have confirmed yet, so that one step alone
  doesn't require `--accept-baseline`. See `skills/job-feedback/SKILL.md` for how this is meant to
  be driven end to end, including the confirmation discipline around it.

- **`scripts/refilter_archive.py`** — a different tool from `diff_profile.py`, answering a
  different question: not "what changed between two profiles" but "what would this *already-
  collected* archived search's candidate list look like if re-run through the *current* profile
  right now" — no network, no adapter/scraper invoked. Rebuilds `candidates` from scratch out of
  SQLite's current `status='active' AND us_eligible=1` job pool (scoped to the same source keys
  the archive's own `source_health` originally attempted, so onboarding a new company later can
  never cause an old keyword archive to silently gain that company's jobs), rather than narrowing
  whatever's already sitting in the archive's `candidates` — that narrowing-in-place design was
  tried first and found to be a one-way ratchet: once a `soft_exclude_terms` edit dropped a job
  from `candidates`, its data was gone from the file, so a *later* loosening edit meant to rescue
  it (a new `strong_relevance_terms` override, a removed `soft_exclude_terms` entry) had nothing
  left to restore. Rewrites the resolved archive file in place by default (`--output` to write
  elsewhere instead), and prints a gained/lost/retained count. Unless `--no-report`, also writes
  an HTML report to `data/profile-diff/archive-{search_stem}-{YYYY-MM-DD-T-HH-MM-SS}.html` (same
  local-timezone `_report_timestamp`/`_local` as `diff_profile.py`, shared via import) — reusing
  `diff_profile.py`'s `_e`/`_fmt_posted_date`/`_job_tags` helpers and its identical
  click-to-feedback JS/export mechanism (`job_feedback` rows from either report are
  indistinguishable to `apply_radar_feedback.py`), but through its own, simpler HTML template
  with no "Profile terms" word-diff section — there's no second profile to diff against here, only
  one on-disk profile evaluated against two different job snapshots (an old archive vs. today's
  live SQLite pool), so `_field_term_diffs` doesn't apply. `--keyword` here means the same full
  positive-term replacement it means everywhere else in this project. As of the
  `docs/pipeline-refilter-stale-source-plan.md` round, its own SQLite query (every active/eligible
  job, optionally source-scoped) no longer lives here privately — it moved to
  `src/job_hunter/active_pool.py`'s `raw_active_jobs()` once `render_radar.py`'s stale-source
  fallback (below) needed the identical query for one source at a time; this module now imports
  that instead of keeping its own copy, a behavior-preserving refactor confirmed by every existing
  test in `tests/test_refilter_archive.py` still passing unchanged.

- **`src/job_hunter/active_pool.py`** — the shared SQLite active/eligible-job-pool query, lifted
  out of `refilter_archive.py` once a second caller needed it. Two functions: `raw_active_jobs()`
  (every active/US-eligible job, optionally scoped to a set of source keys, **no** prefilter/
  recency applied — `refilter_archive.py`'s own gained/lost accounting needs the unfiltered shape
  to distinguish "failed recency" from "failed prefilter for some other reason") and
  `source_jobs()` (one source's active/eligible/prefilter-passing/recency-passing jobs, built on
  `raw_active_jobs()` — what `render_radar.py`'s fallback actually calls). `source_scope` is
  pushed into the SQL `WHERE ... source_key IN (...)` clause rather than fetched-then-filtered in
  Python — this table is the one documented above as 230MB, 98.5% `description` text, and a naive
  fetch-everything-then-discard-most-of-it approach here would mean a full scan per failed source
  in a single render; an empty (but non-`None`) `source_scope` short-circuits before touching
  SQLite at all, since an empty SQL `IN ()` is invalid syntax, not merely slow.

- **`src/job_hunter/pipeline.py`** — `job-hunter pipeline`, a Python-owned command sequencing
  search → review → radar end to end (or, with `--no-scrape`, refilter → optional review → radar,
  entirely offline — see below), writing a durable `data/runs/<run_id>/manifest.json` at every
  stage (`PipelineManifest`/`PipelineStage`/`PipelineStatus` in `models.py`) so an agent or a human
  can poll `job-hunter pipeline-status` instead of re-parsing three separate commands' output. The
  review/radar (and, in `--no-scrape` mode, refilter) stages are still driven via `subprocess`
  against the existing standalone scripts — this module supervises them, it does not reimplement
  their logic, matching the "scoring is delegated entirely to `review_with_lm_studio.py`"
  principle. `--no-scrape` skips the live search stage and instead re-runs `refilter_archive.py`
  against an already-resolved archive (rejected together with `--companies`, since refiltering
  re-evaluates an archive's own already-attempted source scope, not a fresh company selection);
  its `--review` flag defaults review **off**, the opposite default from normal pipeline mode's
  `--skip-review` opt-out — a deliberate, disclosed asymmetry (see
  `docs/pipeline-refilter-stale-source-plan.md` §4.2), not an oversight. `--no-scrape` also takes
  `--search PATH` to refilter an exact archive, bypassing `--keyword`/newest-mtime resolution
  entirely (rejected without `--no-scrape`, same pattern as `--companies`) — added after a live,
  confirmed incident where mtime-based "newest" resolution picked a narrow, recently-refiltered
  archive over a much larger one actually *collected* more recently (every refilter re-stamps
  whatever archive it targets, so a small archive touched more often can permanently outrank a
  big one collected later); `keyword` still applies independently even with `search` given, since
  it's the refilter's own positive-match-term override, not just an archive-selection hint.

  Each stage's own subprocess (`refilter_archive.py`/`review_with_lm_studio.py`/
  `render_radar.py`) is invoked with `--result-json PATH`, and the manifest's
  `reviewed`/`skipped_cached`/`failed`/`gained`/`lost`/`diff_report`/`radar` fields are read back
  from that structured file, **not** parsed from the child's human-readable stdout — a stage that
  exits 0 but leaves no readable result file is treated as `FAILED`, never silently zero-defaulted.
  `settings.pipeline.stage_timeout_seconds` (shipped as `28800`/8h by default in
  `config/settings.yaml`, `None`/unlimited if unset in a config predating this) bounds each stage
  subprocess, launched via `Popen(start_new_session=True)` specifically so a timeout kills the
  *whole process group* (`os.killpg`), not just the direct child — a plain `subprocess.run(...,
  timeout=...)` only ever kills the direct child, which would leave any grandchild process it
  spawned still running. A killed stage's partial `stdout`/`stderr` is decoded defensively
  (`_decode_timeout_output`, `errors="replace"`) before being written to the manifest — confirmed
  live that `TimeoutExpired.stdout`/`.stderr` can come back as raw `bytes` even under `text=True`
  on this project's own Python, and a kill landing mid-multi-byte-UTF-8-character used to crash
  `write_manifest()`'s `model_dump_json()` call outright instead of cleanly recording `TIMED_OUT`.
  `status` distinguishes `complete`/`partial`/`failed`/`no_candidates`/`model_unavailable`/
  `lock_held`/`timed_out` (`PIPELINE_NON_SUCCESS_STATUSES` in `models.py` is the canonical
  exit-code-2 set, shared between `pipeline` and `pipeline-status`'s CLI handlers so the contract
  can't drift between "just ran" and "polled later"). The manifest also records `pid` and
  `pid_start_time` (`runlock.process_start_time`, a `ps -o lstart=` shell-out) — `pipeline-status`
  compares both, not just PID liveness, before reporting a stuck `RUNNING` manifest as `abandoned`,
  since a dead process's PID can be reused by an unrelated later process. Records
  `profile_fingerprint`/`resume_fingerprint`/`model` on the manifest for provenance only —
  explicitly never wired into cache invalidation, respecting the content-hash-only
  assessment-cache principle above. The whole function body runs inside one try/except that
  finalizes the manifest as `failed` with the real exception message on any unhandled error before
  re-raising — without this, an archive-resolution failure in `--no-scrape` mode (no archive
  matches the given keyword) left the manifest stuck at `running` forever, confirmed live and
  fixed with a regression test in `test_pipeline.py`. A `RunLockHeld` (see `runlock.py` below) is
  caught as its own `lock_held` status rather than falling into the generic `failed` branch, since
  a concurrent run is an expected, actionable outcome, not an error.

- **`src/job_hunter/rootutil.py`, `atomic.py`, `runlock.py`** — agent-runtime portability
  infrastructure. `atomic.py`/`runlock.py` are genuinely universal — every writer of a
  load-bearing file and every lock-holder in this codebase goes through them, hook scripts
  included. `rootutil.py`'s `--project`/`add_project_argument()` is used by the CLI and every
  **operational** `scripts/*.py` entry point (`apply_radar_feedback.py`, `assessments_to_csv.py`,
  `diff_profile.py`, `refilter_archive.py`, `render_radar.py`, `review_with_lm_studio.py`,
  `suggest_exclusions.py`) — not literally every script in the directory: `endpoint_probe.py`
  (diagnostic), `prototype_tfidf_broad_match.py` (prototype), and `search_to_csv.py` (a pure
  stdin/stdout converter) have no operational `--project` contract, and
  `claude_profile_hook.py`/`hermes_profile_hook.py`/`install_hermes_hook.py` take the project root
  as a positional argument instead, via each runtime's own hook-invocation convention (or
  `install_skill.sh`'s shell wrapper) rather than a `--project` flag — see `docs/SPEC.md`'s
  `--project` section for the authoritative, re-verifiable list (`grep -L add_project_argument
  scripts/*.py`).

  `--project`/`$JOB_HUNTER_ROOT` resolve and `chdir` into the real project root
  once, early, before any relative config/data path is touched (`git -C <path>` semantics) — every
  config/data default in this codebase is a bare relative `Path`, resolved against whatever the
  process's CWD happens to be, so this is the single choke point that makes a command
  location-independent instead of requiring "already `cd`'d into the repo." Registered on *every*
  subparser, not just the root one — argparse only sees a flag if it appears in the right parser
  for its position, so `job-hunter <command> --project X` (the position every skill's example
  command actually uses) needs the subparser to declare it too, not just `job-hunter --project X
  <command>`; confirmed live as a real, initially-shipped bug before this was fixed. `atomic.py`'s
  `atomic_write_text()` (temp file + `os.replace()`) backs every load-bearing JSON/HTML write —
  `assessments.json`, search archives, radar/profile-diff reports, the profile baseline snapshots
  — so a killed/interrupted process or two writers racing the same path never leaves a truncated
  file behind. `runlock.py`'s `run_lock()` is a per-project file lock (PID-based) shared under one
  name, `"job-hunter"`, across every operation that mutates SQLite, an archive file, or
  `assessments.json` outside of a single already-serialized review call: `job-hunter pipeline`
  (held for the *whole* run, not just its review stage), `job-hunter cleanup --apply`,
  `scripts/review_with_lm_studio.py`, and `scripts/refilter_archive.py`'s in-place rewrite — one
  lock name means none of them can ever race each other, confirmed live with a direct
  concurrency repro (a second `pipeline`/`cleanup --apply`/standalone-review run against a held
  lock is refused with a clear `RunLockHeld` naming the holder's PID/command/start time, not
  silently raced). Stale-lock reclaim (dead-PID lock file) retries the unlink-then-recreate
  sequence rather than assuming a single attempt always wins, and separately retries a *read* of a
  freshly-created lock file for up to ~200ms before concluding it's stale — a lock file is briefly
  empty between its holder's `O_CREAT|O_EXCL` create and the following write, and a reader landing
  in that exact window used to misread "not written yet" as "abandoned" and wrongly reclaim a lock
  someone else had just acquired, confirmed live via a real multi-process race that intermittently
  produced multiple simultaneous "holders" of the same lock before this hardening. Because
  `pipeline.py` already holds this lock for the whole run before spawning
  `review_with_lm_studio.py`/`refilter_archive.py` as stage subprocesses, and both of those
  scripts otherwise try to acquire the identically-named lock themselves, they would deadlock
  against their own parent — confirmed live as an immediate, reproducible failure the first time
  this sharing was wired up. Fixed via `run_lock_or_inherited()`: `pipeline.py` generates a
  per-acquisition capability token (`secrets.token_hex(16)`, written as a line in the lock file
  itself) and passes it to a spawned stage subprocess via the `JOB_HUNTER_LOCK_INHERITED` env var;
  `run_lock_or_inherited()` only treats the lock as inherited when that env var's value matches
  `current_lock_token()`'s live read of the lock file's *current* token, falling back to a real
  acquire on any mismatch, missing lock file, or unset env var (fails closed) — a bare boolean
  flag was tried first and rejected once the review found that any standalone caller could set the
  same env var themselves and skip the lock entirely with no verification a real parent held it.

- **`src/job_hunter/hook_adapter.py`** — the shared logic behind the Claude Code
  (`scripts/claude_profile_hook.py`) and Hermes (`scripts/hermes_profile_hook.py`)
  candidate-profile-diff hooks: `should_run_diff()` (does an edited path, as a runtime reports it,
  resolve to *this project's* `config/candidate_profile.yaml`) and `run_diff()` (actually run
  `scripts/diff_profile.py`, check mode). Each runtime's own adapter script owns only its stdin
  JSON wire shape (Claude's `PostToolUse`: `tool_input.file_path`; Hermes's `post_tool_call`:
  `tool_input.path`) and calls these two shared functions — no argparse/stdin concerns live here,
  which is what makes it directly unit-testable without spawning a subprocess or faking stdin.
  `run_diff()` discovers `uv` via `shutil.which` rather than assuming it's on `PATH`, and logs
  every failure mode (`uv` missing, non-zero exit, timeout, or a skip — see below) to
  `logs/profile-hook.log` instead of swallowing it silently the way the previous Hermes-only
  hook's bare `contextlib.suppress(...)` did — a hook that fails invisibly is worse than one
  that's merely advisory, since nothing else in this pipeline would ever hint an edit-triggered
  report quietly stopped updating. `run_diff()` also guards against a burst of rapid edits
  starting several overlapping `diff_profile.py` runs (whose final written report used to depend
  on whichever process happened to finish last, not necessarily the most recent edit): a
  non-blocking `run_lock("profile-hook", ...)` skips this run entirely if another hook invocation
  for the project is already mid-run, and a 5-second debounce window (a small marker file,
  `logs/.profile-hook-last-run`) skips a run that started too soon after the last one — both
  best-effort and logged on every skip, never silent, matching this hook's own
  "stale-but-visible beats silently lost" philosophy; `HOOK_LOCK_NAME` (`"profile-hook"`) is
  deliberately its own lock, separate from `runlock.py`'s shared `"job-hunter"` pipeline lock
  above, since this hook only ever reads SQLite and rewrites the profile-diff snapshot/report —
  a disjoint concern from `pipeline`/`cleanup --apply` that shouldn't block or be blocked by
  either. `.claude/settings.json`'s `PostToolUse` command now invokes `scripts/run_profile_hook.sh`
  (a portable POSIX-`sh` launcher) rather than a bare `uv run python ...` — the direct form used
  to fail at the shell level if `uv` wasn't resolvable on the *invoking* process's `PATH`, before
  `hook_adapter.py`'s own `shutil.which("uv")` check (which exists to diagnose the *inner* `uv
  run` call that runs `diff_profile.py`) ever got a chance to run; the launcher locates `uv`
  itself, falls back to a bare `python3` with a clear stderr note if only that's available, and
  always exits 0 either way, since a hook is advisory by design.

- **`storage.py`** — SQLite (WAL mode) with five tables: `jobs` (one row per `(source_key,
  job_id)`, upserted with `is_new`/`is_changed` computed from prior content hash), `runs` (one row
  per search invocation), `source_health` (per-source rolling status, consecutive-failure count,
  last success time), `assessments` (one row per `(source_key, job_id)`, a local model's
  fitness verdict — score, recommended, matches, gaps — written by `scripts/review_with_lm_studio.py`
  via `upsert_assessment()`/`get_valid_assessment()` directly, or manually via the
  `record-assessment` CLI command; never produced by Python itself, which only ever persists a
  verdict handed to it), and `job_feedback` (one row per `(source_key, job_id)`, a human's
  click-through "relevant"/"okay"/"irrelevant" verdict from a rendered radar report — see
  `docs/feedback-exclusion-plan.md`. As of 2026-09-06, `scripts/diff_profile.py`'s HTML report
  carries the identical feedback buttons and exports the identical `radar-feedback-*.json` shape,
  so a label can come from either report — `apply_radar_feedback.py`/`suggest_exclusions.py`
  never know or need to know which one it came from. Upserted, not appended: a later label for the
  same job replaces the earlier one, since a reviewer correcting an earlier click must land on one
  current row, not accumulate contradictory history. Written only by
  `scripts/apply_radar_feedback.py` from an exported feedback JSON — with no `--file`, it
  auto-resolves the newest `radar-feedback-*.json` in `~/Downloads` (always printing which file
  and its mtime, so an auto-pick is never silently the wrong one) and exits cleanly if none
  exists, so it's always safe to invoke unconditionally; `job-hunter export-feedback` mirrors
  `export-assessments` for read-only inspection (dumps the table, writes `data/job_feedback.json`).
  Read only by `scripts/suggest_exclusions.py` — `prefilter.py` never reads this table directly,
  only whatever a human approved into `candidate_profile.yaml` from its suggestions. That script
  re-evaluates every feedback-tagged job against the *current* profile via the real
  `evaluate_prefilter` (not a re-derived guess) to route a suggestion to whichever of all six
  filtering fields the job's current pass/fail reason implicates — not just `soft_exclude_terms`,
  the only field it originally covered; see `docs/feedback-exclusion-plan.md` §13. A job is marked `closed` after 3 consecutive runs
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
  None of these tables ever delete rows on their own — confirmed live (2026-09-07): 230 MB after 9
  days of use, 98.5% of it `jobs.description`, with already-`closed` rows kept forever. `job-hunter
  cleanup` (`cleanup.py`, opt-in, dry-run by default — docs/retention-cleanup-plan.md) is the
  deliberate answer, and reuses `last_seen_at` as a free "days since closed" clock rather than
  adding a new column, since `mark_missing()` never touches it when flipping a job to `closed` —
  it already means exactly "last confirmed present." One real gotcha worth remembering for any
  future storage cleanup: SQLite's `DELETE` only frees pages for internal reuse, it does **not**
  shrink the file on disk — an explicit `VACUUM` afterward is required to actually reclaim space,
  easy to forget when the whole point was reducing disk usage. Sets `PRAGMA busy_timeout = 5000`
  on every connection — two job-hunter/agent processes legitimately hitting the same file at once
  (a search run and a concurrent review run, say) now wait briefly on lock contention instead of
  failing immediately with "database is locked." Schema evolution is tracked via SQLite's own
  built-in `PRAGMA user_version` integer against a numbered `_MIGRATIONS` list (each entry a
  version `N-1`→`N` function) rather than the ad-hoc `PRAGMA table_info(jobs)`-plus-conditional-
  `ALTER TABLE` checks this used to be — a mechanism refactor, not a behavior change: the two
  pre-existing checks (adding `visa_sponsorship`/`sponsorship_evidence`/`salary_evidence`) became
  the first migration entries, so an existing database's `user_version` starts accurately
  reflecting which already-shipped schema changes it's actually seen, not just future ones.
  `_migrate()` applies every entry the connection's own `user_version` hasn't seen yet, then
  advances `user_version` to `len(_MIGRATIONS)` — idempotent by construction, re-running it against
  an already-current database is a no-op.

- **`health.py`** — `detect_count_anomaly` flags (but does not fail) a source whose job count drops
  more than 70% from its last known count, guarding against adapters that "succeed" against a
  changed page structure while silently returning far fewer/no jobs.

- **`models.py`** — pydantic schema shared across the pipeline: `JobSummary` (listing-page data) →
  `Job` (summary + detail + location decision + dedup metadata) is the full record; `SearchResult`
  is the CLI/skill-facing output envelope.

- **`skills/`** — the agent-facing half of the system, split into six independently-invocable
  skills (see `docs/skill-split-plan.md` for the full design rationale): `job-scout` (search →
  archive), `job-reviewer` (local-LLM scoring), `job-radar` (compile + render), `job-feedback`
  (turn radar feedback and/or a `candidate_profile.yaml` change — manual or suggested — into a
  confirmed profile update via `diff_profile.py`'s check mode; see that section above),
  `job-hunter` (the orchestrator — see below), and `onboard-source` (a repo-maintenance skill for
  extending `job-hunter` itself with a new employer source, not an end-user job-search skill;
  moved from `.claude/skills`-only to `skills/onboard-source` — `.claude/skills/onboard-source` is
  now a symlink to it — so it installs for every runtime, Hermes included). Each stage's
  `SKILL.md` is the canonical procedure for its own stage: run the collector, read only
  `candidates`, never recommend `us_eligible=false`, never invent salary/sponsorship/
  qualifications. Scoring itself is delegated entirely to `scripts/review_with_lm_studio.py` — a
  deterministic script, not a sub-agent — which sends each not-yet-assessed candidate to a
  **local** model via LM Studio's OpenAI-compatible API (`config/lm_studio.yaml`, one job at a
  time, strictly sequential), so no Claude/cloud tokens are spent scoring anything; the calling
  agent's job is just to run it, then read `data/assessments.json`/`export-assessments` and
  present the results. It persists each verdict immediately inside its loop, so an interrupted run
  is already resumable by re-invoking it with the same `--keyword`/`--input` — no separate resume
  logic needed. `job-reviewer/references/scoring.md` defines the rubric embedded into that
  script's prompt; `job-scout/references/troubleshooting.md` covers source-health diagnosis.
  `job-feedback` is deliberately the one skill whose own `SKILL.md` mandates two explicit
  stop-and-confirm points with the user — which suggested terms to write into
  `candidate_profile.yaml`, and whether to accept a shown diff as the new baseline — never
  inferred from silence or applied because a prior step wasn't rejected; every number it reports
  still comes from a deterministic script, never an LLM judgment. `job-hunter` is now a genuinely
  thin wrapper around `job-hunter pipeline`/`pipeline-status` (`pipeline.py`, above) rather than
  three separately-sequenced commands it used to inline in full — it documents `--no-scrape
  [--review]` for "I edited the profile, show me the report reflecting that, no new scrape"
  without re-explaining `job-radar`'s tiering/tagging rules or its own disclaimer text, citing
  `job-radar`'s `SKILL.md` by step number instead of restating them, once that duplication was
  identified and removed. Every skill's frontmatter now carries `compatibility`/`metadata`
  (`metadata.hermes.tags` for Hermes-side discovery; no `license:` field — this project is
  deliberately unlicensed) and a `## Contract` section (Input/Output, stated once per skill)
  alongside the pre-existing `name`/`description`/`version` — see
  `docs/skill-frontmatter-and-hook-plan.md`. A skill's `version` must bump on any content change
  (see "Working in this repo" below). Install/copy all six skills for other agent runtimes via
  `scripts/install_skill.sh`, which also now supports `--update` (replace a stale symlink/copy,
  e.g. after a moved repo), `--uninstall`, `--dry-run`, and an explicit `--link` (named alongside
  the pre-existing `--copy`, rather than being only "the default when `--copy` isn't given").
  `--uninstall`/`--update` both used to `rm -rf` any pre-existing destination unconditionally, with
  no check that this installer actually put it there — a manually-placed directory, or someone
  else's content at the same conventional path, would be silently deleted. Both are now gated on
  an ownership check: a plain-text marker, `<destination's parent dir>/.job-hunter-installed`
  (one installed basename per line — no JSON/parser dependency, matching this script's POSIX-`sh`-
  only toolset), written after every real install/update; a destination with no marker yet (an
  install made before this existed) is still recognized as owned via the same up-to-date/
  stale-symlink-by-basename signal the function already computes for its own `OK`/`STALE`
  reporting, so an already-live pre-marker install — this repo's own real `.claude/skills/*`
  symlinks, concretely — keeps working without needing to be "reclaimed." A destination matching
  neither signal is refused (`REFUSED ... (not installed by this tool; re-run with --force to
  remove/replace anyway)`) unless the new `--force` flag is passed. Caught a real bug in this
  fix itself before it shipped: the up-to-date branch's marker backfill was originally
  unconditional, so a plain `--dry-run` against this repo's own real `.claude/skills/` actually
  wrote the marker file despite `--dry-run` promising to touch nothing — fixed by gating the
  backfill on `! dry_run`, covered by a dedicated regression test.

  `job-hunter search --archive` writes each run's candidate bundle to
  `data/searches/{slug}_{date}.json` (`search_archive.py`'s `archive_path()`) instead of one fixed
  `data/latest_search.json` that every run overwrote — the same keyword (or "default", without
  one) on the same day overwrites its own file, but a different day or keyword gets its own, so an
  earlier run's exact candidate snapshot survives a later, unrelated search, and stays reachable
  later via `search_archive.py`'s `resolve_search_path()` (also exposed as `job-hunter
  resolve-search --keyword ...`) — resolving "which archive" by globbing the directory's own
  deterministic filenames rather than maintaining a separate pointer file that could drift.
  `archive_path()` also folds `--companies` into the filename whenever it actually restricts the
  run (`default__companies-openai_{date}.json`, company keys sorted before slugifying so order
  never creates a spurious second file) — added after a real, confirmed-live incident where a
  `--companies`-scoped `job-hunter pipeline` run silently overwrote a same-day 65-source archive
  and its radar report, since the filename previously encoded only `keyword`, never `--companies`.
  Omitting `--companies` (the overwhelming majority of real runs) leaves the filename exactly as
  before.

  `resolve_search_path()`'s "newest" fallback (neither `--search` nor `--keyword` given) is raw
  filesystem mtime — and every refilter re-stamps whatever archive it targets, so a narrow archive
  refiltered more recently can permanently outrank a much larger one actually *collected* more
  recently. Confirmed live as a real, user-facing gap: `job-hunter pipeline --no-scrape` (no
  `--keyword`) silently resolved a 1-company archive over a genuine 43-company sweep collected two
  days earlier, purely because the small one had been refiltered (and thus mtime-touched) more
  recently. Rather than redesign mtime-based "newest" semantics itself (a larger, separate
  decision), `pipeline --no-scrape` gained its own `--search PATH` — see the `pipeline.py` entry
  above — as an escape hatch for a caller who already knows the exact archive;
  `refilter_archive.py`/`render_radar.py` already had this, only the orchestrator's CLI surface
  was missing it. For a caller who *doesn't* already know, `job-hunter resolve-search` now also
  prints a stderr-only scope summary after resolving — `scope: N sources attempted (top 5 by job
  count, ... +M more)`, read from the resolved archive's own `source_health` — so a mismatch like
  the one above is visible at resolution time instead of only discoverable by opening the
  archive's raw JSON by hand; stdout's bare-resolved-path contract (relied on by any scripted
  `$(job-hunter resolve-search ...)` caller) is unchanged. Mtime-based resolution itself remains a
  known, deliberately out-of-scope gap for a default no-args/no-keyword invocation that doesn't
  already know which archive it wants.

  `data/assessments.json` / the SQLite `assessments` table stay deliberately **global**, never
  split per keyword or per run: a job's fitness verdict is a property of *(job, resume)*, not of
  whichever search happened to surface it, and splitting it would mean re-reviewing the same job
  from scratch every time a different keyword happens to match it too — real wasted local-model
  time for a score that can't legitimately differ. Cache validity is keyed on the job's
  `content_hash` only, never on `resume_path` — updating the resume never forces re-review of
  already-assessed jobs (by design), while any job actually sent to the model is always scored
  against whatever resume is on disk at that moment. `scripts/render_radar.py` is the read-time
  join between the two: given one archived search file plus the (global) assessments, it renders
  the grouped/tagged HTML report described in `job-radar/SKILL.md` — Strong (≥75), For-review
  (50–74), and Below-50 sections, a five-step score-color gradient across the whole 50-100 range
  (`render_radar.py`'s `_tier`) rather than a hard cutoff only at 80/90, plus a `[New]` tag — reusing
  `scripts/templates/radar_template.html`, to `data/radar/{slug}_{date}.html`. It is pure
  presentation: it never re-derives, adjusts, or overrides a score. Every candidate the model
  actually scored appears in one of the three sections, however low the score — only a candidate
  the review step skipped entirely (an LM Studio error, or an explicit `--limit`) has no verdict
  to show, so it's counted in `never_reviewed` but never listed. One deliberate, disclosed
  exception to "pure presentation": it also implements the stale-source-collection fallback
  (`docs/pipeline-refilter-stale-source-plan.md` §4.3) — a source whose live collection genuinely
  `failed` this run (never `warning`, which already produced real live data this run just fewer
  jobs than expected, and never `unsupported`, which never has cached data to fall back to) still
  has its last-known-good jobs sitting in SQLite, untouched by that failure. `build()` merges that
  source's current active/eligible/prefilter-passing/recency-passing jobs
  (`active_pool.source_jobs()`, above) into the same candidate pool it already renders, extending
  that source's Collection Issues row with a note naming how many jobs came from the fallback and
  when they were last actually collected (or, if the source has never once succeeded, that no
  prior data exists to fall back on) — merged jobs get no special per-row badge, by deliberate
  choice, the note is the only signal. This is why `build()` takes a `database_path` for the first
  time; the archive file on disk is never rewritten by it, only the rendered HTML, so re-running
  against the same archive stays idempotent. `--no-collection-fallback` (default: fallback on)
  restores the old note-with-no-jobs behavior. Deliberately scoped to `render_radar.py` alone, not
  the live collector (`collector.py`) — the collector's own meaning ("jobs I actually fetched this
  run") stays simple and untouched; a report-layer merge was the smaller, more contained change
  for what was actually asked (the report shows old data, clearly marked), not a live-collection
  behavior change.

## Working in this repo

- **Any filtering mechanism must prefer false negatives over false positives.** An exclude term,
  a soft-exclude, a future scoring/ranking gate — whatever the mechanism, judge it first by
  whether it could ever wrongly reject a genuinely relevant/okay posting, and treat that as the
  risk to eliminate, not merely reduce. A false negative (an irrelevant job slips through) costs a
  little wasted local-LLM review time, visibly, and is cheap to correct next round. A false
  positive (a real match gets silently filtered) costs the job itself, invisibly, with nothing in
  any report to reveal it happened. When a choice must be made between the two — e.g. a soft-exclude
  override term specific enough to avoid rescuing most irrelevant postings but broad enough that
  one specific irrelevant posting keeps resurfacing anyway — always resolve it in favor of fewer
  false positives, even at the cost of more false negatives. See `docs/feedback-exclusion-plan.md`
  for a concrete worked example of this tradeoff being made deliberately.
- **Assessment cache validity is keyed on the job's `content_hash` only — never on `resume_path`,
  model, or rubric — by design.** Updating your resume, switching evaluation models, or tweaking
  the scoring rubric must never force re-review of every already-assessed job; only a job whose
  own posting actually changed (a new `content_hash`) should trigger a fresh local-LLM call. This
  is the same category of tradeoff as the false-negative/false-positive principle above: a little
  staleness (an old cached verdict not reflecting your newest resume until that job's content
  changes again) is cheap and visible — rerun with `--force` whenever you actually want a full
  re-review — while forcing a blanket re-review on every resume/model tweak would burn real,
  sequential local-model time re-scoring hundreds of jobs that didn't change, for no gain on most
  of them. See `storage.py`'s `assessments` table notes above and `docs/SPEC.md` §8.4. Do not
  fold `resume_hash`/`rubric_hash`/`model_name`/`profile_version` into the cache key — that
  reverses this decision, not fixes it.
- Adapters and location logic fail loudly (raise `SchemaError`/`AdapterError`) rather than
  guessing or silently returning partial data — preserve that when touching adapter code.
- Don't add credentials or session/CSRF replay for collection. Browser-based stealth fetching is
  allowed *only* via the existing `stealth_html` adapter for a source with no other viable
  anonymous endpoint — it's a deliberate, disclosed exception (see `docs/SPEC.md` §5.8), not a default; every
  other adapter stays plain httpx, and a new source should too unless one genuinely doesn't exist.
- `--new-only` filters *output*, not collection — collection always observes and persists every
  job returned by a source regardless of CLI flags.
- When adding a company to `companies.yaml`, prefer reusing `ConfigurableJsonAdapter`/`json_api`
  via config over writing a new adapter class unless the platform truly needs bespoke parsing.
- **Any content change to a `skills/*/SKILL.md` file must bump its frontmatter `version` field —
  never leave it unchanged.** A skill's version is the only signal an install (Hermes's spec
  mandates it as a top-level field; see `docs/skill-frontmatter-and-hook-plan.md` section 2.2) or
  a person diffing an update has that its procedure actually changed. Judge the bump size by
  semver convention: a new/changed capability or command the procedure now documents (e.g. a new
  CLI flag it calls, a changed sequencing of steps) is a minor bump; a wording/citation/robustness
  trim with no behavior change (e.g. adding `--project`, de-duplicating restated rationale) is a
  patch bump. This was missed once already — five skills were substantially rewritten in the same
  change that added `job-hunter pipeline` support without any of their versions moving — treat
  that as the mistake to not repeat, not as precedent.
