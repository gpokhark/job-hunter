# Personal resume inputs

Everything in this folder except this README is git-ignored. Use fake values in anything you share.

Files (all optional except the first):

- `main_resume_<YYYY-MM-DD>.md` - master resume, e.g. `main_resume_2026-01-01.md`. The newest
  date in the filename wins (never the file modification time).
- `cover_letter_<YYYY-MM-DD>.md` - sample cover letter for tone; newest date wins.
- `personalization.md` - standing preferences, with `## all`, `## resume-generator` and
  `## outreach-writer` sections.

Check what will be used: `uv run job-hunter resume-files`

Contact block for the resume header (name, email, ...): `uv run job-hunter contact`

Generated resumes, JDs and outreach go to `data/output/`, not here.
