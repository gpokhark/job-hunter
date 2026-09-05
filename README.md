# Job Hunter

Job Hunter is a manual Python collector for employer career sites. It normalizes postings,
applies a strict U.S.-eligibility filter, persists history in SQLite, and emits a compact JSON
candidate bundle for an LLM agent to compare with a resume. It does not schedule searches or
apply to jobs.

For the full architecture, adapter internals, per-source status/caveats, filtering pipeline, data
model, and skill design, see **[`docs/SPEC.md`](docs/SPEC.md)** (functionality spec) and
**[`docs/skill-split-plan.md`](docs/skill-split-plan.md)** (skill design). This file covers setup,
commands, and installation only.

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
uv run job-hunter record-assessment --payload '{"source_key": "tri", "job_id": "...", "company": "...", "title": "...", "url": "...", "score": 82, "recommended": true, "matches": ["..."], "gaps": ["..."]}'
uv run python scripts/review_with_lm_studio.py [--keyword "..."] [--status]
uv run python scripts/render_radar.py [--keyword "..."]
uv run python scripts/assessments_to_csv.py
```

Searches attempt every enabled source by default; one source's failure doesn't stop the others.
`--new-only` limits output only — collection always observes and persists every job returned.
`--archive` writes to a deterministic `data/searches/{keyword-or-default}_{date}.json`; omitting
`--keyword`/`--search` on the review/radar scripts resolves to the newest archive (see
`docs/SPEC.md` §11 and `docs/skill-split-plan.md` §4 for the full resolution rule).

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
