# Live radar dashboard and application tracker — implementation plan

Status: **proposed; no feature code is implemented.**

Audit date: **2026-09-25** (revision 2: re-audited against the repository — see §10 for what the
audit changed). Implement in the order below and keep the static file:// radar working throughout.

## 1. Outcome and boundaries

Add an opt-in localhost server:

    uv run python scripts/serve_radar.py [the same report-selection flags as render_radar.py]

It serves a fresh radar page, persists feedback immediately, and (Phase B) tracks applications for
collected jobs. The browser filters and sorts the already-rendered rows; /applications shows
tracked applications after a posting is closed or disappears from the selected archive.

The work ships in two independently releasable phases. Phase A must be merged, green, and usable
before Phase B starts.

**Phase A — live radar and feedback**

- server-rendered radar HTML plus a small vanilla-JS live layer;
- live-only filters added to the existing toolbar: min score, posted within N days, company,
  state/location text, has salary, feedback state; plus sort within tiers;
- URL-hash filter state and session-only open/closed group state;
- SQLite-backed feedback ("relevant", "okay", "irrelevant") with click-to-save and untag;
- stale-write protection (§3.4), polling for new archive/assessment data, tab reconciliation.

**Phase B — application tracking**

- "saved", "applied", "interviewing", "offer", "rejected", "withdrawn" statuses per job;
- Applications page, JSON/CSV exports, `job-hunter export-applications`;
- application-state filters on the radar and a Hide-applied toggle.

Out of scope: manual applications, application history/reminders/contacts, LLM calls, scoring or
prefilter changes, launching pipeline/profile edits from the browser, an SPA rewrite,
authentication, multi-user behavior. A non-loopback bind is an explicitly warned unauthenticated
operator feature, not a production server.

The static report remains usable exactly as today: `render_radar.py` writes data/radar/*.html,
works from file://, uses localStorage plus Export Feedback, and never calls fetch(). **Static
output must stay byte-for-byte compatible for everything the live work does not need to touch**:
all new toolbar controls and scripts are emitted only when `live=True`. In live mode SQLite is the
truth; localStorage is only a bounded retry outbox. Archive and assessment content keeps following
the existing archive/`data/assessments.json` read path.

## 2. Audited repository contracts

Verified against the code on the audit date.

- Python 3.11+, Pydantic 2, no web framework/template engine/JS test framework. Use stdlib
  `http.server`, `sqlite3`, and the existing `str.replace` placeholder template approach.
  `StrEnum` is already used in `models.py`.
- `Storage.__init__` opens one connection, sets `busy_timeout=5000`, and runs `initialize()`
  (DDL `executescript` incl. `PRAGMA journal_mode=WAL`, `_migrate()`, commit) on **every open**.
  `_MIGRATIONS` currently has exactly two entries (v1 sponsorship, v2 salary_evidence); append v3.
- `job_feedback` is keyed `(source_key, job_id)`; `upsert_job_feedback()` is an unconditional
  upsert that commits. `JobFeedback.label` is plain `str`; the importer validates against its own
  `_VALID_LABELS`. `JobFeedback.recorded_at` defaults to `utcnow()`.
- `apply_radar_feedback.py` already rewrites `data/job_feedback.csv` on every run;
  `job-hunter export-feedback` writes `data/job_feedback.json`. It **auto-resolves the newest
  `radar-feedback-*.json` in ~/Downloads** when no `--file` is given, and sets `recorded_at` to
  *import time*. The static export JSON entries carry no timestamp of their own.
- `render_radar.build()` reads the archive and assessments file, merges the stale-source fallback
  (`_apply_collection_fallback`, needs `database_path`/`profile`/`max_age_days`), tiers rows, does
  `str.replace` on the template, and `atomic_write_text`s. Scored rows go to `_row_html`
  (`<details class="row">`); unscored candidates go to `_never_reviewed_row_html`
  (`<div class="plain-row">`, "NR" score, `data-score=""` on its feedback buttons). Both are read
  by the same client scripts as `.row, .plain-row`.
- Row layout: `summary` grid is 4 fixed tracks (`44px 1fr auto auto`); `.tags` is always emitted
  (even empty) and `.row-end` holds date, feedback buttons, and "View posting". New controls go in
  `.row-end` or `.row-detail`, never a new grid track.
- **Row attributes constraint (found in audit):** `_filter_data_attrs`' docstring explains company
  and title are deliberately *not* attributes on the row — a `data-title` earlier in the markup
  than the visible title broke tests that locate a row by `html.index(title)`
  (`tests/test_render_radar.py`, tiers test and "ADAS Engineer" test). The existing filter JS reads
  `.job-title`/`.job-company` text instead. Preserve this.
- The existing filter script keeps filter state unpersisted *by design*; `updateEmptyStates()` finds
  each tier's empty message as `.rows`' next sibling `.filter-empty-state`; each tier's
  `.group-note` says "Sorted by score, highest first"; group counts and header stats are static
  server-rendered numbers. Rows are pre-sorted by `-score` in Python.
- Archive candidates are full `Job` dumps: `state`, `city`, `country`, `location_raw`,
  `salary_evidence`, `posted_at`, `first_seen_at` are all available to the renderer.
- Every archived candidate came from a collector upsert, so it has a row in `jobs` (until
  `cleanup` removes closed jobs). Scores live in the `assessments` table and `data/assessments.json`.
- `review_with_lm_studio.py`, the CLI exports, and `render_radar` all write via `atomic_write_text`.
- `cleanup.delete_closed_jobs()` returns `{"jobs", "assessments", "job_feedback"}` and `cleanup.py`
  writes a pre-delete export of exactly those.
- `run_lock(name, lock_dir=data/locks)`; search/review/pipeline/cleanup share `"job-hunter"`. The
  server takes its own `"radar-server"` lock for its lifetime.
- Operational scripts must register `--project` via `add_project_argument()` and call
  `chdir_to_project_root()` first (`render_radar.main()` is the model). `serve_radar.py` does too.
- `resolve_search_path()` "newest" is raw mtime, and refilters re-stamp mtime (documented gap in
  CLAUDE.md). CLAUDE.md also requires printing the chosen archive path and its company count.

## 3. Models, migration, and storage

### 3.1 Models (`models.py`)

    FeedbackLabel = Literal["relevant", "okay", "irrelevant"]

    class ApplicationStatus(StrEnum):      # Phase B
        SAVED = "saved"
        APPLIED = "applied"
        INTERVIEWING = "interviewing"
        OFFER = "offer"
        REJECTED = "rejected"
        WITHDRAWN = "withdrawn"

Phase A: change `JobFeedback.label` to `FeedbackLabel` (the importer's own check stays as the
friendly-error path; `_VALID_LABELS` derives from `get_args(FeedbackLabel)`).

Phase B: add `Application`:

    source_key, job_id, status,
    applied_at: date | None, notes: str | None,
    company, title, url, location, posted_at, score, salary_evidence,
    created_at: datetime, updated_at: datetime

Use the existing UTC-aware `utcnow()`. `applied_at` is a local calendar date (`YYYY-MM-DD`); bound
notes (e.g. 4,000 chars); reject blank keys/title/url; `score` nullable.

### 3.2 Migrations

Phase A appends `_migrate_v3_create_feedback_tombstones`; Phase B appends
`_migrate_v4_create_applications`. Both use `CREATE TABLE IF NOT EXISTS`; never renumber.

    CREATE TABLE IF NOT EXISTS feedback_tombstones (
        source_key TEXT NOT NULL, job_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
        PRIMARY KEY (source_key, job_id)
    );
    CREATE TABLE IF NOT EXISTS applications (
        source_key TEXT NOT NULL, job_id TEXT NOT NULL, status TEXT NOT NULL,
        applied_at TEXT, notes TEXT,
        company TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
        location TEXT, posted_at TEXT, score INTEGER, salary_evidence TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (source_key, job_id)
    );

No FK to `jobs`: applications are snapshots and survive retention. Tombstones are retained.

### 3.3 Storage API

- Phase A: `delete_job_feedback(source_key, job_id, deleted_at)`, `get_job_feedback()`,
  `feedback_map()`, and a guarded write (§3.4). Existing `upsert_job_feedback()` keeps its
  signature and unconditional behavior for existing callers/tests; the guarded write is a new
  method both the server and the importer call.
- Phase B: `get_application()`, `all_applications()`, `upsert_application()`,
  `delete_application()`, `export_applications()` (ordered `updated_at DESC`).
- `delete_closed_jobs()` is unchanged and keeps its return shape. Add a regression test that
  `applications` and `feedback_tombstones` survive it.

### 3.4 Stale-write rule (replaces "tombstone clears only when newer")

Feedback is last-writer-wins by **event time**, for *every* writer, not just deletes:

- Each write carries an `event_at` (UTC). A write is applied only if `event_at` is **strictly
  newer** than the existing row's `recorded_at` *and* any tombstone's `deleted_at`; otherwise it is
  rejected as stale and nothing changes. A newer applied label clears the tombstone; a delete
  writes/updates the tombstone and removes the row (same transaction, one commit).
- Live browser writes send `client_ts` (ISO-8601 UTC, ms). The server uses it as `event_at`,
  clamped to `<= now` (single-machine clocks; reject a future timestamp rather than trusting it).
  This is what makes an outbox retry that lands late unable to overwrite a newer edit from another
  tab. A stale rejection returns `{ok: true, stale: true, item: <current state>}` (200, not an
  error) so the client converges to the server's state instead of retrying.
- The static importer's `event_at` is the **export file's mtime** (the export entries have no
  timestamp). This fixes a real hazard the audit found: with no `--file`, the importer picks the
  newest `~/Downloads/radar-feedback-*.json`, possibly weeks old, and today would overwrite a
  live relabel or resurrect an untagged job. Importer counters stay `new/changed/unchanged/invalid`
  with an added `stale-skipped` shown only when nonzero; a same-label re-import stays `unchanged`.
  `recorded_at` for imported rows becomes the export's mtime (was import time) — document this.
  Known limit: a stale *static* page exported *after* a live untag still wins; document, don't
  engineer around.
- Applications (Phase B) use the same rule per record via `updated_at` vs `client_ts`.

Application rules (Phase B):

- Creation only for a key present in `jobs` (server-derived snapshot; §5.2). Unknown → 404. No
  manual-job API in this feature.
- First transition to `applied` defaults `applied_at` to the server's local date.
- `saved` always has null `applied_at`; moving back clears it. Other statuses preserve or default.
- Explicit dates must parse as real ISO dates. Delete removes the row and notes after confirmation.
- Snapshot fields are taken at first creation; later edits change only status/date/notes.

## 4. Renderer and template

### 4.1 Preserve build(), add pure render()

In `scripts/render_radar.py`, extract archive/path resolution into a shared helper and split:

    render(..., live: bool = False, state: LiveState | None = None) -> tuple[str, dict[str, Any]]
    build(...) -> dict[str, Any]

`render()` performs the current archive/assessment join, fallback merge, tiering, and template
substitution without writing, returning `(html, stats)`. `build()` calls `render(live=False)`,
`atomic_write_text`s the HTML, and returns the current stats keys unchanged (existing callers and
all 24 renderer tests must pass unmodified). The renderer takes an explicit `LiveState` (feedback
map, application map) — it never opens a hidden DB connection of its own.

*(Dropped from revision 1: render() no longer returns a pool index. Writes are validated against
the `jobs` table — §5.2 — so no render is needed on the write path.)*

Row attributes (both `_row_html` and `_never_reviewed_row_html`), added only when `live=True`, on
the row root next to the existing `_filter_data_attrs`: `data-source-key`, `data-job-id`,
`data-score` (empty for NR), `data-posted` (`YYYY-MM-DD` or empty), `data-state`, `data-country`,
`data-has-salary` (`1`/`0`). **Do not add company or title attributes** (§2 constraint); company/
title/location filtering reads `.job-company`/`.job-title`/`.job-location` text as the existing
filter does. Initial feedback and application state are rendered into the markup from `LiveState`
so a reload never flashes untagged rows. Embedded JSON escapes `<`, `>`, `&`, U+2028/2029.
Client metadata is never authoritative for persistence.

### 4.2 Template behavior

Add placeholders `__LIVE_MODE__` (`true`/`false`), `__LIVE_STATE_JSON__`, `__LIVE_TOOLBAR__`
(empty in static), and `__LIVE_SCRIPT__` (empty in static). The existing feedback and filter
IIFEs are left in place for static mode. In live mode:

- the static feedback IIFE and Export button are omitted; a status pill shows Live, Saving…, or
  Offline (N unsaved); a footer line shows the resolved archive filename, attempted-source count,
  and render time;
- the live toolbar adds the new controls; existing search/chips keep working via the existing
  script, whose `applyFilters()` gains a small hook so live-only predicates compose with it;
- Phase B: compact Track chip in `.row-end`, full status/date/notes in `.row-detail`, Applications
  link; "View posting" propagation guards are preserved.

`scripts/templates/applications_template.html` (Phase B): applications sorted by status then
applied date; counts; days-since-applied; posting **open / closed / removed** (`jobs.status`;
"removed" = job row deleted by cleanup — a state revision 1 omitted); inline edits; never
descriptions or DB paths. All user/scraped text (notes, titles, company) is HTML-escaped.

The live client logic is one small vanilla-JS file, `scripts/templates/radar_live.js`, inlined into
the page at render time (the page stays a single response; no extra route serves it). Keeping pure
logic (filter predicate, sort, hash encode/decode, outbox coalescing, reconcile) as plain functions
in that file makes them unit-testable with `node --test` where node exists (§8).

### 4.3 Client rules

1. Optimistically update, POST with `client_ts`, revert visibly on failure. The outbox
   (localStorage, bounded) stores `{kind, key, payload, client_ts}`, **coalesced to the latest write
   per key**, retried on focus/next request/backoff. A permanent 4xx (unknown job, validation) is
   dropped with a visible notice — never retried forever, never silently swallowed. A `stale: true`
   reply is applied as authoritative state and clears that outbox entry.
2. On visible `visibilitychange`, GET the state maps and reconcile rows so tabs converge.
3. Poll `/api/state` every 10 s while visible. A changed archive/assessment version shows "New
   results — Reload"; never auto-reload.
4. Filters live in `location.hash` (this deliberately reverses the static script's "not persisted"
   stance, but only in live mode — update that comment in the live path, not the static one); group
   open/closed state in `sessionStorage`.
5. Sorting within a tier rewrites DOM order inside that tier's `.rows`, keeps the
   `.filter-empty-state` sibling contract, and updates the tier's "Sorted by …" note. Counts:
   the toolbar shows "Showing X of Y", and each tier count appends ` · N shown` while any filter
   is active; header stats stay as rendered. Filtering never writes data.
6. Prefer event delegation; add no dependency.

## 5. Server contract

`scripts/serve_radar.py`: `ThreadingHTTPServer` (not the single-threaded `HTTPServer` — one slow
render must not block polls and saves) with a `BaseHTTPRequestHandler`. Register `--project` via
`add_project_argument()` and `chdir_to_project_root()` before anything else. Hold
`run_lock("radar-server")` for the process lifetime. Open a fresh `Storage` per handler; run
`initialize()`/migrations once at startup so requests only pay the (cheap) re-check. Never write
data/radar/, serve arbitrary files, or list directories.

### 5.1 CLI

Current `render_radar.py` selection flags (`--search`, `--assessments`, `--title`, `--keyword`,
`--companies`, `--new-days`, `--undated-*-days`, `--no-collection-fallback`) plus:

    --host  default 127.0.0.1
    --port  default 8765
    --open  optional webbrowser.open("http://<host>:<port>/")
    --allowed-host  repeatable; extra Host values accepted (needed only for non-loopback binds)

Resolve an omitted `--search` on each root request; an explicit `--search` stays fixed. **Print the
resolved archive path and its attempted-source count at startup and whenever the resolved archive
changes** (CLAUDE.md archive rule; mtime "newest" can silently pick a narrow archive). Port
collision is a clear failure. A non-loopback `--host` prints a prominent unauthenticated-API warning.
Reload profile/settings per request (soft-exclude edits affect the fallback pool).

### 5.2 Routes

| Method/path | Contract |
|---|---|
| GET / | Fresh radar HTML; `Cache-Control: no-store`. |
| GET /applications | (Phase B) Fresh Applications HTML; `no-store`. |
| GET /api/state | Versions (archive, assessments, feedback, applications) plus counts. |
| GET /api/feedback | `{"ok": true, "feedback": {key: {source_key, job_id, label, recorded_at}}}`, keyed `source_key\|job_id`; no descriptions. |
| GET /api/applications | (Phase B) Application rows. |
| POST /api/feedback | `{source_key, job_id, label, client_ts}`; `label` null deletes. |
| POST /api/application | (Phase B) `{source_key, job_id, status?, applied_at?, notes?, client_ts}`; omitted fields unchanged; status null deletes. |

Success: `{ok: true, item}`, `{ok: true, deleted: true}`, or `{ok: true, stale: true, item: <row|null>}` (§3.4; `null` = currently untagged).
Pydantic request models; reject blank/unknown keys; cap the body **before** parsing. Bad
JSON/validation 400 JSON; unknown job 404; SQLite operational/lock errors 503; unexpected errors
logged and returned as 500 without stopping the server.

**Authoritative job data comes from the `jobs` table** (PK lookup selecting only the needed
columns — never `description`), with `score` from the `assessments` table; no render on the write
path. Because every archived candidate was upserted into `jobs`, this covers fallback-merged jobs
too. A feedback delete may target an existing feedback key even if the job row is gone (stale tab
untag). An existing application update uses its stored snapshot even if the posting is gone.

Versions: archive/assessments via `mtime_ns` + size; feedback/applications via a deterministic
hash of the canonical exported rows, so a relabel/edit changes the version even at equal row count.

### 5.3 Request checks

- **Host:** reject before dispatch unless it matches the bound host:port; for a loopback bind also
  accept `localhost`/`127.0.0.1`/`[::1]` with the port. A wildcard bind (`0.0.0.0`/`::`) accepts
  only loopback names plus `--allowed-host` values — never arbitrary aliases.
- **POST:** `Content-Type: application/json` (charset allowed; this forces a CORS preflight for any
  cross-origin page, which is the real CSRF barrier); a present `Origin` must equal the request's own
  origin; an absent `Origin` is allowed (curl/scripts). No CORS headers ever. Max body 64 KiB.
- These reduce accidental cross-origin/DNS-rebinding exposure; they do not authenticate.

### 5.4 Exports (Phase B)

After each successful application write, atomically refresh `data/applications.json` and
`data/applications.csv` from committed SQLite. Formatting lives in one shared package helper used
by the server and `job-hunter export-applications`; fixed CSV columns documented there.
**CSV cells beginning with `=`, `+`, `-`, `@` (or tab/CR) are prefixed with `'`** — titles/notes are
scraped or free text.

**Export failure does not fail the write.** The row is committed, so the API returns
`{ok: true, item, export_warning: "..."}` (logged); the next write or `export-applications` repairs
the files. (Revision 1 returned an error and made the client revert a saved change.)

Live feedback writes do **not** refresh `data/job_feedback.json`/`.csv` (those come from
`export-feedback` / the importer); `suggest_exclusions.py` reads the table, so it is unaffected.
Document this so nobody trusts a stale CSV. No automatic feedback CSV writer in this feature.

## 6. Compatibility/documentation changes

Update in the same implementation (per phase):

- `scripts/apply_radar_feedback.py`: guarded write with export-mtime event time, `stale-skipped`
  counter, label validation from `FeedbackLabel`; existing counts/output otherwise unchanged.
- `src/job_hunter/cli.py`: `export-applications` (Phase B).
- `docs/SPEC.md`: §8.5 (`job_feedback` + tombstones + stale-write rule), new applications section,
  §8.6 (cleanup exemption), §9 (CLI), §10 (scripts), and the API/route table.
- `README.md` (feedback section near the `apply_radar_feedback.py` example), `CLAUDE.md`
  (storage, scripts list, "safe testing"), `skills/job-radar/SKILL.md` (live-server option),
  `skills/job-feedback/SKILL.md` (live SQLite writes; its step that runs `apply_radar_feedback.py`
  must note the stale-import guard). **Bump every changed skill's `version`** (currently job-radar
  1.2.0, job-feedback 1.1.0; new capability = minor).
- Add the server, templates, live JS, and export helper to the repository maps/examples.

Do not modify `diff_profile.py` or `refilter_archive.py`. Their copied static feedback JS is a
separate refactor; document that limitation.

## 7. Implementation order

**Phase A**
1. `FeedbackLabel`, v3 migration, tombstones, guarded write, storage tests.
2. Importer guard (mtime event time, `stale-skipped`) + tests.
3. Pure `render()`/`build()` split, live placeholders, row attributes, render tests (static bytes
   unchanged; existing tests pass untouched).
4. Server, request models, security tests, per-request storage, lock, fresh rendering, startup
   archive report.
5. Live JS (feedback, outbox, filters, sort, hash, polling, reconcile) + node tests + integration.
6. Docs/skill versions; ruff + full suite; report unrelated baseline failures separately.

**Phase B** (after A is merged)
7. `Application` model, v4 migration, storage, cleanup-survival tests.
8. Export helper, `export-applications`, JSON/CSV/idempotence/formula-guard tests.
9. Application routes, Track UI, Applications page, application filters, tests.
10. Docs/skill versions; ruff + full suite.

## 8. Tests and acceptance

Safe-testing rule (CLAUDE.md): every test uses a temp project dir/DB; **no test or smoke run writes
the real `data/` archive, radar HTML, exports, or `applications.*`**. Server tests use an ephemeral
port; manual smoke uses `--project` on a temp copy.

Storage/model: fresh and pre-v3/v4 DBs; label/enum/date validation; stale-write matrix (older
import vs newer live label, older import vs tombstone, newer import clearing a tombstone, equal
timestamp rejected, future `client_ts` rejected); application upsert/partial update/delete;
snapshot immutability; export order; cleanup preservation.

Renderer: static output has no `fetch`/live placeholders and existing tests pass unmodified; live
mode embeds initial state; every row (scored and NR) has identity/filter attributes and **none carry
title/company**; escaping of hostile titles/notes; existing static localStorage/export unchanged.

Server: all routes; unknown-route 404; no file/directory exposure; no radar-file writes; feedback/
application create/update/delete; unknown job 404; server-derived snapshots (client-supplied
company/title ignored); stale reply shape; malformed JSON, wrong content type, foreign origin,
invalid host, oversized body, 503; port collision; state-version changes after a new archive,
assessment, relabel; export refresh and `export_warning` on export failure; application surviving
cleanup and archive disappearance (Phase B).

Client logic: pure functions in `radar_live.js` covered by `node --test` when `node` is on PATH
(skipped otherwise — no new project dependency); one manual/browser smoke of the live page
(filters, hash restore, two-tab reconcile, offline outbox) recorded in the PR description.

Acceptance:

1. Static `render_radar.py` still produces a usable report; existing renderer tests pass unchanged.
2. `serve_radar.py` serves live pages without writing radar HTML.
3. Feedback survives reload and another tab; an untag or relabel cannot be reverted by a stale
   import or a late outbox retry.
4. (B) Applications move through statuses, survive cleanup, appear on /applications, and exist in
   both exports.
5. Client metadata never trusted for persistence; `data/` never served; non-loopback bind warns.
6. Startup prints the resolved archive and its source count.

## 9. Deliberately deferred

- Manual/referral applications and append-only application-event history.
- Reminders, follow-up dates, contacts, richer CRM fields.
- Serving profile-diff/refilter reports or consolidating their duplicated feedback JavaScript.
- Authentication, TLS, multi-user authorization, network deployment.
- Live-mode auto-refresh of `job_feedback.json`/`.csv`.

## 10. What the repository audit changed (revision 2)

- Row attributes: company/title removed — they conflict with a documented, test-backed constraint.
- Sync: tombstone-only protection replaced by a general event-time stale-write rule; the importer's
  auto-picked stale `~/Downloads` file could overwrite live relabels, not just resurrect deletes.
- Importer facts corrected: it already writes `job_feedback.csv`, sets `recorded_at` to import time,
  and exports carry no timestamps.
- Write path validates against `jobs` (all archived candidates are upserted there); `render()` no
  longer returns a pool index, avoiding a full render per POST.
- `Storage.__init__` re-runs DDL per open → migrate once at startup; server is threaded.
- Migration numbering: v3 (tombstones, Phase A), v4 (applications, Phase B).
- Export failure returns a warning, not an error; CSV formula guard added.
- Added: `--project`, archive-path/scope startup report, Host/Origin details, `--allowed-host`,
  "removed" application state, outbox coalescing/permanent-error handling, node-testable client JS,
  live-only new controls so static output stays untouched, SPEC/README/skill targets by section.
- Scope split into Phase A (live radar + feedback) and Phase B (applications) — a recommendation;
  merge them back into one phase if preferred.
