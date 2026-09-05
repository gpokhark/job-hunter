---
name: job-hunter
description: Run the full job-hunter pipeline — search, local-LLM review, and radar report — end to end for a keyword/title search or the candidate profile's standing criteria.
---

# Job Hunter (orchestrator)

Use this skill only when the user explicitly asks to search or evaluate current jobs **and wants
the complete pipeline run end to end**. For a single stage — "just re-render the radar," "resume
reviewing ADAS," "check what's failing on Ford" — use `job-scout` / `job-reviewer` / `job-radar`
directly instead; each is the canonical, standalone procedure for its own stage, and this skill
only sequences the same underlying commands they document. (This skill runs those commands
directly rather than invoking the other three skills as sub-calls, since cross-runtime support for
one skill invoking another isn't guaranteed across every agent runtime this project installs
into.)

## Examples

- `/job-hunter` — full pipeline, profile-driven (no keyword)
- `/job-hunter ADAS` — full pipeline scoped to one keyword
- `/job-hunter ADAS or Robotics or "Product Technical Leader"` — full pipeline, multiple
  keywords/phrases, comma-joined into one `--keyword` under the hood

## Procedure

1. Work from the project directory containing `pyproject.toml`.
2. Read `config/candidate_profile.yaml` (falling back to `candidate_profile.example.yaml`) and the
   configured resume if available. Never invent experience absent from it.
3. If invoked with one or more keywords/titles, normalize them into a comma-separated
   `--keyword` list, preserving multi-word phrases as single entries. Otherwise proceed with no
   keyword (profile-driven default search).
4. **Search** (same command `job-scout` documents):
   ```bash
   uv run job-hunter search --json --archive [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   Capture the printed `Archived to: <path>` and the keyword you used (or "none" for a default
   search) — pass **both forward explicitly** to steps 5 and 7 below. Never let those steps fall
   back to their own no-arg defaults here: this orchestrator always knows exactly which run it
   just started, so there's no reason to risk resolving to a different, unrelated archive.
   Report any failed/unsupported sources while continuing with successful ones; report the
   `prefilter_candidates` count before review.
5. **Review** (same command `job-reviewer` documents), with the same keyword from step 4:
   ```bash
   uv run python scripts/review_with_lm_studio.py [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   **Scoring happens outside this agent entirely, on a local model — you never score a job
   yourself.** No cap by default; pass `--limit` only if the user explicitly asked for fewer than
   all eligible candidates. It's fully resumable — see `job-reviewer`'s `SKILL.md` for why an
   interrupted run just needs re-invoking with the same keyword, never a full restart from step 4.
   Report how many were newly reviewed versus already cached.
6. Read `data/assessments.json` (or `uv run job-hunter export-assessments`) for the verdicts, and
   run `uv run python scripts/assessments_to_csv.py` for a human-readable `data/assessments.csv`.
7. Compile the final list from those verdicts — **no cap on how many are shown**. Include every
   candidate scoring **50 or above**, split into two score-descending groups, each clearly
   labeled:
   - **Strong matches (score ≥ 75)** — the primary list.
   - **For review (score 50–74)** — a secondary, lower-confidence list, shown separately.
   State the count scoring below 50 that were excluded (don't list individually). Within Strong
   matches, tag **[90+]** (≥90) or **[80+]** (80–89); 75–79 carries no tag. Independently of score,
   tag any job **[New]** if `posted_at` falls within the last 10 days — a job with no `posted_at`
   cannot be tagged `[New]`. This is unrelated to `is_new`/`is_changed` (collection novelty, not
   posting recency) — do not conflate them. Include strengths, gaps, first-party URL, posting
   date, and sponsorship stance (only when explicitly stated) for every listed job.
8. **Render** (same command `job-radar` documents), with the same keyword from step 4:
   ```bash
   uv run python scripts/render_radar.py [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   Pure presentation over the exact same data as step 7 — never re-derives a score. Prints
   `Wrote <path> | strong=N review=N below_50=N never_reviewed=N`; sanity-check against what you
   compiled. If your runtime can publish artifacts, publish/update the same link for a same-day
   rerun of the same keyword; otherwise give the user the local file path.
9. Do not invent salary, sponsorship, arrangement, qualifications, or posting dates. If nothing
   scores 50 or above, say so plainly — do not lower the floor to manufacture results. A candidate
   never reviewed (an LM Studio error skipped it, or `--limit` capped it) must never appear in
   either group.
10. This run's archive (step 4) is permanent — the same keyword/day combination stays resumable
    or re-renderable later via `job-reviewer`/`job-radar` directly, standalone, without rerunning
    this entire orchestrator.

Onsite, hybrid, and remote jobs are all acceptable — remote jobs require explicit U.S. eligibility
evidence, already enforced by the collector. Do not substitute broad web searches for adapter
failures unless the user explicitly requests that fallback.
