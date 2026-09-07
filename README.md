# Job Hunter

**Your next job deserves a smarter search—not another thousand tabs.**
Job Hunter turns employer career pages into a focused shortlist using deterministic filters
and local AI resume scoring. Run it with **Hermes Agent** or directly from the CLI.

- **Collect:** 60 companies with implemented adapters. Add, disable, or remove sources.
- **Filter:** Your profile sets repeatable rules for U.S. eligibility, relevance, and exclusions.
- **Evaluate:** Python filters first; a local LLM scores the shortlist, saving model tokens.
- **Reuse:** Stored jobs and cached assessments avoid repeated detail fetching and AI reviews.
- **Decide:** Browse an HTML job radar with fit scores and per-job sponsorship evidence.
- **Refine:** Preview filter changes and rebuild your shortlist from local data—no new scrape.
- **Inspect:** Source failures are reported; uncertain locations are excluded with reasons.

## How it works

**Collect → Filter → Review locally → Explore your job radar.** SQLite keeps the jobs,
assessments, and feedback between runs. Fresh searches revisit listings but reuse stored
job descriptions; report generation needs no additional model calls.

**Your profile controls the shortlist.** Title and department matches, exclusions, and explicit
relevance overrides make filtering deterministic. U.S. eligibility and relevance gates favor
conservative inclusion over speculative matches. Unknown posting dates are retained;
sponsorship is labeled available, not available, or unmentioned, and never acts as a filter.

**Local AI handles fit.** LM Studio evaluates shortlisted jobs against your resume. Unchanged
jobs with cached assessments are skipped, and interrupted reviews resume where they stopped.
LLM scores can vary when recomputed; deterministic filtering happens before that step.
Hermes may use its own model provider and tokens for orchestration.

**Refresh only what you need.** Assessment caching uses the job's content hash. After changing
your resume or evaluation model, run the review script with `--force`. Use
`job-hunter search --refresh-details` when you want to fetch stored descriptions again.

## Company coverage

The [registry](config/companies.yaml) contains **63 companies: 60 with implemented adapters
and 3 explicitly unsupported** (Tesla, Meta, and MathWorks). Sources include Toyota, Honda,
Ford, NVIDIA, Apple, Google, Microsoft, Rivian, and Waymo. Check current source health with
`source-status` or `source-test`.

Add entries to `config/companies.yaml`, reusing an adapter or onboarding a new one as needed.
Set `enabled: false` or remove an entry to stop collecting from it; stored history remains.
There is no fixed company limit. Searches run on demand; Job Hunter does not submit applications.

## Setup

```bash
uv sync
cp config/candidate_profile.example.yaml config/candidate_profile.yaml
uv run job-hunter doctor
```

Edit `config/candidate_profile.yaml` with your title/domain terms, exclusions, and resume path.

Your resume and filled-in `candidate_profile.yaml` are personal and never committed —
`.gitignore` excludes `config/candidate_profile.yaml` and any `config/*resume*` file except the
checked-in `config/resume.example.md`. Bring your own resume as a `.md` file anywhere under
`config/` matching that pattern (e.g. `config/my_resume.md`) and point `resume_path` at it.

Scoring candidates against your resume needs a local model running in
[LM Studio](https://lmstudio.ai/) (Developer tab > Start Server). Copy
`config/lm_studio.example.yaml` to `config/lm_studio.yaml` (also gitignored) and edit `base_url`
to match your server's address.

## Commands

```bash
uv run job-hunter search
uv run job-hunter search --json --archive [--keyword "ADAS,Robotics"]
uv run job-hunter search --companies tri,toyota --new-only
uv run job-hunter source-status
uv run job-hunter source-test honda
uv run job-hunter db-stats
uv run job-hunter export --format json
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

Searches attempt every enabled source by default; one source's failure doesn't stop the others.
`--new-only` limits output only — collection always observes and persists every job returned.
`--archive` writes to a deterministic `data/searches/{keyword-or-default}_{date}.json`; omitting
`--keyword`/`--search` on the review/radar scripts resolves to the newest archive (see
`docs/SPEC.md` §11 and `docs/skill-split-plan.md` §4 for the full resolution rule).

Nothing is ever deleted automatically — `data/jobs.sqlite3` and `data/profile-diff/`/`data/radar/`
only ever grow. `job-hunter cleanup` (dry-run by default, `--apply` to commit, writing an export of
exactly what it's about to remove first) deletes jobs closed longer than
`retention.closed_job_after_days` and old generated reports past `retention.report_after_days` —
keeping the latest `retention.keep_latest_reports_per_slug` of each regardless of age. All three
are configurable in `config/settings.yaml`; see `docs/SPEC.md` §8.6 and
`docs/retention-cleanup-plan.md` for the full design.

Each radar report row has 👍/🆗/👎 relevance-feedback buttons and a floating "Export Feedback"
button — `apply_radar_feedback.py` ingests the export, `suggest_exclusions.py` turns repeated
"irrelevant" tags into safe `soft_exclude_terms` candidates for `candidate_profile.yaml` (never
auto-applied), and `diff_profile.py` previews any filter-field edit's real effect against every
stored posting before you save it. See `docs/feedback-exclusion-plan.md`/`docs/profile-diff-plan.md`.

After editing `candidate_profile.yaml` (e.g. approving a `suggest_exclusions.py` suggestion),
`refilter_archive.py` rebuilds an existing archive's candidates **with no network/scraper call**
— re-running `render_radar.py` afterward updates the same report in place to reflect the edit.
Use this instead of re-running `search` when you just want an existing report to catch up with a
filter change, not to fetch anything new. It rebuilds from SQLite's current active/eligible job
pool (scoped to that archive's own sources) rather than narrowing whatever's already in the
archive file — this matters for a *loosening* edit (a new `strong_relevance_terms` override, a
removed `exclude_terms`/`soft_exclude_terms` entry): a prior narrowing edit may have already
dropped the job from the archive file entirely, and only a rebuild from SQLite can bring it back.
Every job any run has ever observed stays in SQLite regardless of prefilter outcome, so this is
always possible offline. Prints both directions — `N removed, M gained` — since a single profile
edit can do both at once.

### Regenerating the radar without scraping

To refresh the HTML report from what's already in `data/searches/`/SQLite — e.g. after editing
`candidate_profile.yaml`, or just to re-render — without hitting any employer site, run in order:

```bash
# 1. (optional) rebuild the archive's candidates from SQLite against the current profile — no network call
uv run python scripts/refilter_archive.py [--keyword "ADAS,Robotics"]

# 2. (optional) score any not-yet-assessed candidates — calls your LM Studio endpoint only
uv run python scripts/review_with_lm_studio.py [--keyword "..."] [--status]

# 3. render/update the report
uv run python scripts/render_radar.py [--keyword "..."]
```

Step 1 only matters if the profile changed since this archive was collected; step 2 only matters
if `--status` shows candidates remaining. Both are safe no-ops otherwise. All three resolve to the
same archive by `--keyword` (or the newest archive overall if omitted) — pass the same keyword
through all of them. Step 3 alone is enough for "just re-render what's already there."

## Skills

Five independently-invocable skills, so each stage can be run, checked on, or resumed standalone:

- **`job-scout`** — search (`job-hunter search --archive`)
- **`job-reviewer`** — score candidates against your resume via local LM Studio
- **`job-radar`** — compile + render the report
- **`job-hunter`** — orchestrator that runs all three end to end
- **`job-feedback`** — turn relevance feedback and profile edits into a confirmed profile update

Example invocations (see `docs/SPEC.md` §11.1 for the full set, including exact-path resume and
`--status` progress checks):

```
/job-hunter                          # full pipeline, profile-driven
/job-hunter ADAS                     # full pipeline, keyword-scoped
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

This installs all five skills and prompts interactively for which runtime(s) to install into
(Hermes, Claude Code globally, Claude Code for this repo only, OpenCode, or any combination). To
skip the prompt, pass one or more target flags instead, e.g. `sh scripts/install_skill.sh
--claude-local`, `sh scripts/install_skill.sh --all`. Add `--copy` to create independent copies
instead of symlinks. Run `sh scripts/install_skill.sh --help` for the full flag list.

For Hermes, `sh scripts/install_skill.sh --hermes` also installs the candidate-profile
diff hook under `~/.hermes/agent-hooks/` and registers it in `~/.hermes/config.yaml`
(`HERMES_HOME` overrides this location for both hooks and skills). Installation needs
`uv` and the project's dependencies. The installer preserves existing config values
and hooks, saves the original config as `config.yaml.job-hunter.bak` before rewriting
YAML (comments/formatting may change), and skips an identical registration on reruns.
`--copy` copies the hook too; the command still points to this checkout for the profile,
database, and diff script, so keep the checkout available.

Following the [Hermes shell hook format](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#shell-hooks),
the hook uses `post_tool_call` with `write_file|patch` and reads `tool_input.path`.
Edits to this checkout's `config/candidate_profile.yaml` run the same best-effort
`diff_profile.py` check as `.claude/settings.json`, generating reports in `data/profile-diff/`.
Relative paths are resolved against the event's `cwd`; other profiles are ignored.
Terminal-based edits are not covered. Restart Hermes after installing; its normal
first-use hook approval still applies. Inspect registration with `hermes hooks list`.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
```

Tests use saved response fixtures and do not require internet. Standard runtime dependencies are
`httpx`, `selectolax`, `pydantic`, and `PyYAML` — no browser is installed by default. The optional
`stealth` extra (`uv sync --extra stealth && uv run scrapling install`) adds
[Scrapling](https://github.com/D4Vinci/Scrapling) and a real headless browser, used only by the
`stealth_html` adapter (see `docs/SPEC.md` §5.8).

## Adding a new source

Given a company name, its careers listing URL, and one sample job URL, the `onboard-source`
project skill (`.claude/skills/onboard-source`) discovers the real backing system, wires up (or
writes) an adapter, tests it, verifies it live, and updates this README and `docs/SPEC.md`. See
`docs/SPEC.md` §5.20 for the manual process and `.claude/skills/onboard-source/references/
discovery-playbook.md` for known ATS/platform signatures.

## Keep your search history

Your collected data stays in `data/`, including `jobs.sqlite3`, search archives, cached
assessments, feedback, and reports. Your personal profile and resume live in `config/`.
To move to another machine, stop running collection/review processes and copy both complete
folders into the new checkout. Recreate dependencies with `uv sync` and reinstall agent skills
from the new location. Git alone does not transfer these ignored personal and generated files.

## Explore the design

- [Architecture and functionality](docs/SPEC.md)
- [Agent skills and pipeline stages](docs/skill-split-plan.md)
- [Feedback-driven exclusion suggestions](docs/feedback-exclusion-plan.md)
- [Previewing profile changes](docs/profile-diff-plan.md)
