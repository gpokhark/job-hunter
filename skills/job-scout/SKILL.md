---
name: job-scout
description: Search configured employer career sites for current U.S.-eligible jobs matching a keyword/title or the candidate profile, and archive the results for review.
---

# Job Scout

Use this skill when the user wants to search — with or without a keyword — but is not
necessarily asking for review/scoring in the same breath. For the full search→review→radar
pipeline in one go, use the `job-hunter` orchestrator skill instead; it calls this same command.

## Examples

- `/job-scout` — profile-driven search, no keyword
- `/job-scout ADAS` — keyword-scoped search
- `/job-scout ADAS or Robotics or "Product Technical Leader"` — multiple keywords/phrases

## Procedure

1. Work from the project directory containing `pyproject.toml`.
2. If invoked with one or more keywords/titles (e.g. `/job-scout ADAS`, `/job-scout ADAS or
   Robotics or "Product Technical Leader"`), normalize them into a comma-separated list,
   preserving multi-word phrases as single entries, and pass it as `--keyword`. Otherwise omit
   `--keyword` entirely — the search then falls back to `config/candidate_profile.yaml`'s
   `target_title_terms`/`target_domains` (or `candidate_profile.example.yaml` if the real file
   doesn't exist).
3. Run:
   ```bash
   uv run job-hunter search --json --archive [--keyword "ADAS,Robotics,Product Technical Leader"]
   ```
   `--archive` writes to the deterministic `data/searches/{slug}_{date}.json` (`"default"` slug
   when no keyword is given). The same keyword on the same day overwrites (refreshing today's
   answer); a new day or a different keyword always gets its own file — an earlier run's
   candidate snapshot is never silently lost or overwritten by a later, different search. The
   command prints `Archived to: <path>`.
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
   loses an earlier run. Any prior archive stays reachable by keyword: `job-hunter resolve-search
   --keyword "..."` prints its path, and `job-reviewer`/`job-radar` accept the same `--keyword` to
   resolve it directly.

Onsite, hybrid, and remote jobs are all acceptable output — remote jobs require explicit U.S.
eligibility evidence, already enforced by the collector. Do not substitute broad web searches for
adapter failures unless the user explicitly requests that fallback.
