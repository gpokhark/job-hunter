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
6. Evaluate only `candidates`, and never recommend a candidate with `us_eligible=false`. The
   collector has already deterministically excluded postings older than `search.max_posting_age_days`
   (default 30) using each job's `posted_at`; a job with no `posted_at` was kept because its age
   could not be determined, not because it is known-recent — do not claim a specific age for it.
7. Score candidates using [references/scoring.md](references/scoring.md).
8. Recommend only jobs meeting the configured threshold, with strengths, gaps, first-party URLs,
   and the posting date (`posted_at`) when it is present on the candidate.
9. Do not invent salary, sponsorship, arrangement, qualifications, or posting dates.
10. If nothing meets the threshold, say so. At most three candidates within five points may be labeled near-matches.

Onsite, hybrid, and remote jobs are acceptable. Remote jobs require explicit U.S. eligibility evidence.
The collector owns retrieval and location filtering. Do not substitute broad web searches for adapter failures unless the user requests that fallback.

