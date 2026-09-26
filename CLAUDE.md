# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Job Hunter: manual Python collector for employer career sites. Normalizes postings, applies a
strict U.S.-eligibility filter, persists history in SQLite, emits a compact JSON candidate bundle
for an LLM agent (`skills/job-scout`/`job-reviewer`/`job-radar`, or the `skills/job-hunter`
orchestrator) to score against a resume. Doesn't schedule searches or apply to jobs.

Collection is config-driven HTTP/HTML fetching, with one exception: `stealth_html`
(`src/job_hunter/adapters/stealth_html.py`) drives a real headless stealth browser (Scrapling) for
sources with no plain anonymous endpoint — either bot-blocked (`astemo`: Cloudflare Turnstile) or
JS-rendered content (`google`). Using it is a disclosed choice to defeat anti-automation controls
(real ToS exposure, not solved by "it's public data") — don't reach for it by default; every other
adapter stays plain httpx. See `docs/SPEC.md` §5.8.

**Before reaching for `stealth_html`, always check for a real backend behind a blocked/skinned
front end** — a real job link, not a guess, is what reveals it:
- GM: looked Cloudflare-blocked like Astemo; actually a public unauthenticated Workday API. Dropped the adapter.
- Stellantis: JS-`onclick` pagination capped scraping at ~10 jobs; real backend is ADP Recruiting Management, a public two-call handshake (`adp_recruiting.py`). Dropped `stealth_html` too.
- Apple: assumed browser-only; plain HTML actually server-renders a full JSON snapshot (`window.__staticRouterHydrationData`, needs one extra unescape). No browser needed.
- OpenAI: marketing site genuinely Cloudflare-challenged, but the real ATS is Ashby (`jobs.ashbyhq.com/openai`, public posting API), zero-auth.
- Anthropic: NOT on Ashby despite the obvious-looking URL (`jobs.ashbyhq.com/anthropic` 404s) — it's Greenhouse (`job-boards.greenhouse.io/anthropic`).
- Roche: front end works fine unblocked, but each job's `applyUrl` points to a public Workday CXS API with the full global catalog vs. the front end's one-category-at-a-time view. A working front end isn't a reason to stop looking either.

Related trap: **an endpoint good for scraping isn't always safe to hand a human as `url`.** GM's
Workday CXS API and Ford/DENSO's Oracle HCM REST API are JSON endpoints, not browsable pages —
opening them just shows JSON. Fix: decouple `fetch_detail`'s API url from the human-facing `url`
(`workday.py` reconstructs the CXS url from `externalPath`; `json_api.py` gained an opt-in
`public_url_template` for Ford/DENSO). New JSON-API adapters: ask whether `url` is human-openable
or only `fetch_detail`-parseable.

A 200 response with plausible job cards doesn't prove a query param filtered anything — Molex
(koch.avature.net) ignored every guessed param and only revealed its real one (`732_format=...`)
via a one-time Playwright drive to observe its own form-redirect URL. Only two *different* facet
values producing two *different* result sets proves a filter is real. Conversely, not every
Radancy/TalentBrew site hides a different backend — Toro's is genuinely plain server-rendered
HTML; confirm by finding real job cards in the raw response, don't assume from branding.

Division of responsibility is load-bearing: **Python owns networking, normalization, persistence,
health, and location filtering; the agent skill owns evidence-based resume scoring.** Don't move
scoring into Python or retrieval into the skill.

## Safe testing — NEVER overwrite real data

- Smoke tests and pipeline test runs must write to a temp output dir (e.g. `--out-dir $(mktemp -d)`) or use a `--dry-run` flag. Never write to the real daily archive or radar HTML.
- Before any run that writes reports or archives, list the target paths and check whether they exist with `ls -la <exact path>`. Report the result truthfully.
- Output filenames must reflect filters such as `--companies` and `--project`.

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
uv run python scripts/check_lm_studio.py  # is the configured LM Studio server actually reachable right now?

uv run pytest                           # full suite (fixtures only, no network)
uv run pytest tests/test_adapters.py::test_name   # single test
uv run pytest -m "not live"             # skip tests marked live (network-dependent)
uv run ruff check .
```

Tests use saved response fixtures in `tests/fixtures/` and never hit the network unless marked
`live`. Setup requires `cp config/candidate_profile.example.yaml config/candidate_profile.yaml`
before most commands will find a profile (falls back to the example file otherwise).

## Architecture

**Pipeline:** `cli.py` → `Collector.search()` (`collector.py`) → per-company `JobAdapter` →
normalize → `location.evaluate_location()` → `prefilter.passes_prefilter()` → `Storage` upsert →
ranked `SearchResult` JSON.

- **`config.py`** — loads/validates three YAML files via pydantic: `config/settings.yaml`
  (collection tuning, DB path), `config/companies.yaml` (per-employer adapter selection),
  `config/candidate_profile.yaml` (title/domain terms, exclusions, resume path). An `unsupported`
  adapter entry *must* carry `unsupported_reason`, enforced by a model validator — a company with
  no verified endpoint and no viable `stealth_html` path stays `unsupported`, never faked data.

- **`adapters/`** — one class per ATS platform family (`workday.py`, `lever.py`, `ashby.py`,
  `greenhouse.py`, `oracle_hcm.py`, `phenom.py`, `successfactors_rmk.py`, `html_paginated.py`,
  `html_multi_index.py`, `discovered_api.py`, `stealth_html.py`, `adp_recruiting.py`, `apple.py`,
  `eightfold.py`, `successfactors_rmk_v2.py`, `bosch.py`, `zf.py`, `csod.py`, `icims_attract.py`,
  `dayforce.py`, `smartrecruiters.py`, `paycom.py`, `paylocity.py`, `ultipro.py`, `brose.py`), registered in
  `adapters/__init__.py`'s `ADAPTERS` dict, selected via `companies.yaml`'s `adapter` key. All
  inherit `JobAdapter` (`adapters/base.py`): retry-with-backoff `request()` (429/500/502/503/504 +
  network/timeout, honors `Retry-After`), default `healthcheck()`. Adapters implement
  `fetch_summaries()` (required), optional `fetch_detail()`.

  `json_api.py`'s `ConfigurableJsonAdapter` is a generic JSON-listing kernel driven by
  `companies.yaml` config (`list_url`, `items_path`, `fields`); several adapters are thin
  subclasses:
  - `oracle_hcm.py` adds pagination (`config: {paginate: true, total_path: ...}`) — Oracle's finder syntax caps page size below some sites' full count.
  - `ashby.py` (OpenAI) is a one-line alias, no bespoke code — the public posting API returns everything flat (full `descriptionHtml`, structured address fields, `workplaceType` → `work_arrangement` via generic opt-in `fields.work_arrangement`).
  - `greenhouse.py` (Anthropic): `content` comes back HTML-entity-double-encoded — `fetch_detail` unescapes once, same shape `apple.py` handles.

  HTML adapters use `selectolax` with CSS-selector config instead of schema paths;
  `posted_at_selector` covers per-card dates (`normalizer.parse_display_date`).
  - `html_paginated.py`'s `fetch_detail` always checks for schema.org JobPosting JSON-LD (`normalizer.extract_job_posting_ld`) regardless of config, using its `datePosted`/`employmentType` without overriding `description_selector`'s find.
  - `html_multi_index.py` (HRI) adds a fallback tried only when JSON-LD finds nothing: a `publicationDate` inside an inline Liferay `JobOfferData` JS object (`normalizer.parse_liferay_publication_date`). Feeds `passes_recency` with no exemption, so most of HRI's postings filter out as stale by design.

  Use `scripts/endpoint_probe.py` (or curl) to inspect a candidate endpoint before wiring a new
  adapter — never hand-invent a shape. When real content/pagination only exists post-JS (or params
  are silently ignored, e.g. Honda's `start`/`num` vs. real `from`/`s`), render once with Scrapling
  (`stealth_html`'s `AsyncStealthySession`) to discover the real DOM/links, then hardcode as static
  config — browser as a one-time discovery tool, not a runtime dependency (`docs/SPEC.md` §5.20).

  Other lessons: a static frontend Bearer token embedded in plain HTML (not from a login endpoint)
  is safe to reuse, same category as Ashby's public key (`bosch.py`). When one CSS class holds
  several different fields' values, match by an adjacent label's text, not `:nth-of-type`
  (unreliable — counts among *all* siblings of the tag, not just same-class ones; BMW's
  `.rtltextaligneligible` via `successfactors_rmk_v2.py`'s `_parse_job_layout_tokens`). The same
  ATS platform can wear different templates per customer (BMW/VW both SuccessFactors RMK Job2Web,
  one server-rendered, one client-rendered) — recognize by shared static-asset hosts/paths, not
  page appearance.

- **`collector.py`** — orchestrates one search run: fetches all companies concurrently
  (`max_concurrent_sources` semaphore), fetches details only when needed (no prior record / no
  description / `--refresh-details`), concurrently per source (`max_concurrent_details`),
  re-evaluates location using detail data, upserts into `Storage`, computes `SearchSummary`. One
  source's failure never aborts others (per-company exceptions → `FAILED`/`UNSUPPORTED`
  `SourceHealth`); CLI exits non-zero only if *all* sources failed. A job whose listing-level
  `posted_at` already proves staleness skips its detail fetch (`is_recent()`, `prefilter.py`) —
  this is what made large catalogs (Apple, Ford, Stellantis) fast. Apple/Stellantis also stop
  paginating early (confirmed sorted newest-first); Ford/DENSO's Oracle listing isn't reliably
  sorted, so it always fetches the full catalog. `storage.mark_missing(stale_before=...)` prevents
  early-pagination-stop from falsely closing jobs it simply stopped looking for. See
  `docs/SPEC.md` §12.

- **`location.py`** — `evaluate_location` → `LocationDecision` (`us_eligible`, `confidence`,
  `evidence`). Precedence: structured country/state fields → "remote in the U.S." phrasing → U.S.
  state name/abbrev → explicit "United States" text → a short allowlist of state-less U.S. metro phrases ("San Francisco Bay Area"; add here, never bare city names that exist abroad) → recognized non-U.S. country/city list → bare
  "remote" with no U.S. evidence rejected as low-confidence. This ordering prevents ambiguous
  multi-location/remote postings from being mis-included/excluded — read the comments before
  reordering.

- **`sponsorship.py`** — `evaluate_sponsorship`, same pattern as `location.py` but for visa stance
  (`Job.visa_sponsorship`: available/not_available/unmentioned + `sponsorship_evidence`).
  **Purely informational, never a filter** — `passes_prefilter` never calls it; a company's stance
  can change, so surface it, don't gate on it. Uses a curated phrase list, not bare `"sponsor"`
  (real false positives: PACCAR's "sponsor Key-Op program", Hyundai's "sponsors... training",
  Valeo's "sponsored sports hall"/Polish insurance phrase — none visa-related). Strips HTML tags
  before matching (Nissan's structured `<b>Sponsorship:</b> No` needs this). Not-available phrases
  checked before available ones (GM's "DOES NOT PROVIDE... SPONSORSHIP" must never hit a looser
  positive pattern). Defaults to `unmentioned` — never guess.

- **`salary.py`** — `evaluate_salary`, same evidence-based never-a-filter pattern, for
  `Job.salary_min`/`salary_max`/`salary_currency` + `salary_evidence`. One regex: a real two-number
  range (`$X` then `-`/`–`/`—`/`to` then a second number, `$` optional on the second). Validated
  against live GM/Honda/Ford/Torc postings plus the full stored pool (~9,500/~30,000 matched, no
  false positive beyond the pattern below). Never matches a bare single dollar figure (Ford's
  "$3,000"/"$1,500" benefits boilerplate isn't salary). Excludes literal `"$0.00 - $0.00"`
  (unfilled Workday template field on Caterpillar/Nissan union postings). Unescapes HTML entities
  before matching (Torc's Greenhouse postings render `—` as literal `&mdash;` inside a separator
  `<span>`). `storage.py`'s `reevaluate_salary()`/`reevaluate_sponsorship()` (also CLI commands)
  backfill stored jobs from existing description, no network — needed since `upsert_job` only sets
  these on fresh collection.

- **`prefilter.py`** — `passes_prefilter`'s positive-term gate matches only `job.title` +
  `job.department`, never free-text `description`. Deliberate fix: description-wide matching let
  boilerplate ("...autonomous driving technologies...") or optional "preferred" bullets inject a
  target term into unrelated postings (a GM RF-hardware role passed purely via an optional ADAS
  bullet + boilerplate). `department` stays in-gate because it's structured metadata (e.g. Honda's
  "Autonomous Tech Dev Dep"), not marketing prose. `exclude_terms` still scans full `description` —
  over-excluding is low-risk; the fix only targets the inclusion side. `relevance_score` (ordering
  only) still uses the full haystack.

  `passes_prefilter` takes an optional `keywords` override (`--keyword`): when given, *replaces*
  the profile's `target_title_terms`/`target_domains` for that run (still title+department scope);
  `exclude_title_terms`/`exclude_terms`/U.S.-eligibility stay in force. `passes_recency` is a
  separate deterministic date check (`max_posting_age_days`, default 30) — no discoverable
  `posted_at` means kept, not excluded.

  No default candidate cap (`--max-candidates` is explicit opt-in). A removed
  `recommendation.max_results * 3` cap used `relevance_score` (a coarse keyword-count heuristic) as
  tiebreaker under the old full-description gate (~74% pass rate, ~3,100+ jobs/run) — that let
  zero-relevance postings (bare "senior"/generic "validation") occupy review slots. The
  title+department-scoped gate passes ~50-150/day, small enough for full sequential local-LLM
  review. Candidates sort newest-first (not by `relevance_score`) so an interrupted review covers
  freshest postings first; final ranking is always the LLM's own score.

  Two profile fields for feedback-derived exclusions (`docs/feedback-exclusion-plan.md`):
  `soft_exclude_terms` (like `exclude_terms`, title+department only — description-wide matching
  here produced false positives, e.g. Ford's "vehicle platform architectures" vs. Apple's "platform
  architecture") is overridden whenever title+department also matches `strong_relevance_terms`
  (deliberately not `target_domains`, which already contains the broad terms —
  `validation`/`verification`/`simulation` — that caused the false positives). Both empty by
  default; populated only via human-approved `scripts/suggest_exclusions.py` suggestions.

  Gating logic lives in `evaluate_prefilter()` → `PrefilterDecision` (`passes`, `rule:
  PrefilterRule`, `term`, `rescued_by`), mirroring `location.py`/`sponsorship.py`'s structured-
  verdict pattern. `passes_prefilter` is a thin `.passes` wrapper for existing callers.
  Short-circuit evaluation: `rule`/`term` name the *decisive* check only, not every failing check.

- **`scripts/diff_profile.py`** — compares two `CandidateProfile`s (two saved YAMLs, or the
  on-disk profile + an in-memory `--add`/`--remove` patch never written back) against every stored
  `us_eligible`/recency-passing job via `evaluate_prefilter` directly. Reports four counts
  (retained/still-excluded/gained/lost) + terminal summary + HTML report
  (`data/profile-diff/{timestamp}.html`, local-timezone filename via `_report_timestamp`/bare
  `.astimezone()`; `evaluated_at` also converted to local time via `_local()` everywhere displayed)
  with per-job before/after reason plus any existing assessment/feedback label. Reads via a genuine
  read-only SQLite connection (not `Storage`, whose `__init__` runs migrations/commits). Maps
  `canonical_url` → `Job.url`. `--keyword` fully replaces `target_domains`/`target_title_terms`
  like everywhere else — testing a field edit alongside `--keyword` correctly shows zero effect,
  and the tool says so explicitly. Report includes a "Profile terms" section (`_field_term_diffs`:
  +added/-removed/unchanged per field) rendered as tags in the HTML. No `--apply` (see
  `docs/profile-diff-plan.md` §7) — that's about writing changes to the file, separate from the
  baseline mechanism below (a verbatim copy, never a parse/re-serialize).

  With no flags: **check mode** — diffs the on-disk profile against
  `data/candidate_profile.snapshot.yaml` (a plain-text copy of "profile as of last accepted
  baseline," so it can't damage the real file's formatting). Answers "what changed since I last
  looked" regardless of edit source. Only shows the diff, never auto-advances the baseline (an
  earlier version auto-advanced, which could silently bake in an unreviewed diff) — advancing needs
  explicit `--accept-baseline`, which keeps the replaced snapshot at `.snapshot.prev.yaml` for one
  level of undo via `--rollback-baseline` (a true swap, idempotent on repeat). The first-ever
  check-mode run bootstraps the snapshot with nothing to compare — no `--accept-baseline` needed
  for that one. See `skills/job-feedback/SKILL.md`.

- **`scripts/refilter_archive.py`** — answers "what would this already-collected archive's
  candidates look like under the *current* profile," no network. Rebuilds `candidates` from
  SQLite's current `status='active' AND us_eligible=1` pool, scoped to the archive's own
  originally-attempted source keys (so onboarding a new company later can't retroactively add its
  jobs to an old archive) — rather than narrowing the archive's existing `candidates` in place
  (tried first, found to be a one-way ratchet: a `soft_exclude_terms` edit could drop a job whose
  data was then gone, so a later loosening edit had nothing to restore). Rewrites the archive in
  place by default (`--output` for elsewhere); prints gained/lost/retained. Unless `--no-report`,
  writes HTML to `data/profile-diff/archive-{stem}-{timestamp}.html` (same local-timezone helpers,
  shared `_e`/`_fmt_posted_date`/`_job_tags`/feedback JS as `diff_profile.py`, a simpler template
  with no "Profile terms" section — only one profile here). `--keyword` means the same full
  replacement everywhere. Its SQLite query moved to `src/job_hunter/active_pool.py`'s
  `raw_active_jobs()` once `render_radar.py` needed the same query per-source (behavior-preserving
  refactor, confirmed by existing tests).

- **`src/job_hunter/active_pool.py`** — the shared active/eligible-job-pool query.
  `raw_active_jobs()` (every active/US-eligible job, optionally source-scoped, no prefilter/
  recency — `refilter_archive.py` needs the unfiltered shape to distinguish failure reasons) and
  `source_jobs()` (one source's active/eligible/prefilter/recency-passing jobs, built on the
  former — what `render_radar.py`'s fallback calls). `source_scope` is pushed into SQL `WHERE ...
  IN (...)`, not fetch-then-filter (the `jobs` table is 230MB, 98.5% `description` text — a naive
  fetch would mean a full scan per failed source per render); an empty scope short-circuits before
  touching SQLite (empty `IN ()` is invalid SQL).

- **`src/job_hunter/pipeline.py`** — `job-hunter pipeline`: sequences search → review → radar (or,
  `--no-scrape`: refilter → optional review → radar, offline), writing
  `data/runs/<run_id>/manifest.json` (`PipelineManifest`/`PipelineStage`/`PipelineStatus`) so
  `job-hunter pipeline-status` can poll instead of re-parsing three commands. Review/radar (and
  refilter in `--no-scrape`) stages run via `subprocess` against existing standalone scripts — this
  module supervises, doesn't reimplement. `--no-scrape` skips live search, re-runs
  `refilter_archive.py` against a resolved archive (rejects `--companies` — refiltering re-evaluates
  an already-attempted scope, not a fresh selection); its `--review` defaults **off** (opposite of
  normal mode's `--skip-review` opt-out — a deliberate asymmetry, `docs/pipeline-refilter-stale-
  source-plan.md` §4.2). `--no-scrape --search PATH` targets an exact archive, bypassing
  keyword/mtime resolution (added after mtime "newest" picked a narrow recently-refiltered archive
  over a larger one collected more recently — every refilter re-stamps its target's mtime).
  `--keyword` still applies independently of `--search`.

  Each stage invokes its script with `--result-json PATH`; manifest fields are read back from that
  structured file, never parsed from stdout — a stage exiting 0 with no result file is `FAILED`,
  never zero-defaulted. `settings.pipeline.stage_timeout_seconds` (default `28800`/8h, `None`=
  unlimited if unset) bounds each stage, launched via `Popen(start_new_session=True)` so a timeout
  kills the whole process group (`os.killpg`), not just the direct child. Killed-stage output is
  decoded defensively (`_decode_timeout_output`, `errors="replace"`) — `TimeoutExpired.stdout/
  .stderr` can come back as raw bytes even under `text=True`, and a mid-UTF-8-char kill used to
  crash `write_manifest()` outright. `status`: `complete`/`partial`/`failed`/`no_candidates`/
  `model_unavailable`/`lock_held`/`timed_out` (`PIPELINE_NON_SUCCESS_STATUSES` shared between
  `pipeline` and `pipeline-status` CLI handlers). Manifest records `pid`/`pid_start_time`
  (`runlock.process_start_time`) — `pipeline-status` checks both, not just PID liveness, before
  calling a stuck `RUNNING` manifest `abandoned` (PID reuse). Records `profile_fingerprint`/
  `resume_fingerprint`/`model` for provenance only, never wired into cache invalidation. The whole
  function body is wrapped in try/except that finalizes the manifest as `failed` with the real
  error before re-raising (fixes a `--no-scrape` archive-resolution failure leaving the manifest
  stuck `running` forever — regression-tested). `RunLockHeld` → its own `lock_held` status, not
  generic `failed`.

- **`src/job_hunter/rootutil.py`, `atomic.py`, `runlock.py`** — portability infra. `atomic.py`/
  `runlock.py` are universal (every load-bearing writer/lock-holder uses them, hooks included).
  `rootutil.py`'s `--project`/`add_project_argument()` is used by the CLI and every *operational*
  `scripts/*.py` entry point (`apply_radar_feedback.py`, `assessments_to_csv.py`,
  `check_lm_studio.py`, `diff_profile.py`, `refilter_archive.py`, `render_radar.py`,
  `review_with_lm_studio.py`, `suggest_exclusions.py`); not `endpoint_probe.py`/
  `prototype_tfidf_broad_match.py`/`search_to_csv.py` (diagnostic/prototype/pure-stdio), and hook
  scripts take the project root positionally per their own runtime convention instead. Authoritative
  list: `grep -L add_project_argument scripts/*.py` (`docs/SPEC.md`).

  `--project`/`$JOB_HUNTER_ROOT` resolve+`chdir` once, early, before any relative path is touched
  (`git -C` semantics) — the single choke point making a command location-independent, since every
  config/data default is a bare relative `Path`. Registered on every subparser, not just root
  (argparse needs it per-position; `job-hunter <command> --project X` is the form every skill
  actually uses — this was a real shipped bug before the fix). `atomic.py`'s `atomic_write_text()`
  (temp file + `os.replace()`) backs every load-bearing JSON/HTML write, so an interrupted/racing
  writer never leaves a truncated file. `runlock.py`'s `run_lock()` is a per-project PID-based file
  lock shared under one name, `"job-hunter"`, across every SQLite/archive/`assessments.json`
  mutator outside a single serialized review call: `pipeline` (whole run), `cleanup --apply`,
  `review_with_lm_studio.py`, `refilter_archive.py`'s in-place rewrite — confirmed via a direct
  concurrency repro that a second run against a held lock is refused with `RunLockHeld` naming the
  holder, not raced. Stale-lock reclaim retries unlink-then-recreate, and retries reading a
  freshly-created lock file for ~200ms before concluding it's stale (a lock file is briefly empty
  between `O_CREAT|O_EXCL` and its write — a reader landing there used to misread this as abandoned
  and wrongly reclaim a live lock).

  Because `pipeline.py` holds this lock for the whole run before spawning
  `review_with_lm_studio.py`/`refilter_archive.py`, which each also try to acquire the identically-
  named lock, they'd deadlock against their own parent. Fixed via `run_lock_or_inherited()`:
  `pipeline.py` generates a per-acquisition token (`secrets.token_hex(16)`, written into the lock
  file) and passes it via `JOB_HUNTER_LOCK_INHERITED`; the function only treats the lock as
  inherited when that env var matches `current_lock_token()`'s live read, falling back to a real
  acquire on any mismatch/missing file/unset var (fails closed) — a bare boolean flag was rejected
  since any standalone caller could set the env var and skip the lock with no verification.

- **`src/job_hunter/hook_adapter.py`** — shared logic behind the Claude Code
  (`scripts/claude_profile_hook.py`) and Hermes (`scripts/hermes_profile_hook.py`) profile-diff
  hooks: `should_run_diff()` (does an edited path resolve to this project's
  `candidate_profile.yaml`) and `run_diff()` (runs `diff_profile.py` check mode). Each runtime
  script owns only its stdin wire shape (Claude's `PostToolUse`: `tool_input.file_path`; Hermes's
  `post_tool_call`: `tool_input.path`) and calls these — directly unit-testable, no subprocess/
  stdin faking needed. `run_diff()` discovers `uv` via `shutil.which` and logs every failure mode
  to `logs/profile-hook.log` (vs. the previous Hermes-only hook's silent
  `contextlib.suppress`) — a silently-failing hook is worse than an advisory one. Guards against
  overlapping runs: a non-blocking `run_lock("profile-hook", ...)` skips if another hook invocation
  for the project is mid-run, plus a 5s debounce marker file (`logs/.profile-hook-last-run`) — both
  logged, never silent. `HOOK_LOCK_NAME` (`"profile-hook"`) is its own lock, separate from
  `runlock.py`'s `"job-hunter"` pipeline lock, since this hook only reads SQLite and rewrites the
  profile-diff snapshot/report. `.claude/settings.json`'s `PostToolUse` invokes
  `scripts/run_profile_hook.sh` (portable POSIX-`sh`) rather than a bare `uv run python ...` — the
  direct form failed at the shell level if `uv` wasn't on the *invoking* process's `PATH`, before
  `hook_adapter.py`'s own `shutil.which("uv")` check (which diagnoses the *inner* call) ever ran;
  the launcher locates `uv` itself, falls back to `python3` with a stderr note, and always exits 0
  (advisory by design).

- **`storage.py`** — SQLite (WAL). Five tables: `jobs` (one row per `(source_key, job_id)`,
  upserted with `is_new`/`is_changed` from content hash), `runs` (per search invocation),
  `source_health` (per-source rolling status/consecutive-failures/last-success), `assessments`
  (per `(source_key, job_id)`, a local model's verdict — score/recommended/matches/gaps — written
  by `review_with_lm_studio.py` via `upsert_assessment()`/`get_valid_assessment()`, or manually via
  `record-assessment`; never produced by Python itself), `job_feedback` (per `(source_key,
  job_id)`, a human's click-through relevant/okay/irrelevant verdict from a rendered radar report —
  `docs/feedback-exclusion-plan.md`; `diff_profile.py`'s HTML report shares the identical feedback
  buttons/export shape, so `apply_radar_feedback.py`/`suggest_exclusions.py` don't need to know
  which report a label came from). Upserted, not appended — a later label replaces the earlier
  one. Written only by `scripts/apply_radar_feedback.py` from exported feedback JSON — with no
  `--file`, auto-resolves the newest `radar-feedback-*.json` in `~/Downloads` (always prints which
  file/mtime), exits cleanly if none exists. `job-hunter export-feedback` mirrors
  `export-assessments`. Read only by `scripts/suggest_exclusions.py` — `prefilter.py` never reads
  this table directly, only whatever a human approved into the profile. That script re-evaluates
  every tagged job against the *current* profile via real `evaluate_prefilter` to route a
  suggestion to whichever of all six filtering fields the job's pass/fail reason implicates (not
  just `soft_exclude_terms` — `docs/feedback-exclusion-plan.md` §13).

  A job is marked `closed` after 3 consecutive runs missing from a healthy source's listing
  (`mark_missing`); otherwise stays `active` (why previously-seen jobs surface by default).
  `mark_missing`'s `stale_before` excludes jobs already past a source's early-pagination-stop
  cutoff (otherwise a source that deliberately stops looking past the recency window would falsely
  close every job that ages past it). `Collector.search()` joins `assessments` onto each candidate
  as `Job.prior_assessment` only when the stored `content_hash` still matches — a changed posting
  is treated as unassessed again. Lets `job-hunter`'s per-job sub-agent review skip already-scored
  jobs at zero token cost.

  No table ever deletes rows automatically — 230MB after 9 days, 98.5% `jobs.description`, closed
  rows kept forever. `job-hunter cleanup` (`cleanup.py`, opt-in, dry-run by default,
  `docs/retention-cleanup-plan.md`) is the answer, reusing `last_seen_at` as a free "days since
  closed" clock (`mark_missing()` never touches it on close — it already means "last confirmed
  present"). Gotcha: SQLite `DELETE` doesn't shrink the file — `VACUUM` is required to reclaim
  space. Sets `PRAGMA busy_timeout = 5000` so two concurrent job-hunter/agent processes wait
  briefly on lock contention instead of failing immediately. Schema evolution via `PRAGMA
  user_version` against a numbered `_MIGRATIONS` list (replacing ad-hoc `PRAGMA table_info`-plus-
  `ALTER TABLE` checks — mechanism refactor, not behavior change: the two pre-existing checks
  became the first migration entries). `_migrate()` applies every unseen entry then advances
  `user_version` — idempotent.

- **`health.py`** — `detect_count_anomaly` flags (doesn't fail) a source whose job count drops
  >70% from its last known count — guards against adapters "succeeding" against a changed page
  structure while returning far fewer/no jobs.

- **`models.py`** — pydantic schema: `JobSummary` (listing data) → `Job` (summary + detail +
  location decision + dedup metadata); `SearchResult` is the CLI/skill output envelope.

- **`skills/`** — agent-facing half, six independently-invocable skills
  (`docs/skill-split-plan.md`): `job-scout` (search → archive), `job-reviewer` (local-LLM
  scoring), `job-radar` (compile + render), `job-feedback` (turn radar feedback/profile edits into
  a confirmed profile update via `diff_profile.py` check mode), `job-hunter` (orchestrator),
  `onboard-source` (repo-maintenance skill for adding a new employer source, not end-user;
  `.claude/skills/onboard-source` symlinks to `skills/onboard-source` so it installs for every
  runtime). Each `SKILL.md` is the canonical procedure for its stage: run the collector, read only
  `candidates`, never recommend `us_eligible=false`, never invent salary/sponsorship/
  qualifications. Scoring is delegated entirely to `scripts/review_with_lm_studio.py`
  (deterministic script, not a sub-agent) — sends each unassessed candidate to a **local** LM
  Studio model, one at a time, strictly sequential, zero cloud tokens; the calling agent just runs
  it then reads `data/assessments.json`/`export-assessments`. Persists each verdict immediately,
  so an interrupted run resumes by re-invoking with the same `--keyword`/`--input`.
  `job-reviewer/references/scoring.md` defines the rubric; `job-scout/references/
  troubleshooting.md` covers source-health diagnosis.

  When a review run reports `model_unavailable`, run `scripts/check_lm_studio.py` before
  concluding LM Studio itself is down — an agent runtime (Hermes) can misreport this when the real
  fault is its own network path (different container/host, stale LAN IP, firewall). Shares one
  implementation with the real review call, `src/job_hunter/lm_studio_health.py`'s
  `check_lm_studio()`/`load_lm_studio_config()`, so they can't disagree about server state; prints
  `OK`/`FAIL LM Studio: ...` (exit 0/1).

  `job-feedback` mandates two explicit stop-and-confirm points — which terms to write into the
  profile, and whether to accept a diff as the new baseline — never inferred from silence.
  `job-hunter` is a thin wrapper around `job-hunter pipeline`/`pipeline-status`, documenting
  `--no-scrape [--review]` by citing `job-radar`'s `SKILL.md` by step number instead of restating
  its rules. Every skill's frontmatter carries `compatibility`/`metadata`
  (`metadata.hermes.tags`, no `license:` — deliberately unlicensed) and a `## Contract` section
  (`docs/skill-frontmatter-and-hook-plan.md`). **A skill's `version` must bump on any content
  change** — see "Working in this repo" below. Install for other runtimes via
  `scripts/install_skill.sh`, supporting `--update`/`--uninstall`/`--dry-run`/`--link`/`--copy`.
  `--uninstall`/`--update` are gated on an ownership check
  (`<dest-parent>/.job-hunter-installed` marker, one basename per line) rather than unconditional
  `rm -rf`, to avoid deleting a manually-placed or third-party directory at the same path; a
  pre-marker install (this repo's own `.claude/skills/*` symlinks) is still recognized via the
  existing up-to-date/stale-symlink signal. A destination matching neither is refused unless
  `--force`.

  `job-hunter search --archive` writes each run to `data/searches/{slug}_{date}.json`
  (`search_archive.py`'s `archive_path()`) instead of one overwritten `data/latest_search.json` —
  same keyword+day overwrites itself, different day/keyword gets its own file, reachable later via
  `resolve_search_path()` (also `job-hunter resolve-search --keyword ...`). `archive_path()` folds
  `--companies` into the filename when it restricts the run (company keys sorted before
  slugifying) — fixes an incident where a `--companies`-scoped run silently overwrote a same-day
  65-source archive. Omitting `--companies` (most runs) is unaffected.

  `resolve_search_path()`'s "newest" fallback is raw mtime, and every refilter re-stamps its
  target — so a narrow, frequently-refiltered archive can permanently outrank a larger one
  collected more recently (`pipeline --no-scrape` with no `--keyword` once picked a 1-company
  archive over a 43-company sweep 2 days older). `pipeline --no-scrape --search PATH` is the
  escape hatch for a caller who knows the exact archive. For a caller who doesn't, `job-hunter
  resolve-search` prints a stderr scope summary (`scope: N sources attempted (top 5 by job
  count...)`) from the resolved archive's `source_health`, making a mismatch visible at resolution
  time; stdout's bare-path contract is unchanged. Mtime-based resolution itself remains a known,
  deliberately out-of-scope gap.

  `data/assessments.json`/the `assessments` table are deliberately **global**, never split per
  keyword/run — a verdict is a property of *(job, resume)*, not of whichever search surfaced it;
  splitting would force re-review of the same job under every different keyword that matches it.
  Cache validity keys on `content_hash` only, never `resume_path` — updating the resume never
  forces re-review (by design); any job actually sent to the model is scored against whatever
  resume is on disk then. `scripts/render_radar.py` is the read-time join: given one archived
  search + the global assessments, renders the grouped/tagged HTML report (Strong ≥75, For-review
  50-74, Below-50; five-step color gradient across 50-100, not a hard 80/90 cutoff; `[New]` tag) to
  `data/radar/{slug}_{date}.html` via `templates/radar_template.html`. Pure presentation — never
  re-derives/adjusts a score. Every actually-scored candidate appears in one of the three sections
  regardless of score; only a review-skipped candidate (LM Studio error, `--limit`) is absent,
  counted in `never_reviewed`.

  One disclosed exception to "pure presentation": the stale-source-collection fallback
  (`docs/pipeline-refilter-stale-source-plan.md` §4.3) — a source whose live collection `failed`
  this run (not `warning`, which had real live data; not `unsupported`, which has no cached data)
  still has last-known-good jobs in SQLite. `build()` merges that source's current active/
  eligible/prefilter/recency-passing jobs (`active_pool.source_jobs()`) into the rendered pool,
  extending its Collection Issues row with a note (job count + last-collected time, or "no prior
  data" if never succeeded) — no per-row badge, the note is the only signal. This is why `build()`
  takes `database_path`; the archive file is never rewritten, only the HTML, so re-running stays
  idempotent. `--no-collection-fallback` (default: on) restores the old no-jobs note. Scoped to
  `render_radar.py` alone, not `collector.py` — the live collector's meaning ("jobs fetched this
  run") stays untouched; this is a report-layer merge, not a live-collection change.

## Job Radar Pipeline

### Archives & refilter

- 'Default' archive = today's full default search across ALL companies. Do not pick it by mtime alone; resolve it by date plus search name, and print the chosen archive path and its company count before running refilter or reports.
- If a report looks stale (for example, missing tags), check the archive snapshot date before debugging the code.

## Job Sources

### Onboarding a job source checklist

1. Identify the ATS (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Eightfold, Phenom, etc.) and prefer the JSON API (e.g. Ashby `publishedAt`, JSON-LD `datePosted`) over HTML selectors.
2. Add config and tests, verify live, and confirm that title, description, `posted_at` and location are all populated.
3. Update README and docs.
4. If the site is blocked by Cloudflare or rate-limited, mark it `unsupported` or paced and tell the user.

- On macOS use `sed -i ''` or edit with Python instead.

## Working in this repo

- **Prefer false negatives over false positives in any filtering mechanism** — judge an
  exclude/soft-exclude/scoring gate by whether it could ever wrongly reject a genuinely relevant
  posting, and eliminate that risk, not merely reduce it. A false negative costs a little visible
  wasted review time, cheap to fix next round. A false positive silently costs the job itself,
  invisible in any report. Resolve any either/or tradeoff in favor of fewer false positives.
  Worked example: `docs/feedback-exclusion-plan.md`.
- **Assessment cache validity keys on `content_hash` only — never `resume_path`, model, or
  rubric.** Updating your resume/model/rubric must never force re-review of already-assessed jobs;
  only a changed posting (new `content_hash`) triggers a fresh call. Same tradeoff category as
  above: a little staleness (rerun with `--force` when you want a full re-review) beats burning
  sequential local-model time re-scoring hundreds of unchanged jobs. Don't fold
  `resume_hash`/`rubric_hash`/`model_name`/`profile_version` into the cache key. See `storage.py`
  above, `docs/SPEC.md` §8.4.
- Adapters and location logic fail loudly (`SchemaError`/`AdapterError`) rather than guessing or
  silently returning partial data — preserve this.
- Don't add credentials or session/CSRF replay for collection. `stealth_html` is the only allowed
  browser-based fetch, and only when no anonymous endpoint exists (`docs/SPEC.md` §5.8) — every
  other adapter stays plain httpx.
- `--new-only` filters output, not collection — collection always persists every job returned
  regardless of CLI flags.
- Prefer `ConfigurableJsonAdapter`/`json_api` config over a new adapter class unless the platform
  truly needs bespoke parsing.
- **Any content change to a `skills/*/SKILL.md` file must bump its frontmatter `version`.** It's
  the only signal an install or a diff has that the procedure changed. Semver: new/changed
  capability or step sequencing = minor; wording/citation trim with no behavior change = patch.
  This was missed once (five skills rewritten alongside `job-hunter pipeline` support, no version
  bumped) — don't repeat it.
