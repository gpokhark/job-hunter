# Usage guide

Full setup, command reference, skills, and the resume/salary workflows. For the short version see the [README](../README.md); for locks, timeouts, retention and installer internals see [OPERATIONS.md](OPERATIONS.md).

## Sources and caching

Add entries to `config/companies.yaml`, reusing an adapter or onboarding a new one as needed.
Set `enabled: false` or remove an entry to stop collecting from it; stored history remains.
There is no fixed company limit. Searches run on demand; Job Hunter does not submit applications.

**Refresh only what you need.** Assessment caching keys on the job's content hash only — by
design, never on your resume, evaluation model, or rubric — so changing any of those never forces
a re-review of jobs that haven't changed. Run the review script with `--force` when you actually
want a full re-review after such a change. Use `job-hunter search --refresh-details` when you want
to fetch stored descriptions again.

Every command here also takes `--project <path>` (see [OPERATIONS.md](OPERATIONS.md)).

## Setup

```bash
uv sync --all-extras                  # runtime + dev + the optional `stealth` extra, in one command
uv run playwright install chromium    # once: PDF rendering for the resume/cover-letter skills
uv run scrapling install              # once: browser for `stealth_html` sources (astemo, google)
cp config/candidate_profile.example.yaml config/candidate_profile.yaml
uv run job-hunter doctor
```

`playwright` and `pypdf` (resume PDFs) are base dependencies. Only `stealth` is an extra, because
it drives a real browser against bot-protection (see `docs/SPEC.md` §5.8). **`uv sync` makes the
environment match exactly what you ask for**, so a bare `uv sync` (or `--extra stealth` alone)
removes whatever you left out, so keep using `--all-extras` (drop it, and the `scrapling install`
line, only if you don't need the `stealth_html` sources). Browser binaries live in a shared cache
(`~/Library/Caches/ms-playwright` on macOS) and survive syncs.

Edit `config/candidate_profile.yaml` with your title/domain terms and exclusions.

Put your master resume at `config/resume/main_resume_<YYYY-MM-DD>.md` (newest filename date wins;
see `config/resume/README.md`). The profile's `resume_path` remains only as a fallback when no
dated file exists there.

Your resume and filled-in `candidate_profile.yaml` are personal and never committed —
`.gitignore` excludes `config/candidate_profile.yaml`, everything in `config/resume/` except its
README, and any `config/*resume*` file except the checked-in `config/resume.example.md`.

Scoring candidates against your resume needs a local model running in
[LM Studio](https://lmstudio.ai/) (Developer tab > Start Server). Copy
`config/lm_studio.example.yaml` to `config/lm_studio.yaml` (also gitignored) and edit `base_url`
to match your server's address.

## Commands

```bash
uv run job-hunter pipeline                              # search -> review -> radar, one command
uv run job-hunter pipeline --keyword "ADAS,Robotics"
uv run job-hunter pipeline --no-scrape [--review]        # re-filter + re-render, no new scrape
uv run job-hunter pipeline --no-scrape --search <path>   # ...this exact archive, not a resolved one
uv run job-hunter pipeline-status [--run <run-id>]       # poll/inspect a pipeline run's manifest
# A rate-limited or timed-out source keeps what it fetched and is named in the radar's
# Collection Issues with the reason; the pipeline then ends `partial`.

uv run job-hunter search
uv run job-hunter search --json --archive [--keyword "ADAS,Robotics"]
uv run job-hunter search --companies tri,toyota --new-only
uv run job-hunter source-status
uv run job-hunter source-test honda
uv run job-hunter db-stats
uv run job-hunter why-missed ford:71202     # why wasn't this job in the radar? stage-by-stage, plus title terms that would admit it
uv run job-hunter near-misses               # report rejected jobs that look relevant from their description + vocabulary hints
uv run job-hunter export --format json
uv run job-hunter collect start [--companies a,b] [--slow]   # background collection; returns immediately
uv run job-hunter collect status [--json]                     # progress, rate-limited/failed sources
uv run job-hunter collect stop                                # cooperative stop (in-flight sources finish)
uv run job-hunter snapshot                                    # archive from what's collected so far, then pipeline --no-scrape --search <path>
uv run job-hunter cleanup                 # dry run — reports what's eligible, deletes nothing
uv run job-hunter cleanup --apply         # deletes closed jobs / old reports, writes an export first
uv run job-hunter resolve-search [--keyword "..."] [--search <path>]
uv run job-hunter export-assessments
uv run job-hunter export-feedback
uv run job-hunter record-assessment --payload '{"source_key": "tri", "job_id": "...", "company": "...", "title": "...", "url": "...", "score": 82, "recommended": true, "matches": ["..."], "gaps": ["..."]}'
uv run python scripts/review_with_lm_studio.py [--keyword "..."] [--status]
uv run python scripts/render_radar.py [--keyword "..."]
uv run python scripts/assessments_to_csv.py
uv run python scripts/apply_radar_feedback.py --file ~/Downloads/radar-feedback-<report>.json
uv run python scripts/suggest_exclusions.py [--min-support 2]
uv run python scripts/diff_profile.py --add soft_exclude_terms:"some term"
uv run python scripts/diff_profile.py --remove target_domains:"some term"
uv run python scripts/refilter_archive.py [--keyword "..."] [--search <path>] [--output <path>]
```

`why-missed` takes a job id, `source_key:job_id`, a URL (matched by its normalized canonical URL, then an id in its query or last id-bearing path segment) or part of the title and changes nothing.
`near-misses` writes `data/near-miss/<timestamp>.html`/`.csv` listing only jobs first seen since the last scan (`--all` for everything; a first run is capped at 100 rows and any rows a cap or `--limit` drops are counted in the report) and is a scouting aid, never scored or added to the radar.

Searches attempt every enabled source by default; one source's failure doesn't stop the others.
`--new-only` limits output only — collection always observes and persists every job returned.
`--archive` writes to a deterministic `data/searches/{keyword-or-default}_{date}.json`, factoring
in `--companies` too when given (so a company-scoped run never silently overwrites a full,
unscoped one — or vice versa); omitting `--keyword`/`--search` on the review/radar scripts
resolves to the newest archive (see `docs/SPEC.md` §11 and `docs/skill-split-plan.md` §4 for the
full resolution rule).

If a source fails to collect on a given run, the radar report still shows its last-known-good
jobs from a prior successful run rather than silently dropping the source to zero — the report's
"Collection issues" section names the failure and, for each affected source, when its data is
actually from. Pass `--no-collection-fallback` to `render_radar.py` to disable this and see only
what this run actually fetched.

**Live mode:** `uv run python scripts/serve_radar.py --open` serves the radar with click-to-save feedback (SQLite), extra filters (min score, posted-within, company, location, has-salary, feedback state, sort), and "New results — Reload" polling. Static `render_radar.py` output and Export Feedback are unchanged; the server is loopback-only by default. The **Stop server** button (bottom right, confirms first) shuts the server down cleanly; it works on loopback binds only, and Ctrl+C in the terminal still works. Profile edits apply on the next page load; `settings.yaml` edits (age windows, undated days) need a server restart. In live mode, treat the static Export Feedback file as older than any live changes.

**Application tracking (live mode only):** each row has a Track chip that opens an editor panel (status saved/applied/interviewing/offer/rejected/withdrawn, applied date, notes; edits autosave), the toolbar adds Application and Hide applied filters, and `/applications` lists every tracked application. Application data lives in SQLite; `data/applications.json`/`.csv` are refreshed after each save (a refresh failure is reported as a warning and never fails the save), and `uv run job-hunter export-applications` rewrites them on demand. Live saves do not refresh the feedback exports. Applications are never removed by `cleanup`; a posting it deletes shows as "removed".

The radar toolbar (static and live) has chips for New, Long-standing, Remote, Hybrid, **Sponsorship OK**
and **No Sponsorship** (these keep only matching rows, so they also drop the ~85% of jobs that never
mention sponsorship), plus **Hide No Sponsorship**, which removes only jobs that state they don't
sponsor and keeps both Sponsorship OK and unmentioned jobs. All are view-only; the live page keeps
them in the URL hash.

Each radar report row has 👍/🆗/👎 relevance-feedback buttons and a floating "Export Feedback"
button — `apply_radar_feedback.py` ingests the export, `suggest_exclusions.py` turns repeated
"irrelevant" tags into safe `soft_exclude_terms` candidates for `candidate_profile.yaml` (never
auto-applied), and `diff_profile.py` previews any filter-field edit's real effect against every
stored posting before you save it. See `docs/feedback-exclusion-plan.md`/`docs/profile-diff-plan.md`.

### Regenerating the radar without scraping

After editing `candidate_profile.yaml` (e.g. approving a `suggest_exclusions.py` suggestion), refresh
the HTML report from what's already in `data/searches/`/SQLite — no employer site is contacted:

```bash
uv run job-hunter pipeline --no-scrape                 # refilter + re-render, review stays cached
uv run job-hunter pipeline --no-scrape --review         # also score whatever's newly surfaced
uv run job-hunter pipeline --no-scrape --search data/searches/default_2026-09-15.json
                                                         # ...this exact archive, skip resolution entirely
```

The archive is chosen like everywhere else (`--keyword`, or the newest overall if omitted). Its
candidates are rebuilt from SQLite's current active/eligible pool (scoped to that archive's own
sources) rather than narrowed in place, so a *loosening* edit (a new `strong_relevance_terms`
override, a removed `exclude_terms`/`soft_exclude_terms` entry) can bring back jobs an earlier
narrowing edit dropped. Both directions are printed (`N removed, M gained`), and the updated radar
plus a gained/lost diff report are written. Unlike plain `pipeline`, review is **off** by default,
since a profile edit alone doesn't warrant spending local-model time; pass `--review` when it does.

"Newest archive" means the run date in the filename (mtime only breaks a same-date tie), so a
refilter re-stamping its target can't make a small archive look newer than a large one. Two
same-date archives with different keywords still fall back to mtime. To skip resolution, pass
`--search <path>`; to sanity-check what a resolution will pick, `job-hunter resolve-search
[--keyword "..."]` prints the path on stdout and a scope summary on stderr (`scope: N sources
attempted (...)`) — a "default" archive that only queried one company is a sign something's off.

The manual three-step equivalent (what `pipeline --no-scrape` runs; useful to inspect or skip a step):

```bash
# 1. (optional) rebuild the archive's candidates from SQLite against the current profile — no network call
uv run python scripts/refilter_archive.py [--keyword "ADAS,Robotics"]

# 2. (optional) score any not-yet-assessed candidates — calls your LM Studio endpoint only
uv run python scripts/review_with_lm_studio.py [--keyword "..."] [--status]

# 3. render/update the report
uv run python scripts/render_radar.py [--keyword "..."]
```

Step 1 matters only if the profile changed since collection; step 2 only if `--status` shows
candidates remaining; both are otherwise safe no-ops. Pass the same `--keyword` to all three. Step 3
alone re-renders what's already there.

## Skills

Nine independently-invocable skills, so each stage can be run, checked on, or resumed standalone:

- **`job-scout`** — search (`job-hunter search --archive`)
- **`job-reviewer`** — score candidates against your resume via local LM Studio
- **`job-radar`** — compile + render the report
- **`job-hunter`** — orchestrator that runs `job-hunter pipeline` end to end (or `pipeline
  --no-scrape` to re-filter + re-render without a new scrape)
- **`job-feedback`** — turn relevance feedback and profile edits into a confirmed profile update
- **`onboard-source`** — repo-maintenance skill for adding a new employer source to job-hunter
  itself (see "Adding a new source" below); not part of a normal job-search session
- **`resume-generator`** — tailored, ATS-friendly resume (HTML + PDF) from your newest master resume and a job description (see "Resume and outreach")
- **`outreach-writer`** — outreach email and/or cover letter for the same job
- **`salary-compare`** — compare a job offer's total compensation against your current package, research the market rate, and draft a negotiation plan (see "Salary negotiation")

Example invocations (see `docs/SPEC.md` §11.1 for the full set, including exact-path resume and
`--status` progress checks):

```
/job-hunter                          # full pipeline, profile-driven
/job-hunter ADAS                     # full pipeline, keyword-scoped
/job-hunter --no-scrape              # you edited candidate_profile.yaml — refilter + re-render,
                                      #   no new scrape; add --review to also score what's new
/job-scout                           # search only, profile-driven
/job-scout ADAS                      # search only, keyword-scoped
/job-reviewer --keyword ADAS         # start or resume review for that keyword — re-invoking
                                      #   this exact command after an interruption just continues
/job-radar --keyword ADAS            # render/update the report — safe to re-run any time,
                                      #   including mid-review
/job-radar --keyword ADAS --refilter # re-apply candidate_profile.yaml to an already-collected
                                      #   archive and re-render — no scraper call
/job-feedback                        # after tagging jobs 👍/🆗/👎 in a radar report, or after
                                      #   any candidate_profile.yaml edit (yours or a suggested
                                      #   one) — shows the exact before/after effect, asks before
                                      #   writing anything or accepting a new baseline
```

Use `job-hunter` when you want the whole pipeline run in one go; use `job-scout`/`job-reviewer`/
`job-radar` on their own for a single stage — resuming an interrupted review, re-rendering after
an edit, or checking what's failing — without repeating the stages before it. `job-feedback` is
the separate, occasionally-invoked loop that turns radar feedback and profile edits into a
confirmed `candidate_profile.yaml` change; it's never part of a `job-hunter` run.

Install with:

```bash
sh scripts/install_skill.sh
```

This installs all nine skills and prompts interactively for which runtime(s) to install into
(Hermes, Claude Code globally, Claude Code for this repo only, OpenCode, or any combination). To
skip the prompt, pass one or more target flags instead, e.g. `sh scripts/install_skill.sh
--claude-local`, `sh scripts/install_skill.sh --all`. Add `--copy` to create independent copies
instead of symlinks (`--link` names the default explicitly, if you want to say so). Re-run with
`--update` to replace a stale install (e.g. after moving the repo or a skill), or `--uninstall` to
remove one; `--dry-run` shows what any of the above would do without touching anything. Run
`sh scripts/install_skill.sh --help` for the full flag list.

## Resume and outreach

The `resume-generator` and `outreach-writer` skills turn a job the radar found into a tailored resume
(HTML + PDF) and outreach copy. Everything they produce lives under the git-ignored `data/output/`; the personal inputs they read live
under the git-ignored `config/resume/` (and `config/candidate_profile.yaml`).

**Setup**

1. Complete **Setup** above (`uv sync --all-extras`, then `uv run playwright install chromium`; only PDF output needs the browser).
2. Fill the `contact:` block in `config/candidate_profile.yaml` (`name` and `email` required; `job-hunter contact` checks it).
3. Save your master resume as `config/resume/main_resume_<YYYY-MM-DD>.md`. The newest filename date wins; `job-hunter resume-files` shows what will be used.
4. Optional: `cp config/resume/personalization.example.md config/resume/personalization.md`, then replace the sample rules with your own. It has `## all`, `## resume-generator` and `## outreach-writer` sections for standing preferences (tone, phrases, emphasis, page-size defaults) and employer-specific rules such as naming a client only for one employer. The skills themselves contain no personal data.

**Workflow**

1. Open the live radar (`uv run python scripts/serve_radar.py --open`) and click **Resume** on a job. The JD is saved under `data/output/<Company>/` and a prompt is copied to your clipboard.
2. Paste the prompt into Claude Code or Hermes. `resume-generator` writes the resume and PDF next to the JD and logs it in `data/output/resume_log.csv`.
3. Ask for an outreach email and/or cover letter; `outreach-writer` uses the same folder.

Free-text instructions work on every run ("2 page, lead with functional safety", "address it to Sam
Lee"). They apply to that run only and never override the integrity rules: nothing fabricated, no invented
recipient names or contact details, nothing that contradicts your master resume.

**Portability.** No Microsoft Word is involved; the same steps work on Windows, macOS and Linux. Page
fill is measured by the machine that renders the PDF, and fonts differ between operating systems, so
results can vary slightly per machine. Without Chromium the skills print the install commands
instead of a PDF. See `docs/SPEC.md` §11.2.

## Salary negotiation

The `salary-compare` skill turns a job offer into a total-compensation comparison and a negotiation
plan. `scripts/salary_compare.py` (stdlib only, no extra installs) does every calculation — taxes,
net pay, break-even base salary, negotiation numbers — so results are reproducible; the skill does the
market/cost-of-living research and writes the comparison and negotiation script.

**Setup**

1. `uv run python scripts/salary_compare.py init` creates `config/salary_config.json` (git-ignored) from
   `config/salary_config.example.json`. Fill in your real current package (base, bonus, 401k match,
   perks, premiums, location, PTO).
2. Copy `config/offer.example.json` to `data/output/<Company_Name>/offer_<Company>_<Role>_<date>.json`
   for each real offer.

**Workflow**

1. Ask Claude Code or Hermes to compare an offer ("should I take this offer from Acme", "compare this
   offer to my current job").
2. `salary-compare` fills in the offer file, verifies the relevant states' tax data, researches market
   pay and cost of living, then runs `scripts/salary_compare.py compare` and writes
   `data/output/<Company_Name>/comparison_<date>.md`.
3. It writes a negotiation plan — floor/target/stretch/anchor numbers, a phone script, and an email
   draft — to `data/output/<Company_Name>/negotiation-plan_<date>.md`.

Like your resume and offer files, everything with real compensation numbers stays under the git-ignored
`config/salary_config.json` and `data/output/`; only the `.example.json` templates are tracked.

## Development

```bash
uv sync --all-extras
uv run pytest -m "not live"
uv run ruff check .
```

Tests use saved response fixtures and need no internet (the browser test skips without Chromium).
Runtime dependencies are `httpx`, `selectolax`, `pydantic`, `PyYAML`, plus `playwright`/`pypdf` for
resume PDFs. The optional `stealth` extra adds [Scrapling](https://github.com/D4Vinci/Scrapling) and a
real headless browser, used only by the `stealth_html` adapter (`docs/SPEC.md` §5.8).

## Adding a new source

Given a company name, its careers listing URL, and one sample job URL, the `onboard-source`
project skill (`skills/onboard-source` — installable for every runtime including Hermes via
`install_skill.sh`, same as the other eight; `.claude/skills/onboard-source` is a symlink to it)
discovers the real backing system, wires up (or writes) an adapter, tests it, verifies it live,
and updates this README and `docs/SPEC.md`. See `docs/SPEC.md` §5.20 for the manual process and
`skills/onboard-source/references/discovery-playbook.md` for known ATS/platform signatures.

## Keep your search history

Your collected data stays in `data/`, including `jobs.sqlite3`, search archives, cached
assessments, feedback, and reports. Your personal profile and resume live in `config/`.
To move to another machine, stop running collection/review processes and copy both complete
folders into the new checkout. Recreate dependencies with `uv sync --all-extras` (plus the two one-time browser installs) and reinstall agent skills
from the new location. Git alone does not transfer these ignored personal and generated files.

## Themes (forest, forest-dawn)

`config/settings.yaml` → `radar.theme`: `auto` (default, the plain light/dark page), `forest`
(night) or `forest-dawn` (sunrise). `render_radar.py --theme NAME` overrides it for one run;
`serve_radar.py` and `job-hunter pipeline` read the setting. The theme applies to the static radar,
the live radar and the Applications page.

Optional `vision:` block in `config/candidate_profile.yaml` (display text only):

```yaml
vision:
  role: "Staff Perception Engineer"   # shown on the Signed & Sealed card
  pay_note: "Better. Higher paying."
  start_note: "Start date: soon"
  use_first_name: true                 # false keeps your name out of the generated pages
```

The first name is `contact.name` without a leading honorific; it is omitted if missing, still the
example placeholder, or `use_first_name: false`. The page's **offer mode** switch (bottom-left,
remembered in the browser) hides every manifestation line and restores the original labels.
Rendered pages contain your first name, so mind screenshots you share.
