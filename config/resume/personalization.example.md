# Personalization (example)

This is a fake-valued template. To use it:

```bash
cp config/resume/personalization.example.md config/resume/personalization.md
```

Then replace the sample rules with your own and delete the ones you do not want. Your copy
(`personalization.md`) is git-ignored; this example is tracked, so never put real data in it.

TEMPLATE-NOT-CUSTOMIZED: delete this whole line once you have replaced the sample rules below with your own. While it is present, `job-hunter resume-files` withholds this file, so an unedited copy never steers your resumes.

How it works:

- Used by the `resume-generator` and `outreach-writer` skills. Each reads `## all` plus its own
  section (`## resume-generator` or `## outreach-writer`) and ignores the other.
- Write rules in plain English, one per bullet, under one of those `## ` sections (a section with any
  other name, such as `## resume generator`, is ignored, and `resume-files` warns about it).
- A rule can be conditional. For an employer, just name it ("When applying to Acme Corp, ..."):
  `resume-files` lists the rules that name the employer. For a job title, start the bullet with a tag,
  `- [role: program manager, TPM] ...`: it applies only when one of the listed titles appears, as whole
  words, in the job's title (`resume-files --jd` reads the title from the JD file), and never otherwise.
- Precedence: integrity rules > what you type in the request for that run > this file > the skill's
  defaults. Anything in the request wins for that run only.
- Integrity rules cannot be overridden here: nothing may be invented (skills, tools, employers,
  dates, metrics, contact details). A rule can only steer which real experience is shown and how.
- Check the file is being picked up: `uv run job-hunter resume-files` (the `personalization` field;
  `personalization_warning` explains why it is null if the template line is still present).

## all

Applies to resumes and outreach alike.

- Write in US English.
- Never use the words "passionate", "synergy" or "rockstar".
- Standing fact I am happy to state when relevant: I am open to hybrid or on-site work near Springfield.

## resume-generator

### Format defaults

- Default to a 2 page resume unless my request says otherwise.
- Start recent roles at 5 bullets instead of 4.
- Use en dashes for date ranges only; avoid em dashes everywhere else.
- Keep the Technical Skills block to 2 lines and list tools in order of relevance to the job.
- Leave out roles older than 10 years unless the job description asks for them.

### Employer-specific rules

- When applying to Acme Corp, add a summary bullet stating my combined experience with Acme:
  3 years as a direct Acme employee (2019 to 2022) plus 2 years delivering Acme programs as an
  Initech consultant (2022 to 2024), 5 years in total. Omit this bullet for every other employer.
- The client for all work in my Initech role was Acme Corp. Name Acme as the client only on resumes
  for Acme itself. For any other employer describe it without naming the client (for example
  "a large enterprise client") and never print the Acme name in the Initech section.

### Role-specific rules

- [role: program manager, project manager, TPM] Keep the roles in reverse-chronological order, but lead
  the summary and each recent role with program-management work (schedules, budgets, risk, stakeholder
  communication), then technical depth.
- [role: data engineer, analytics engineer] Put SQL and pipeline tools first in Technical Skills.

### Transferable experience

- My experience with ToolA is comparable to ToolB. When a job description asks for ToolB, describe
  my ToolA work as transferable experience; never claim direct ToolB experience.

### Content preferences

- For cloud or infrastructure roles, put the Certified Example Architect certification first in
  Education.
- For management roles, lead each recent role with its team-leadership bullet.

## outreach-writer

- Sign emails "Best regards," instead of "Sincerely,".
- Keep emails under 180 words.
- Address hiring managers by first name when the job description gives one.
- Keep the tone warm and direct; no exclamation marks.
