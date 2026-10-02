---
name: job-scout
version: 1.3.0
description: Search configured employer career sites for current U.S.-eligible jobs matching a keyword/title or the candidate profile, and archive the results for review.
compatibility: Requires uv and Python 3.11+. No LM Studio dependency — this stage only searches and archives, it never scores anything.
metadata:
  job_hunter:
    stage: search
  hermes:
    tags: [jobs, search, scraping]
---

# Job Scout

Use this skill when the user wants to search — with or without a keyword — but is not
necessarily asking for review/scoring in the same breath. For the full search→review→radar
pipeline in one go, use the `job-hunter` orchestrator skill instead; it calls this same command.

## Examples

- `/job-scout` — profile-driven search, no keyword
- `/job-scout Analytics` — keyword-scoped search
- `/job-scout Analytics or "Data Engineer" or "Product Manager"` — multiple keywords/phrases

## Contract

Input:
- project path (`--project`, defaults to `$JOB_HUNTER_ROOT`/cwd)
- keyword(s) (optional, comma-joined `--keyword`; defaults to the profile's
  `target_title_terms`/`target_domains`)
- optional `--companies` (scope to specific sources instead of every enabled one)

Output:
- an archived candidate bundle at `data/searches/{slug}_{date}.json` (printed as `Archived to:
  <path>`) — the same keyword/day combination overwrites its own file, a different one gets its own
- summary counts (`prefilter_candidates`, total jobs seen)
- per-source health (`source_health`: ok/failed/unsupported/warning) — one source's failure never
  invalidates the rest
- next command: `job-reviewer --keyword <same>` to score these candidates, then `job-radar
  --keyword <same>` to render them

## Procedure

1. Every command below takes `--project "$CLAUDE_PROJECT_DIR"` (Claude Code) — or the equivalent
   workspace path for another runtime, e.g. Hermes — so this skill works regardless of whether the
   calling process already `cd`'d into the repo.
2. If invoked with one or more keywords/titles (e.g. `/job-scout Analytics`, `/job-scout Analytics or
   "Data Engineer" or "Product Manager"`), normalize them into a comma-separated list,
   preserving multi-word phrases as single entries, and pass it as `--keyword`. Otherwise omit
   `--keyword` entirely — the search then falls back to `config/candidate_profile.yaml`'s
   `target_title_terms`/`target_domains` (or `candidate_profile.example.yaml` if the real file
   doesn't exist).
3. Run:
   ```bash
   uv run job-hunter search --project "$CLAUDE_PROJECT_DIR" --json --archive [--keyword "Analytics,Data Engineer,Product Manager"]
   ```
   `--archive` writes to `data/searches/{slug}_{date}.json` — the same keyword on the same day
   overwrites (refreshing today's answer), a new day or a different keyword always gets its own
   file. See `search_archive.py`'s own docstring for the full naming rule. The command prints
   `Archived to: <path>`.
4. Report to the user:
   - The archived path and the keyword used (or "profile-driven default search" if none).
   - Any failed/unsupported sources from `source_health`, while noting successful sources still
     returned candidates — one source's failure never invalidates the rest. See
     `references/troubleshooting.md` if a source is failing and needs diagnosis.
   - The `prefilter_candidates` count from `summary` — every job in `candidates` has already
     passed the U.S.-eligibility gate, the title/department relevance gate, and the recency
     window (`max_posting_age_days`, default 30); nothing further to filter before review.
5. This skill's job ends here — it does not score or render anything. To review these candidates,
   invoke `job-reviewer`, passing the **same `--keyword`** you used here (or omitting it if you
   ran a default search) so it resolves to exactly this archive rather than "whichever archive is
   newest," which could be a different run by the time review actually happens. To render/update
   the HTML report for this run, invoke `job-radar` the same way.
6. Because every keyword+day combination gets its own permanent archive file, running this again
   later with a different keyword — or the same keyword on a different day — never overwrites or
   loses an earlier run. Any prior archive stays reachable by keyword: `uv run job-hunter
   resolve-search --project "$CLAUDE_PROJECT_DIR" --keyword "..."` prints its path, and
   `job-reviewer`/`job-radar` accept the same `--keyword` to resolve it directly.

Onsite, hybrid, and remote jobs are all acceptable output — remote jobs require explicit U.S.
eligibility evidence, already enforced by the collector. Do not substitute broad web searches for
adapter failures unless the user explicitly requests that fallback.

## Background collection

For a long or rate-limited sweep, collect in the background instead of waiting on `search`:

- Start: `uv run job-hunter collect start --project "$CLAUDE_PROJECT_DIR" [--companies a,b] [--slow]`
  returns immediately. `--slow` fetches one source at a time with a delay between them.
- Poll: `uv run job-hunter collect status --project "$CLAUDE_PROJECT_DIR"` shows progress and any
  rate-limited, timed-out or failed sources. `collect stop` ends it cooperatively (in-flight sources
  finish; no archive is written).
- Build an archive any time from what has been collected so far:
  `uv run job-hunter snapshot --project "$CLAUDE_PROJECT_DIR"`, then run the command it prints,
  `job-hunter pipeline --no-scrape --search <path> [--review]`.
- A completed run writes the normal archive itself.
- Do not run `cleanup --apply` while a collector is running; it refuses.

## Spotting vocabulary gaps

Optional, after a scrape: `uv run job-hunter near-misses --project "$CLAUDE_PROJECT_DIR"` lists
rejected-but-relevant-looking jobs new since the last scan. It is a human scouting aid (not scored, not in the
radar); its vocabulary hints feed the job-feedback flow.
