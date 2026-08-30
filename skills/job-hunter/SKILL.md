---
name: job-hunter
description: Search configured employer career sites for current U.S.-eligible jobs and rank strong matches against the user's resume or candidate profile.
---

# Job Hunter

Use this skill only when the user explicitly asks to search or evaluate current jobs.

## Procedure

1. Work from the project directory containing `pyproject.toml`.
2. Read `config/candidate_profile.yaml`, falling back to `candidate_profile.example.yaml` only if needed.
3. Read the configured resume if it is available. Never invent experience absent from it.
4. Run `uv run job-hunter search --json --output data/latest_search.json`.
5. Read `data/latest_search.json`; report failed sources while continuing with successful sources.
6. Filter to `candidates` with `us_eligible=true` only — never send an ineligible job to review.
   The collector has already deterministically excluded postings older than
   `search.max_posting_age_days` (default 30) using each job's `posted_at`; a job with no
   `posted_at` was kept because its age could not be determined, not because it is known-recent —
   do not claim a specific age for it.
7. **Scoring happens outside this agent entirely, on a local model — you never score a job
   yourself.** Run:
   ```bash
   uv run python scripts/review_with_lm_studio.py
   ```
   This is a plain deterministic script (see its docstring for one-time LM Studio setup), not a
   sub-agent: it reads `data/latest_search.json`, skips any candidate whose `prior_assessment`
   already matches its current `content_hash` (a job already reviewed and unchanged since — zero
   added cost), then sends **every remaining eligible candidate** to the local model **one at a
   time, strictly sequentially**, persisting each verdict immediately (SQLite plus
   `data/assessments.json`) as it goes, so an interrupted run still keeps what it reviewed and a
   later run never redoes a job it already covered. It does **not** cap how many jobs get
   reviewed by default — review all of them; `recommendation.max_results` only governs how many
   of the *reviewed* jobs get recommended in step 9, not how many get reviewed here. Only pass
   `--limit` if the user explicitly asks to review fewer than all eligible candidates this run.
   Report to the user how many were newly reviewed versus already cached from a prior run. No
   Claude/agent tokens are spent scoring anything; the only LLM involved in this step is the
   local one running in LM Studio.
8. Run `uv run python scripts/assessments_to_csv.py` for a human-readable
   `data/assessments.csv`, and read `data/assessments.json` (or the `assessments` table via
   `uv run job-hunter export-assessments`) for the verdicts to compile from.
9. Compile the final list from those verdicts: recommend only jobs meeting the configured
   threshold, sorted by score, **capped at `recommendation.max_results`** (10 by default — this
   is where that setting actually applies, not at review time), with strengths, gaps,
   first-party URLs, and the posting date (`posted_at`) when present on the candidate.
10. Do not invent salary, sponsorship, arrangement, qualifications, or posting dates — you may
    only relay what the job record, the resume, or a recorded verdict actually contains.
11. If nothing meets the threshold, say so. At most three candidates within five points of the
    threshold (not the cap) may be labeled near-matches. A candidate that was never reviewed (an
    LM Studio error skipped it, or the user explicitly capped `--limit` — check the script's
    stderr output) is not a near-match — say it wasn't reviewed, don't imply it was scored.

Onsite, hybrid, and remote jobs are acceptable. Remote jobs require explicit U.S. eligibility evidence.
The collector owns retrieval and location filtering. Do not substitute broad web searches for adapter failures unless the user requests that fallback.

