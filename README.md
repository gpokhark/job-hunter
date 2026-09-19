# Job Hunter

**Your next job deserves a smarter search—not another thousand tabs.**
Job Hunter turns employer career pages into a focused shortlist using deterministic filters
and local AI resume scoring. Run it with **Hermes Agent** or directly from the CLI.

- **Collect:** 62 companies with implemented adapters. Add, disable, or remove sources.
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

**Refresh only what you need.** Assessment caching keys on the job's content hash only — by
design, never on your resume, evaluation model, or rubric — so changing any of those never forces
a re-review of jobs that haven't changed. Run the review script with `--force` when you actually
want a full re-review after such a change. Use `job-hunter search --refresh-details` when you want
to fetch stored descriptions again.

## Company coverage

The [registry](config/companies.yaml) contains **65 companies: 62 with implemented adapters
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
uv run job-hunter pipeline                              # search -> review -> radar, one command
uv run job-hunter pipeline --keyword "ADAS,Robotics"
uv run job-hunter pipeline --no-scrape [--review]        # re-filter + re-render, no new scrape
uv run job-hunter pipeline --no-scrape --search <path>   # ...this exact archive, not a resolved one
uv run job-hunter pipeline-status [--run <run-id>]       # poll/inspect a pipeline run's manifest

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

Every command above (and every *operational* `scripts/*.py` entry point — not the diagnostic/
prototype/converter utilities or the hook/installer scripts, which take the project root
positionally via their own runtime's calling convention instead; see `docs/SPEC.md`'s `--project`
section for the exact list) also takes `--project <path>`, falling back to `$JOB_HUNTER_ROOT`,
then the current directory — run job-hunter from any directory, including from inside an agent
whose working directory isn't this checkout, without `cd`-ing in first (works whether the flag
comes before or after the subcommand).

`job-hunter pipeline` bounds each stage (search/review/refilter/radar) to
`pipeline.stage_timeout_seconds` in `config/settings.yaml` (8 hours by default, sized to comfortably
exceed a large sequential local-model review, not as a tight SLA) — a stage that hangs is killed
(its whole process tree, not just the immediate subprocess) and the run finalizes as `timed_out`
rather than hanging indefinitely; set it to `null`/comment it out for no timeout. Two overlapping
pipeline runs (or a `pipeline` run and a `cleanup --apply`/standalone review/refilter) never race
each other's writes to SQLite/the archive/`assessments.json` — the second one gets a clear
`lock_held` status naming the first run's PID and command instead of corrupting shared state.

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
`candidate_profile.yaml` — without hitting any employer site:

```bash
uv run job-hunter pipeline --no-scrape                 # refilter + re-render, review stays cached
uv run job-hunter pipeline --no-scrape --review         # also score whatever's newly surfaced
uv run job-hunter pipeline --no-scrape --search data/searches/default_2026-09-15.json
                                                         # ...this exact archive, skip resolution entirely
```

This resolves the same archive-selection rule as everything else (`--keyword`, or the newest
archive overall if omitted), rebuilds its candidates from SQLite against the current profile,
writes both the updated radar report and a gained/lost diff report, and — unlike plain
`pipeline` mode — leaves review **off** by default, since a profile edit alone doesn't necessarily
warrant spending local-model time; pass `--review` when it does.

"Newest archive overall" is resolved by file modification time, not collection date — and every
refilter touches (re-stamps) whatever archive it targets, so a small archive you've been
iterating on can end up looking "newer" than a much larger one collected more recently. If you
already know exactly which archive you want, skip resolution with `--search <path>` instead of
relying on `--keyword`/no-args resolution. If you're not sure what a resolution will pick,
`job-hunter resolve-search [--keyword "..."]` prints the resolved path on stdout and a one-line
scope summary on stderr (`scope: N sources attempted (...)`) so you can sanity-check it — e.g. a
"default" (profile-driven) archive that only ever queried one company is a sign something's off —
before committing to it.

The equivalent manual three-step version (what `pipeline --no-scrape` runs under the hood, useful
if you want to inspect or skip a step individually) still works the same way it always has:

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

Six independently-invocable skills, so each stage can be run, checked on, or resumed standalone:

- **`job-scout`** — search (`job-hunter search --archive`)
- **`job-reviewer`** — score candidates against your resume via local LM Studio
- **`job-radar`** — compile + render the report
- **`job-hunter`** — orchestrator that runs `job-hunter pipeline` end to end (or `pipeline
  --no-scrape` to re-filter + re-render without a new scrape)
- **`job-feedback`** — turn relevance feedback and profile edits into a confirmed profile update
- **`onboard-source`** — repo-maintenance skill for adding a new employer source to job-hunter
  itself (see "Adding a new source" below); not part of a normal job-search session

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

This installs all six skills and prompts interactively for which runtime(s) to install into
(Hermes, Claude Code globally, Claude Code for this repo only, OpenCode, or any combination). To
skip the prompt, pass one or more target flags instead, e.g. `sh scripts/install_skill.sh
--claude-local`, `sh scripts/install_skill.sh --all`. Add `--copy` to create independent copies
instead of symlinks (`--link` names the default explicitly, if you want to say so). Re-run with
`--update` to replace a stale install (e.g. after moving the repo or a skill), or `--uninstall` to
remove one; `--dry-run` shows what any of the above would do without touching anything. Run
`sh scripts/install_skill.sh --help` for the full flag list.

`--update`/`--uninstall` only ever touch a destination this installer can prove it owns — a
plain-text marker (`.job-hunter-installed`, next to each install) recorded on every real install,
falling back to "does this destination still look like what we'd install" for one made before the
marker existed, so an already-live install keeps working without extra steps. A destination that
matches neither (someone else's directory sitting at the same path) is refused with a clear
message instead of being silently deleted; pass `--force` if you're sure and want to remove/replace
it anyway.

For Hermes, `sh scripts/install_skill.sh --hermes` also installs the candidate-profile
diff hook under `~/.hermes/agent-hooks/` and registers it in `~/.hermes/config.yaml`
(`HERMES_HOME` overrides this location for both hooks and skills). Installation needs
`uv` and the project's dependencies. The installer preserves existing config values
and hooks, saves the original config as `config.yaml.job-hunter.bak` before rewriting
YAML (comments/formatting may change), and skips an identical registration on reruns;
`--uninstall --hermes` unregisters exactly the entry it added, nothing else.
`--copy` copies the hook too; the command still points to this checkout for the profile,
database, and diff script, so keep the checkout available.

Following the [Hermes shell hook format](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#shell-hooks),
the hook uses `post_tool_call` with `write_file|patch` and reads `tool_input.path`. Both this hook
and Claude Code's (`.claude/settings.json`) now delegate to the same shared adapter
(`src/job_hunter/hook_adapter.py`) — each runtime script only parses its own stdin JSON shape and
calls it. Edits to this checkout's `config/candidate_profile.yaml` run the same best-effort
`diff_profile.py` check either way, generating reports in `data/profile-diff/`. `uv` is located
via `shutil.which` rather than assumed on `PATH`, and every failure (uv missing, a non-zero exit,
a timeout, or a skip — see below) is logged to `logs/profile-hook.log` instead of failing
silently. Relative paths are resolved against the event's `cwd`; other profiles are ignored.
Terminal-based edits are not covered. Restart Hermes after installing; its normal first-use hook
approval still applies. Inspect registration with `hermes hooks list`.

A burst of rapid edits touching `candidate_profile.yaml` won't start several overlapping diff
runs — a short lock plus a 5-second debounce window collapse them into one report reflecting the
final state, logged (never silent) whenever a run is skipped for this reason. Claude Code's own
`PostToolUse` command now runs through `scripts/run_profile_hook.sh`, a small portable launcher
that locates `uv` itself (falling back to a bare `python3` with a clear message if that's all
that's available) instead of failing outright if `uv` isn't on the invoking process's `PATH`.
Hermes's own registration in `~/.hermes/config.yaml` is now identified by the hook script's
stable, repo-independent path rather than the full command string (which used to embed this
checkout's absolute path) — re-running the installer after moving/re-cloning the repo replaces
the existing registration instead of appending a second, stale one alongside it, and
`--uninstall --hermes` can find and remove it from the new location too. `job-hunter doctor`
reports if a Hermes registration exists but points at a hook script that no longer exists.

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
project skill (`skills/onboard-source` — installable for every runtime including Hermes via
`install_skill.sh`, same as the other five; `.claude/skills/onboard-source` is a symlink to it)
discovers the real backing system, wires up (or writes) an adapter, tests it, verifies it live,
and updates this README and `docs/SPEC.md`. See `docs/SPEC.md` §5.20 for the manual process and
`skills/onboard-source/references/discovery-playbook.md` for known ATS/platform signatures.

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
- [`job-hunter pipeline`, `--no-scrape`, and the stale-source radar fallback](docs/pipeline-refilter-stale-source-plan.md)
- [Skill frontmatter and the shared Claude/Hermes hook adapter](docs/skill-frontmatter-and-hook-plan.md)
- [Agent-runtime portability audit (`--project`, locking, atomic writes)](docs/agent-runtime-audit.md)
