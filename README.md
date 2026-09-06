# Job Hunter

Job Hunter is a manual Python collector for employer career sites. It normalizes postings,
applies a strict U.S.-eligibility filter, persists history in SQLite, and emits a compact JSON
candidate bundle for an LLM agent to compare with a resume. It does not schedule searches or
apply to jobs.

For the full architecture, adapter internals, per-source status/caveats, filtering pipeline, data
model, and skill design, see **[`docs/SPEC.md`](docs/SPEC.md)** (functionality spec),
**[`docs/skill-split-plan.md`](docs/skill-split-plan.md)** (skill design),
**[`docs/feedback-exclusion-plan.md`](docs/feedback-exclusion-plan.md)** (radar click-feedback →
safe exclusion terms), and **[`docs/profile-diff-plan.md`](docs/profile-diff-plan.md)** (preview a
filter edit's effect before saving it). This file covers setup, commands, and installation only.

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

# 2. (optional) score any not-yet-assessed candidates — hits local LM Studio only, never the network
uv run python scripts/review_with_lm_studio.py [--keyword "..."] [--status]

# 3. render/update the report
uv run python scripts/render_radar.py [--keyword "..."]
```

Step 1 only matters if the profile changed since this archive was collected; step 2 only matters
if `--status` shows candidates remaining. Both are safe no-ops otherwise. All three resolve to the
same archive by `--keyword` (or the newest archive overall if omitted) — pass the same keyword
through all of them. Step 3 alone is enough for "just re-render what's already there."

## Skills

Four independently-invocable skills, so each stage can be run, checked on, or resumed standalone:

- **`job-scout`** — search (`job-hunter search --archive`)
- **`job-reviewer`** — score candidates against your resume via local LM Studio
- **`job-radar`** — compile + render the report
- **`job-hunter`** — orchestrator that runs all three end to end

Example invocations (see `docs/SPEC.md` §11.1 for the full set, including exact-path resume and
`--status` progress checks):

```
/job-hunter                          # full pipeline, profile-driven
/job-hunter ADAS                     # full pipeline, keyword-scoped
/job-scout ADAS                      # search only
/job-reviewer --keyword ADAS         # start or resume review for that keyword — re-invoking
                                      #   this exact command after an interruption just continues
/job-radar --keyword ADAS            # render/update the report — safe to re-run any time,
                                      #   including mid-review
/job-radar --keyword ADAS --refilter # re-apply candidate_profile.yaml to an already-collected
                                      #   archive and re-render — no scraper call
```

Install with:

```bash
sh scripts/install_skill.sh
```

This installs all four skills and prompts interactively for which runtime(s) to install into
(Hermes, Claude Code globally, Claude Code for this repo only, OpenCode, or any combination). To
skip the prompt, pass one or more target flags instead, e.g. `sh scripts/install_skill.sh
--claude-local`, `sh scripts/install_skill.sh --all`. Add `--copy` to create independent copies
instead of symlinks. Run `sh scripts/install_skill.sh --help` for the full flag list.

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
`docs/SPEC.md` §5.11 for the manual process and `.claude/skills/onboard-source/references/
discovery-playbook.md` for known ATS/platform signatures.
