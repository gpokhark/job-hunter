---
name: job-reviewer
description: Score job-hunter's archived candidates against the user's resume using a local LLM (LM Studio) — resumes automatically from wherever a prior run left off, never spends cloud/agent tokens.
---

# Job Reviewer

Use this skill when the user wants candidates scored/reviewed against their resume — whether
that's starting a review, checking progress, or resuming one that was interrupted. For the full
search→review→radar pipeline in one go, use the `job-hunter` orchestrator skill instead; it calls
this same command.

**Scoring happens outside this agent entirely, on a local model — you never score a job
yourself.** `scripts/review_with_lm_studio.py` is a plain deterministic script (see its docstring
for one-time LM Studio setup), not a sub-agent.

## Examples

- `/job-reviewer --keyword ADAS` — start (or resume) reviewing the newest ADAS archive
- `/job-reviewer --keyword ADAS` (re-invoked after an interruption) — resumes automatically,
  already-scored jobs are skipped, never restarts from scratch
- `/job-reviewer --status --keyword ADAS` — check remaining/cached/total counts, no model calls
- `/job-reviewer --search data/searches/adas_2026-08-20.json` — review one specific historical
  archive by exact path, bypassing keyword resolution
- `/job-reviewer` — cold start, resolves to the newest archive of any keyword (not a resume
  guarantee — pass `--keyword` whenever you already know it)

## Procedure

1. Work from the project directory containing `pyproject.toml`.
2. Decide which archive to review:
   - **If you already know the keyword** (e.g. you — or the orchestrator step before you — just
     ran `job-scout` with a specific keyword), pass `--keyword` explicitly. Never rely on the
     no-arg default when the keyword is already known: it resolves to whichever archive is
     *newest overall*, which silently changes if any other search has run since — you could end
     up resuming the wrong job. See `docs/skill-split-plan.md` section 4 for the full rationale.
   - If invoked cold with no known keyword, omitting both `--keyword` and `--input` resolves to
     the newest archive of any keyword — a genuine "I don't know/care which run" convenience.
   - To review one specific historical run regardless of what's newest, pass `--keyword "..."` (
     resolves to the newest archive for that keyword's slug) or `--input <exact path>`.
3. Optionally check progress first, with no model calls and no changes made:
   ```bash
   uv run python scripts/review_with_lm_studio.py --status [--keyword "..."]
   ```
   Report the remaining/cached/total counts to the user before committing to a full run if it's
   likely to take a while.
4. Run the review:
   ```bash
   uv run python scripts/review_with_lm_studio.py [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   Sends every not-yet-cached U.S.-eligible candidate to the local model **one at a time, strictly
   sequentially**, persisting each verdict immediately (SQLite plus `data/assessments.json`) as it
   goes. **This makes it fully resumable with zero extra steps**: if the run is interrupted for
   any reason (network blip, LM Studio error, the calling agent's own process/turn getting killed
   — this can happen on Hermes with a local agent under a long-running call), simply re-invoke
   this skill with the **same `--keyword`/`--input`**. Already-scored jobs are skipped
   automatically (matched by `content_hash`, not re-sent to the model), so a resumed run only
   processes what's actually left — never redo the whole thing, never lose partial progress.
   It does **not** cap how many jobs get reviewed by default — review all of them. Only pass
   `--limit` if the user explicitly asks to review fewer than all eligible candidates this run.
5. Report to the user how many were newly reviewed versus already cached from a prior run (the
   script's final line: `Reviewed N job(s); skipped M already-assessed (unchanged) job(s).`). No
   Claude/agent tokens are spent scoring anything — the only LLM involved in this step is the
   local one running in LM Studio.
6. Resume changes never affect this step's caching: a job already assessed stays cached
   regardless of resume edits (cache validity is keyed on the job's `content_hash` only), while
   any job actually sent to the model this run is always scored against whatever resume is on
   disk right now. This is intentional — see `docs/skill-split-plan.md` section 5 — not something
   to "fix" by adding a resume-change trigger.
7. Run `uv run python scripts/assessments_to_csv.py` for a human-readable `data/assessments.csv`.
8. This skill's job ends here — it does not render anything. To get a report (text summary and/or
   HTML radar), invoke `job-radar`, passing the same `--keyword` you used here. If compiling a
   text summary yourself instead of delegating to `job-radar`, read `data/assessments.json` (or
   `uv run job-hunter export-assessments`) for the verdicts to compile from — never invent a
   score, match, or gap not actually present in a recorded assessment.
