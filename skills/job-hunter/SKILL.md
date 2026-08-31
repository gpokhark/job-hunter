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
4. If the user invoked this skill with one or more keywords/titles (e.g. `/job-hunter ADAS`,
   `/job-hunter ADAS or Robotics or "Product Technical Leader"`), normalize them into a
   comma-separated list, preserving multi-word phrases as single entries, and pass it as
   `--keyword`. Run:
   ```bash
   uv run job-hunter search --json --output data/latest_search.json [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   A keyword argument **replaces** the profile's `target_title_terms`/`target_domains` as the
   prefilter's positive-match set for this run only (the profile file itself is untouched) — a
   match requires the keyword to appear in the job's title or department (not the free-text
   description, which is too noisy for gating — see `CLAUDE.md`'s `prefilter.py` notes).
   `exclude_title_terms`, `exclude_terms`, U.S. eligibility, and the recency window still apply on
   top of a keyword search; it only ever narrows further, never bypasses those. There is no
   candidate cap by default (with or without a keyword) — tell the user the match count from
   `prefilter_candidates` before running the local-LLM review in step 7, since a broad keyword can
   still match a sizeable pool and each job is reviewed strictly sequentially.
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
   reviewed by default — review all of them. Only pass `--limit` if the user explicitly asks to
   review fewer than all eligible candidates this run. Report to the user how many were newly
   reviewed versus already cached from a prior run. No Claude/agent tokens are spent scoring
   anything; the only LLM involved in this step is the local one running in LM Studio.
8. Run `uv run python scripts/assessments_to_csv.py` for a human-readable
   `data/assessments.csv`, and read `data/assessments.json` (or the `assessments` table via
   `uv run job-hunter export-assessments`) for the verdicts to compile from.
9. Compile the final list from those verdicts — **there is no cap on how many are shown**
   (`--limit` in step 7 governs how many get *reviewed*, if the user asked for fewer than all of
   them; it has no bearing on how many scored jobs get reported here). Include every candidate
   scoring **50 or above**, split into two score-descending groups, each clearly labeled:
   - **Strong matches (score ≥ 75)** — the primary list.
   - **For review (score 50–74)** — a secondary, lower-confidence list shown separately, not
     folded into the primary one.
   State the count of jobs scoring below 50 that were excluded from the list (don't list them
   individually). Within the Strong matches group, tag each job **[90+]** if its score is 90 or
   above, or **[80+]** if it's 80–89, so the strongest candidates are scannable at a glance —
   scores 75–79 carry no tag. Independently of score, tag any job **[New]** if its `posted_at`
   falls within the last 10 days (compute this from today's date vs. `posted_at`; a job with no
   `posted_at` cannot be tagged `[New]` — do not guess). This posting-recency flag is unrelated
   to the candidate's `is_new`/`is_changed` fields, which only mean "not previously seen by this
   tool" and say nothing about how recently the job was actually posted — do not conflate them.
   Include strengths, gaps, first-party URL, and posting date for every listed job.
10. Do not invent salary, sponsorship, arrangement, qualifications, or posting dates — you may
    only relay what the job record, the resume, or a recorded verdict actually contains.
11. If nothing scores 50 or above, say so plainly — do not lower the floor to manufacture
    results. A candidate that was never reviewed (an LM Studio error skipped it, or the user
    explicitly capped `--limit` — check the script's stderr output) must never appear in either
    group — say it wasn't reviewed, don't imply it was scored.

Onsite, hybrid, and remote jobs are acceptable. Remote jobs require explicit U.S. eligibility evidence.
The collector owns retrieval and location filtering. Do not substitute broad web searches for adapter failures unless the user requests that fallback.

