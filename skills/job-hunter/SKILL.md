---
name: job-hunter
version: 1.3.0
description: Run the full job-hunter pipeline — search, local-LLM review, and radar report — end to end for a keyword/title search or the candidate profile's standing criteria.
compatibility: Requires uv and Python 3.11+; LM Studio required (this orchestrator's review stage delegates to it).
metadata:
  job_hunter:
    stage: orchestrator
  hermes:
    tags: [jobs, resume, pipeline]
---

# Job Hunter (orchestrator)

Use this skill only when the user explicitly asks to search or evaluate current jobs **and wants
the complete pipeline run end to end**. For a single stage — "just re-render the radar," "resume
reviewing ADAS," "check what's failing on Ford" — use `job-scout` / `job-reviewer` / `job-radar`
directly instead; each is the canonical, standalone procedure for its own stage.

This skill is a thin wrapper around `job-hunter pipeline` (`pipeline.py`), the Python-owned
command that actually sequences search → review → radar and writes a durable
`data/runs/<run_id>/manifest.json` at every stage — an agent (or a human) can poll
`job-hunter pipeline-status` afterward instead of re-parsing three separate commands' stdout. This
skill calls `pipeline`/`pipeline-status` directly rather than invoking `job-scout`/`job-reviewer`/
`job-radar` as sub-calls, since cross-runtime support for one skill invoking another isn't
guaranteed across every agent runtime this project installs into.

## Examples

- `/job-hunter` — full pipeline, profile-driven (no keyword)
- `/job-hunter ADAS` — full pipeline scoped to one keyword
- `/job-hunter ADAS or Robotics or "Product Technical Leader"` — full pipeline, multiple
  keywords/phrases, comma-joined into one `--keyword` under the hood
- `/job-hunter --no-scrape` — you (or the user) just edited `candidate_profile.yaml` and want the
  report to reflect it against jobs already collected, with no new scrape; add `--review` to also
  score whatever the refilter surfaces as new/changed (review defaults **off** in this mode)

## Contract

Input:
- project path (`--project`, defaults to `$JOB_HUNTER_ROOT`/cwd)
- keyword(s) (optional, comma-joined `--keyword`; defaults to the profile-driven search)
- mode: normal (a fresh live search) or `--no-scrape` (refilter an already-resolved archive
  against SQLite + the current profile, no network), with `--no-scrape`'s own optional `--review`
- optional `--skip-review`/`--skip-radar`/`--limit`/`--refresh-details`/`--max-candidates`/
  `--new-only` (normal mode only — rejected together with `--no-scrape`, see `pipeline.py`)

Output:
- a durable run manifest (`data/runs/<run_id>/manifest.json`), read back via `job-hunter
  pipeline-status --project ...`
- `status`: `complete` / `partial` / `no_candidates` / `model_unavailable` / `failed`
- counts: `candidates`, and once review ran, `reviewed`/`skipped_cached`/`failed` — plus, for a
  `--no-scrape` run, `gained`/`lost`/`diff_report` from the refilter stage
- the run's `archive` path and (unless skipped) `radar` report path
- next command: none required — this orchestrator already ran review and radar; `job-reviewer`/
  `job-radar` remain available standalone against the same archive/keyword later

## Procedure

1. Every command below takes `--project "$CLAUDE_PROJECT_DIR"` (Claude Code) — or the equivalent
   workspace path for another runtime, e.g. Hermes — so this skill works regardless of whether the
   calling process already `cd`'d into the repo; you never need to `cd` there yourself first.
2. Read `config/candidate_profile.yaml` (falling back to `candidate_profile.example.yaml`) and the
   configured resume if available. Never invent experience absent from it.
3. If invoked with one or more keywords/titles, normalize them into a comma-separated
   `--keyword` list, preserving multi-word phrases as single entries. Otherwise proceed with no
   keyword (profile-driven default search).
4. Decide the mode:
   - **Normal (default) — a fresh live search:**
     ```bash
     uv run job-hunter pipeline --project "$CLAUDE_PROJECT_DIR" [--keyword "ADAS,Robotics,Product Technical Leader"]
     ```
     Runs search → review → radar end to end. `--skip-review`/`--skip-radar` stop the run early if
     the user only wants a subset; `--limit` caps *new* reviews this run — only pass it if the user
     explicitly asked for fewer than all eligible candidates.
   - **`--no-scrape` — re-filter only, no new scrape.** Use when the user says they just edited
     `candidate_profile.yaml` (by hand, or via a `job-feedback`-suggested change) and wants the
     report to reflect it against jobs already collected, without waiting for (or risking rate
     limits from) a fresh search:
     ```bash
     uv run job-hunter pipeline --project "$CLAUDE_PROJECT_DIR" --no-scrape [--review] [--keyword "..."]
     ```
     Re-evaluates the resolved archive's already-collected, still-active SQLite job pool against
     the *current* profile — no network, no adapter calls. Review defaults **off** here (the
     opposite default from normal mode's `--skip-review` opt-out — flagged explicitly since it's a
     real, if small, asymmetry) — pass `--review` to also score whatever the refilter surfaced as
     new/changed. `--companies` is rejected together with `--no-scrape`: refiltering re-evaluates
     an archive's own already-attempted source scope, not a fresh company selection.
5. Poll/confirm the outcome:
   ```bash
   uv run job-hunter pipeline-status --project "$CLAUDE_PROJECT_DIR"
   ```
   Report `status` plainly: `complete`/`partial` both succeeded (`partial` means some individual
   reviews failed — name how many); `no_candidates` means say so and stop, don't manufacture
   results; `model_unavailable` means LM Studio wasn't reachable — before reporting that to the
   user as "the server is down," run `uv run python scripts/check_lm_studio.py --project
   "$CLAUDE_PROJECT_DIR"` (see `job-reviewer`'s `SKILL.md` step 4) — a runtime's own network path
   to `base_url` can be broken while LM Studio itself runs fine, confirmed live with Hermes, and
   that's a different problem to report than the server actually being down; `failed` means
   relay `error` verbatim. Report `candidates`, and — once review ran — `reviewed`/
   `skipped_cached`/`failed`; for a `--no-scrape` run, also report `gained`/`lost`/`diff_report`
   from the refilter stage (the same vocabulary `refilter_archive.py`'s own HTML diff report uses).
6. Compile and present the results exactly as `job-radar`'s `SKILL.md` describes it — its step 5
   (tiering into Strong/For-review, `[90+]`/`[80+]`/`[New]` tags) and step 10 (never invent salary,
   sponsorship, arrangement, qualifications, or posting dates) apply here unchanged; this skill
   does not restate that procedure. Also relay `job-radar` step 6's Collection Issues note for any
   non-`ok` source, including its stale-source fallback note for a source that failed to scrape
   this run — a merged-in job there is real but not freshly re-verified today.
7. This run's archive and radar report (from `pipeline-status`'s `archive`/`radar` fields) are
   permanent — the same keyword/day combination stays resumable or re-renderable later via
   `job-reviewer`/`job-radar` directly, standalone, without rerunning this whole orchestrator.

Onsite, hybrid, and remote jobs are all acceptable — remote jobs require explicit U.S. eligibility
evidence, already enforced by the collector. Do not substitute broad web searches for adapter
failures unless the user explicitly requests that fallback.
