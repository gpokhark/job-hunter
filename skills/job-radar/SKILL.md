---
name: job-radar
description: Compile job-hunter's scored candidates into a two-tier report (text summary and/or HTML radar page) — reflects review progress so far, safe to re-run at any time including mid-review.
---

# Job Radar

Use this skill when the user wants the reviewed candidates presented — as a text summary, an HTML
report, or both — whether review just finished, is still in progress, or finished a while ago. For
the full search→review→radar pipeline in one go, use the `job-hunter` orchestrator skill instead;
it calls this same command.

This is pure presentation: it never re-derives, adjusts, or overrides a score. Every number comes
from `data/assessments.json` as it stands right now.

## Examples

- `/job-radar --keyword ADAS` — render/update the report for that keyword, any time (including
  mid-review — always reflects exactly what's been reviewed so far)
- `/job-radar --search data/searches/adas_2026-08-20.json` — render one specific historical
  archive by exact path, bypassing keyword resolution
- `/job-radar` — cold start, resolves to the newest archive of any keyword

## Procedure

1. Work from the project directory containing `pyproject.toml`.
2. Decide which archive to render — the same resolution rule as `job-reviewer`: if you already
   know the keyword, pass `--keyword` explicitly; the no-arg default (newest archive overall) is a
   cold-start convenience only, not a substitute for a known keyword. See
   `docs/skill-split-plan.md` section 4.
3. If compiling a text summary yourself (not just the HTML report), read the resolved archive's
   `candidates` plus `data/assessments.json` — or run `uv run job-hunter resolve-search
   [--keyword "..."]` first to get the exact archive path, then read both files directly.
4. Compile the final list from verdicts — **no cap on how many are shown**. Include every
   candidate scoring **50 or above**, split into two score-descending groups, each clearly
   labeled:
   - **Strong matches (score ≥ 75)** — the primary list.
   - **For review (score 50–74)** — a secondary, lower-confidence list, shown separately.
   State the count scoring below 50 that were excluded (don't list them individually). Within
   Strong matches, tag **[90+]** (score ≥ 90) or **[80+]** (80–89); 75–79 carries no tag.
   Independently of score, tag any job **[New]** if `posted_at` falls within the last 10 days — a
   job with no `posted_at` cannot be tagged `[New]`, do not guess. This posting-recency flag is
   unrelated to `is_new`/`is_changed`, which only mean "not previously seen by this tool," not
   "recently posted" — do not conflate them. Include strengths, gaps, first-party URL, and posting
   date for every listed job. Mention visa-sponsorship stance when explicitly stated
   (`available`/`not_available`) — never invent one for a posting that doesn't mention it.
5. Render the standalone HTML report:
   ```bash
   uv run python scripts/render_radar.py [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   It prints `Wrote <path> | strong=N review=N below_50=N never_reviewed=N`; sanity-check those
   counts against what you just compiled. A candidate `job-reviewer` hasn't gotten to yet is
   counted in `never_reviewed` and never listed in either group — say it wasn't reviewed, don't
   imply a score for it.
6. **Safe to re-run at any point, including mid-review** — it reflects exactly whatever's been
   reviewed so far each time it runs, nothing cached or stale. Re-running against the same archive
   always writes to the same output path (`data/radar/{same-stem}.html`), so "update the radar" is
   just "call this again" — no separate sync/refresh mechanism needed.
7. If your runtime has an artifact-publishing capability (e.g. Claude Code's Artifact tool),
   publish the rendered file — load whatever design-guidance skill that capability requires first.
   A same-day rerun of the same keyword should update the *same* published link (pass its existing
   `url` if your runtime distinguishes create vs. update); a new day or a different keyword gets
   its own new link, mirroring the archive's own naming. Never publish over the *wrong* keyword's
   report — check what a link was for before reusing it.
8. If your runtime has no such capability, tell the user the local file path (e.g.
   `data/radar/product-manager_2026-09-03.html`) so they can open it directly.
9. Do not invent salary, sponsorship, arrangement, qualifications, or posting dates — you may only
   relay what the job record, the resume, or a recorded verdict actually contains. If nothing
   scores 50 or above, say so plainly — do not lower the floor to manufacture results.
