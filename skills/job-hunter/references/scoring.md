# Candidate scoring

This rubric is embedded verbatim into the prompt `scripts/review_with_lm_studio.py` sends to the
local model for each job, one at a time — it is not applied by the coordinating agent itself (see
`SKILL.md`'s procedure). Score one job against the resume, from 0 to 100:

- Core domain and technical relevance: 30
- Experience and seniority alignment: 20
- Skills, tools, and methods overlap: 20
- Responsibility and ownership alignment: 15
- Career progression and role quality: 10
- Other profile preferences: 5

Bands: 90–100 exceptional, 80–89 strong, 75–79 good, below 75 omit by default.
Location carries no quality points after U.S. eligibility. Penalize missing required qualifications materially and missing preferred qualifications modestly. If the resume does not establish a qualification, describe it as “not evidenced.”

Cite two to four concrete matches and one to three meaningful gaps using both job-description and resume/profile evidence. Do not score by raw keyword count. Respond with only the JSON object the prompt specifies (score, recommended, matches, gaps) — nothing else.

