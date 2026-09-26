# Resume generator and outreach writer skills — design

Status: **proposed; awaiting review. No code is implemented.**

Date: **2026-09-26**. Source material: `data/temp/Job-Op-Resume/` (a scratch copy of the standalone
"Job-Op-Resume" repo, to be deleted by the owner later; its git history is *not* carried over).

## 1. Goal

Bring the standalone Job-Op-Resume workflow into job-hunter as first-class, runtime-portable
features: an agent skill that writes a tailored, ATS-friendly resume for one job
(`resume-generator`), an agent skill that writes an outreach email and/or cover letter for it
(`outreach-writer`), the deterministic Python they need, and a **Resume** button on the live radar
that hands a radar job to the resume skill in one click.

Success looks like: on the live radar the user clicks **Resume** on a job, pastes the copied
prompt into Claude Code or Hermes, and gets a one-page (or 1.5/2-page) tailored CV as `.html` +
`.pdf` in `data/output/<Company>/`, built from their newest master resume, with no Microsoft Word,
no hook, and nothing personal committed to git.

## 2. Scope

**In scope**

1. `skills/resume-generator` and `skills/outreach-writer` (SKILL.md in this repo's conventions).
2. `job-hunter export-jd` (CLI) and `POST /api/jd` (live radar server): write one job's JD as a
   text file in `data/output/<Company>/`.
3. A **Resume** button on live radar rows.
4. A shared "newest dated master resume" resolver (`job-hunter resume-files`, which also reports
   the optional personalization and cover-letter-sample files), also used by the local-LLM reviewer.
   Owner-controlled personalization at invocation time and via `data/resume/personalization.md` (§6.8).
5. A `contact:` block in the (git-ignored) candidate profile.
6. `scripts/measure_resume.py` (HTML → PDF, page-fill check) and `scripts/log_resume.py`
   (append to `data/output/resume_log.csv`), behind an optional `resume` dependency extra.
7. Installer, docs and skill-version updates so both skills install for Hermes, Claude Code
   (global/local) and OpenCode like the existing six.

**Out of scope** (deliberately)

- The `job-scraper` skill and its MCP/Scrapling/Playwright-MCP machinery (job-hunter already
  collects job descriptions; a URL-scrape skill would duplicate that).
- The `.md` → `.docx` → PDF path (`build_resume.py`, `docx2pdf`) — needs Microsoft Word, which is
  not portable; the HTML path replaced it in the source repo.
- The PostToolUse hook (`convert_resume.py`) — runtime-specific and Write-tool-only; the skills
  call the scripts explicitly instead.
- A resume queue, running Claude/Hermes from the radar server, and an **Outreach** button.
- Applying to jobs, scheduling, or any change to scoring/prefilter (job-hunter's existing
  division of labor holds: Python owns retrieval and deterministic mechanics; the agent skill owns
  the writing).

## 3. Decisions (all confirmed with the owner)

| # | Decision |
|---|---|
| D1 | Scope = core skills + feed from job-hunter; `job-scraper` is not ported. |
| D2 | The radar button **exports the JD to a text file and copies a prompt**; nothing AI runs from the web page. |
| D3 | The JD is saved as a file on click (not read from SQLite at skill time) so it survives job closure/`cleanup`, keeps a stable copy of the text the CV was tailored to, and serves outreach and the log later. |
| D4 | Generated files live under `data/output/<Company_Name>/` (already git-ignored). |
| D5 | Master resume = newest dated `main_resume_<YYYY-MM-DD>.md` in `data/resume/` (no config edit needed); the profile's `resume_path` is only a fallback. The reviewer uses the same resolver. |
| D6 | Renderer = HTML → PDF via Playwright/Chromium (cross-platform, no Word install needed). HTML is the only output format. |
| D7 | Skills must work in Claude Code **and** Hermes (and OpenCode), like the existing skills. |
| D8 | Contact details (name/email/phone/LinkedIn/GitHub) come from a `contact:` block in the git-ignored `config/candidate_profile.yaml`. |
| D9 | No Outreach button in this iteration. |
| D10 | Personalization stays fully in the user's hands: free-text instructions at every invocation (as today) **and** an optional standing `data/resume/personalization.md`; only integrity rules are non-overridable (see §6.8). |

## 4. Repository facts this design relies on (audited)

- Skills live in `skills/<name>/SKILL.md`; frontmatter carries `name`, `version`, `description`,
  `compatibility`, and `metadata.{job_hunter,hermes.tags}` (no `license:`); each has a
  `## Contract` section (Input/Output). `scripts/install_skill.sh` has a hard-coded
  `SKILL_NAMES` list and installs into Hermes/Claude/OpenCode targets; `.claude/skills/*` are
  symlinks for this repo. Any content change to a SKILL.md bumps its version.
- Operational scripts take `--project` via `rootutil.add_project_argument()` and call
  `chdir_to_project_root()` first; atomic file writes use `atomic.atomic_write_text`.
- `Storage` holds `jobs` rows (`title, company, location_raw, posted_at, description, canonical_url,
  department, salary_evidence, job_id, source_key, status`); `description` may contain HTML.
  `get_job_snapshot()` deliberately omits `description` (large); a new method is needed.
- `config/candidate_profile.yaml` is git-ignored (copied from the `.example` file);
  `CandidateProfile` is a Pydantic model with `resume_path: Path | None`.
  `review_with_lm_studio.py:288` reads `profile.resume_path` only.
- `data/*` is git-ignored (`.gitignore` line 1117).
- The live server (`scripts/serve_radar.py`) already has per-request `Storage`, Pydantic request
  models with `extra="forbid"`, Host/Origin/Content-Type/size guards, and a shared
  `_POST_ROUTES` table; the radar template/JS have a fixed inline-slot discipline so **static
  report output stays byte-identical** (golden test).
- The source skills (`data/temp/.../.claude/skills/`) contain Claude-Code-specific parts that must
  not be ported verbatim: `model:` pin, dependence on the PostToolUse hook, `ToolSearch`/MCP,
  `CLAUDE.local.md`, the `@file` mention syntax, a hard-coded first name in cover-letter filenames,
  and `D:\github\...` absolute paths.

## 5. Architecture

```
radar row  ──[Resume button]──▶ POST /api/jd ──▶ jd_export.export_jd()
                                    │                 │  reads Storage (jobs row incl. description)
                                    │                 └─ writes data/output/<Company>/JD_….txt
                                    ▼
              copies prompt:  "Use the resume-generator skill on <path>"
                                    │
                    (user pastes into Claude Code / Hermes)
                                    ▼
resume-generator skill ─ job-hunter resume-files ─▶ newest data/resume/main_resume_<date>.md (+ personalization.md, cover-letter sample if present)
                       ─ profile contact block  ─▶ name/email/phone/links
                       ─ writes _draft.html ─▶ scripts/measure_resume.py ─▶ fit report (JSON)
                       ─ writes <Last>_CV_….html ─▶ measure_resume.py --save-pdf ─▶ .pdf
                       ─ scripts/log_resume.py ─▶ data/output/resume_log.csv
outreach-writer skill  ─ reads the CV + JD_….txt in the same folder ─▶ email .txt / cover letter .html/.pdf/.txt
```

Division of responsibility (unchanged repo rule): **Python** resolves paths, exports the JD,
renders/measures PDFs and logs; **the skills** do keyword mapping and writing, with the source
skills' no-fabrication hard rules intact.

## 6. Components

### 6.1 Master-resume resolver — `src/job_hunter/resume_source.py`

`resolve_master_resume(root: Path, *, explicit: Path | None = None, profile: CandidateProfile | None = None) -> Path`

Precedence: (1) `explicit` if given (must exist, else `FileNotFoundError` naming it); (2) the newest
`main_resume_<YYYY-MM-DD>.md` in `<root>/data/resume/` by the **date in the filename** (ties or
unparseable dates ignored; a name that doesn't match the pattern is never chosen — no
mtime guessing); (3) `profile.resume_path` if set and existing; else `FileNotFoundError` with a
message telling the user to add `data/resume/main_resume_<YYYY-MM-DD>.md`.
`resolve_master_resume` returns `ResolvedResume(path, source)` with `source` in
`{"explicit", "dated", "profile"}`. The `resume-files` CLI **refuses** (exit 2) when the only
candidate is a profile fallback whose filename contains `.example.` (the repo ships
`config/resume.example.md`), so a resume is never generated from placeholder text; the local
reviewer keeps today's behavior (it may still use the fallback).
The same module also provides `find_personalization(root)` (`data/resume/personalization.md` if it
exists) and `find_cover_sample(root)` (newest dated `data/resume/cover_letter_<date>.md`, same
filename-date rule). The CLI `job-hunter resume-files [--resume PATH]` prints one JSON object
`{master_resume, master_resume_source, personalization, cover_letter_sample, review_evidence}`
(absolute paths; the last three `null` when absent; `review_evidence` = newest dated
`data/resume/Review_Evidence_<date>.md`, used only for IEEE/SAE/committee-style contexts) on stdout so a skill can capture everything in one call, and exits non-zero with the
message on stderr only when no master resume can be found.
`review_with_lm_studio.py` calls the same function instead of reading `profile.resume_path`
directly; assessment cache validity still keys on `content_hash` only (updating the resume never
forces re-review — unchanged).

### 6.2 Contact block — `CandidateProfile.contact`

```yaml
contact:
  name: "Jane Doe"
  email: "jane@example.com"
  phone: "+1 555 555 0100"
  linkedin: "https://www.linkedin.com/in/janedoe"
  github: "https://github.com/janedoe"
```

New Pydantic model `ContactInfo` (all fields `str | None`; `name` required for generation) and
`CandidateProfile.contact: ContactInfo = ContactInfo()`. `candidate_profile.example.yaml` gets the
block with obvious placeholders. `job-hunter contact` prints the block as JSON (plus derived `first_name`/`last_name`) and exits
non-zero listing missing/placeholder fields; `name` and `email` are required, `phone`, `linkedin`
and `github` are optional (omitted from the resume header when absent) (a value is a placeholder if it contains `example.com`,
`xxxx`, or equals the example's text) so skills fail fast with exact instructions and **never
invent contact details**. `LastName`/first name are derived from `contact.name` by the skills
(last whitespace-separated token; first token).

### 6.3 JD export — `src/job_hunter/jd_export.py`

- `export_jd(storage, source_key, job_id, output_root: Path, today: date) -> ExportResult`
  where `ExportResult = {path: Path, created: bool, prompt: str}`.
- **Source of truth:** a new `Storage.get_job_for_jd(source_key, job_id)` returning
  `company, title, department, location_raw, posted_at, job_id, canonical_url, salary_evidence,
  description, source_key` (only this call reads `description`). Missing job → `JobNotFound`;
  empty/whitespace-only description → `JobHasNoDescription` (no file is written).
- **File format** (plain text; a deliberate, documented subset of the source `job-scraper`
  format — sections that need an LLM to split (Minimum/Preferred Qualifications) are not
  fabricated; the description is included whole, as plain text):

```
<Title>
<Location>
<Department, or omitted>

Summary
Posted: <YYYY-MM-DD or "Not specified">
Job ID: <job_id>
Job URL: <canonical_url>
Source: <company> (<source_key>)

Description
<description converted to plain text: block/paragraph breaks preserved, <li> -> "- ", entities decoded>

Pay & Benefits
<salary_evidence, section omitted if none>
```

- **Path:** `<output_root>/<Company_Name>/JD_<Company>_<Title4Words>_<YYYY-MM-DD>.txt`, where
  `<Company_Name>` = company with spaces → underscores and special characters stripped;
  `<Title4Words>` = first four words of the title, same sanitization (matches the source repo's
  rule); the date is `today`. Filename sanitization allows only `[A-Za-z0-9_-]`, never path
  separators; an empty result falls back to `Company`/`Job`.
- **Idempotence:** if today's file exists **and its content equals what would be written**,
  return it with `created=False`; if it exists with *different* content (the job changed
  since), write `..._<YYYY-MM-DD>_2.txt` (then `_3`, …) so an earlier tailored CV's JD is never
  overwritten. A different day always gets its own dated file.
- **Prompt returned:** `Use the resume-generator skill on <relative path from project root>` — a
  runtime-neutral sentence (no `@file` syntax). Relative path is `data/output/...` so it works from
  the project root in either agent.
- HTML → text uses `selectolax` (already a dependency); the converter is pure and unit-tested.
- Writes go through `atomic_write_text`; the output root is a parameter (default
  `settings.database_path.parent / "output"`, i.e. `data/output`).
- CLI: `job-hunter export-jd <source_key> <job_id>` prints the JSON `{path, created, prompt}`;
  exit 1 with a message on `JobNotFound` / `JobHasNoDescription`.

### 6.4 Renderer and logger — `scripts/measure_resume.py`, `scripts/log_resume.py`

Ported from the source repo with these changes: `--project` support; paths relative to the project
root (`data/output/...`); `log_resume.py` writes `data/output/resume_log.csv` (same columns as the
source: `Date, Company, Role, Job_URL, Fill_Pct, Pages, Iterations, Resume_File`, with
`Fill_Pct` = the last page's fill against the target band); the numeric/band logic is factored into
pure functions (`fill_status(content_height_px, page_count, last_page_fill_pct, target_pages)`)
that are unit-tested without a browser. `measure_resume.py` prints one JSON object (as in the source:
`status`, `last_page_fill_pct`, `pages`, `guidance`) and, with `--save-pdf`, writes the PDF.
Playwright and pypdf are imported lazily; if they (or Chromium) are missing the script prints the
exact fix (`uv sync --extra resume` and `uv run playwright install chromium`) and exits 2 rather
than a traceback. US Letter, 0.5" margins, 1/1.5/2-page targets and fill bands are unchanged from
the source.
**Skill interface:** the skills only ever call `measure_resume.py <html> [--target-pages N]
[--save-pdf out.pdf]`, so the skill text never depends on how the PDF is produced.
**Fonts:** the HTML template's font stack becomes `Arial, "Liberation Sans", Helvetica, sans-serif`
(metric-compatible fallbacks), and the skill notes that page-fill is measured on the machine that
renders the PDF, so it is re-measured per machine rather than assumed portable.

### 6.5 Dependencies

`pyproject.toml`: `[project.optional-dependencies] resume = ["playwright>=1.45,<2", "pypdf>=5,<7"]`
(exact bounds confirmed during implementation against what resolves under Python 3.11–3.13). No base
dependency changes. `python-docx`, `docx2pdf`, `scrapling[ai]` are **not** added. Setup docs:
`uv sync --extra resume` then `uv run playwright install chromium` (on a bare Linux host
`--with-deps` may be needed for system libraries).

### 6.6 Skills

Both are `skills/<name>/SKILL.md` with frontmatter `name`, `version: 1.0.0`, `description`,
`compatibility` (requires uv, the `resume` extra + Chromium for PDF output), and
`metadata.job_hunter.stage: resume` / `outreach`, `metadata.hermes.tags`. Each has a `## Contract`
(Input/Output/next command). Content changes from the source skills:

- Remove: `model:` pin, version-history tables (replaced by a short changelog line),
  `CLAUDE.local.md`, hook reliance, MCP/`ToolSearch`, `@file` syntax, absolute `D:\` paths,
  hard-coded first name in filenames.
- Inputs: JD = a path to a `JD_*.txt` (from the button/`export-jd`) or pasted JD text, plus any
  free-text instructions in the request (§6.8); master resume, `personalization.md` and cover-letter
  sample = `job-hunter resume-files --project <project path>`; contact = `job-hunter contact`
  (stop and report missing fields); output folder = the JD file's folder (else
  `data/output/<Company_Name>/`).
- Commands run explicitly by the skill: `uv run python scripts/measure_resume.py ...`,
  `... --save-pdf ...`, `uv run python scripts/log_resume.py ...` (the source's `_iterations.json`
  sidecar is dropped: the skill passes `--iterations N` directly).
- Cover-letter filename uses the contact first name: `<First>_CL-<Company>-<RoleToken>_<date>`;
  CV/email use `<LastName>`. `RoleToken` derivation stays a skill instruction (LLM judgment, as in
  the source).
- Kept verbatim in substance: keyword extraction/mapping, bullet formula and pitfalls, page-size
  rules and bullet counts, HTML/CSS template, measurement loop (one adjustment iteration),
  Dale Carnegie email rules (200–250 words, exactly 3 points), cover-letter format (exactly 5 bold
  bullets, ~220 words), and every **Hard Rule** (no fabrication, no invented recipient names,
  fixed formats).
- The cover-letter format reference (a PDF of the applicant's own earlier letter in the source repo)
  is replaced by an optional `data/resume/cover_letter_<date>.md` sample (the newest dated file,
  same resolver rule); if absent, the skill uses the built-in structure in its own text.
- **Runtime portability:** no runtime-specific tool names; invocation works by name
  (`/resume-generator …` where slash commands exist, or the button's plain sentence) and by natural
  language (the `description` carries the trigger phrases). Installed by `install_skill.sh` for
  Hermes, Claude Code (global/local) and OpenCode; `SKILL_NAMES` gains the two names.

### 6.7 Radar button — live mode only

- **Markup** (only when `live=True`, following the inline-slot discipline so static output is
  unchanged): a `button.resume-btn` (`title="Generate a tailored resume"`) placed in `.row-end`
  right after the Track chip, on both `details.row` and `div.plain-row`.
- **Behavior (`radar_live_ui.js`, delegated click handler):** `POST /api/jd`
  `{source_key, job_id}` → on 200, copy `prompt` to the clipboard (`navigator.clipboard`, with a
  selectable fallback inside the notice when the API is unavailable/denied) and show the notice
  `Copied — paste into Claude Code or Hermes (JD saved: <relative path>)`; the button shows a brief
  "Saved" state. 404 → notice "This job is no longer in the database (cleanup removed it)"; 409 →
  "This job has no description to tailor against"; network/5xx → the existing retryable-notice
  wording. The button is **not** queued through the offline outbox (it needs the server now; a
  failed click simply reports and can be clicked again).
- **Server:** `POST /api/jd` added to `_POST_ROUTES` with model `JdExport(source_key, job_id,
  extra="forbid", no client_ts)`; same guards as other POSTs. Responses: `200 {ok, path,
  created, prompt}`, `404 {ok:false, error:"unknown job"}`, `409 {ok:false, error:"job has no
  description"}`. `path` in the response is project-relative (never an absolute path); a write
  failure returns 503/500 with a generic message and logs the traceback (no path leakage).
- The prompt/path never come from the browser; the client sends only the two identifiers.

### 6.8 Personalization (owner-controlled, at any time)

Personalization works exactly as it does in the source workflow, plus one persistent file:

1. **At invocation (every run).** Anything the user writes alongside the request is a first-class
   instruction: page size ("1.5 page", "2 page"), emphasis ("lead with functional safety",
   "feature the Ford role first"), what to drop ("leave out the older roles"), tone or length
   changes, a specific recipient name or angle for outreach, extra context about the company or
   role, "use this JD text" pasted inline, or any combination. The skills must read the whole
   request for such instructions before applying their defaults, and must not ignore or
   re-interpret them. The button's copied prompt is plain text the user pastes and can freely
   extend (e.g. `Use the resume-generator skill on <path>, 2 page, emphasise ADAS validation`).
2. **Standing preferences (optional file).** `data/resume/personalization.md` (git-ignored via
   `data/*`, plain markdown, edited any time, no config, no restart) holds standing instructions
   with optional per-skill sections (`## resume-generator`, `## outreach-writer`, and `## all`).
   The skills load it on every run via `job-hunter resume-files`; typical content: preferred
   tone, phrases to avoid or prefer, sections to always/never include, default page size, whether
   to lead with certain skills, signature/sign-off style, things about the user's situation
   (e.g. relocation, visa status wording rules, notice period) that may be stated in outreach.
3. **Precedence** (highest first): integrity rules (below) > instructions in the current request >
   `personalization.md` > the skill's built-in defaults. A request-time instruction always beats a
   standing one for that run only; nothing in a run rewrites the personalization file.
4. **What can never be overridden (integrity rules).** No fabrication (skills, tools,
   certifications, metrics, employers, dates, experience not in the master resume), no
   invented recipient names or contact details, no claims contradicting the master resume, and
   no leaving the job-hunter output folder. If an instruction would violate one, the skill says
   so briefly and does the honest version instead (e.g. names the closest real experience).
5. **What can be overridden (format defaults).** Page size, bullet counts, the email's word
   range and 3-point structure, the cover letter's 5-bullet/~220-word structure, the summary
   length, the Technical Skills block, filename tokens' page suffix, etc. are *defaults*: the user
   may change any of them by explicit request or in `personalization.md`. Where a default is
   overridden, the skill still runs the measurement loop against the overridden target and
   reports honestly if the result doesn't fit.
6. **Editing the skills themselves** remains possible (they are plain `SKILL.md` files), with the
   repo rule that a behavior change bumps the skill's `version` (and the installer re-links/copies).
   Personalization through the request or the file needs no repo change and is the recommended path.
7. **Transparency.** Each run's report says which personalization it applied (which parts of the
   request and which sections of `personalization.md`) so the user can verify and adjust.

## 7. Data layout

```
data/resume/main_resume_<YYYY-MM-DD>.md        # you add these; newest date wins (git-ignored via data/*)
data/resume/cover_letter_<YYYY-MM-DD>.md       # optional format sample for outreach-writer
data/resume/personalization.md                 # optional standing preferences (see 6.8)
data/resume/Review_Evidence_<YYYY-MM-DD>.md    # optional; IEEE/SAE/committee contexts only
data/output/<Company_Name>/JD_<Company>_<Title>_<date>[_N].txt
data/output/<Company_Name>/<Last>_CV_<Company>_<RoleToken>[_1p5_|_2p_]<date>.html / .pdf
data/output/<Company_Name>/<Last>_Email_<Company>_<date>.txt
data/output/<Company_Name>/<First>_CL-<Company>-<RoleToken>_<date>.html / .pdf / .txt
data/output/resume_log.csv
```

## 8. Error handling

| Situation | Behavior |
|---|---|
| No master resume found | `resume-files` exits 2; message says to add `data/resume/main_resume_<date>.md` (or pass `--resume`). |
| Contact missing/placeholder | `contact` exits 2 listing the fields; skills stop and relay it. |
| `resume` extra / Chromium missing | scripts exit 2 with the exact install commands. |
| JD job missing / no description | `export-jd` exits 1; server 404/409; UI notice; no file written. |
| Output write fails | server generic error + log; CLI non-zero. Files are written atomically (no partial files). |
| Fill check not `ok` after one iteration | skill saves anyway and reports the remaining issue (source-skill rule kept). |
| Path traversal via title/company | impossible: filenames are sanitized to `[A-Za-z0-9_-]`; the output root is fixed. |

## 9. Testing

- **Unit:** `resume_source` (newest by filename date, ignores non-matching names, explicit/fallback
  precedence, error messages); `ContactInfo`/`job-hunter contact` (missing/placeholder detection);
  `jd_export` (format golden with HTML description, `<li>`/entity handling, filename
  sanitization incl. hostile titles, idempotent re-export, changed-content `_2`, missing job, empty
  description, no absolute path in results); `find_personalization`/`find_cover_sample` and the
  `resume-files` JSON shape (present/absent/`null`); `fill_status` bands for 1/1.5/2 pages; `log_resume` CSV
  header/row/append; `review_with_lm_studio` uses the resolver (existing tests still pass).
- **Server:** `POST /api/jd` — success (file exists, relative path, prompt), 404, 409, guards (Host,
  Origin, Content-Type, size, `extra="forbid"`), no absolute path/leak, repeated click
  (`created=False`), changed description (`_2`). Hermetic (`tmp_path`, port 0).
- **Renderer:** live rows contain `button.resume-btn` for scored and never-reviewed rows;
  static output has none and the golden test passes unmodified; hostile text stays escaped.
- **JS:** the click handler's pure helpers (prompt copy fallback, notice text per status) covered by
  `node --test` where practical; the DOM wiring verified with the existing jsdom smoke approach
  and the owner's manual pass.
- **Optional integration:** one test that runs `measure_resume.py` on a fixture HTML with
  Playwright, **skipped** when Chromium/Playwright aren't installed (never required for the suite).
- **Skills:** not unit-testable; quality is guarded by their retained checklists and hard rules,
  the Contract sections, and a manual end-to-end run on one real job before merge.

## 10. Compatibility, docs and migration

- Additive: no existing command/route/table changes; `job-hunter search`/pipeline untouched.
  The only behavior touch is the reviewer's resume resolution (falls back to today's
  `profile.resume_path` when no dated file exists, so an existing setup keeps working).
- Docs: `docs/SPEC.md` (new sections: resume/outreach feature, CLI `export-jd`/`resume-files`/
  `contact`, routes, scripts, data layout, deps), `README.md` (setup and workflow), `CLAUDE.md`
  (architecture bullets, safe-testing note: output/resume files are personal data), and
  `scripts/install_skill.sh`'s help/`SKILL_NAMES`; `.claude/skills/*` symlinks for the two skills.
  `skills/job-radar/SKILL.md` gets one line about the Resume button (version bump 1.4.0 → 1.5.0);
  the two new skills start at 1.0.0.
- `data/temp/` is untouched by the implementation (the owner deletes it); nothing in the repo
  may reference it.

## 11. Assumptions to confirm (defaults chosen; say if any is wrong)

1. The button appears only in **live** mode (static reports can't write the JD file).
2. `data/resume/` also holds the optional dated cover-letter sample (the source repo kept a PDF
   reference; a text/markdown sample is simpler and portable).
3. The exported JD is header fields + whole plain-text description (no LLM-split qualification
   sections).
4. The copied prompt is the runtime-neutral sentence `Use the resume-generator skill on <path>`.
5. The `resume` extra bounds and Python-version support are settled at implementation time.

## 12. Deliberately deferred

An Outreach button; a resume/outreach queue; the job-scraper skill; batch
"generate for all Saved applications"; showing generated-CV links back on the radar row.
