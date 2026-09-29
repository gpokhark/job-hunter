# Resume Generator + Outreach Writer skills and radar Resume button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the standalone Job-Op-Resume workflow into job-hunter: two runtime-portable agent skills (`resume-generator`, `outreach-writer`), the deterministic Python they need (newest-resume resolver, contact config, JD export, HTML→PDF measure/log scripts), and a **Resume** button on the live radar that exports a job's JD file and copies a ready-to-paste prompt.

**Architecture:** Python owns retrieval and mechanics (`resume_source`, `jd_export`, `measure_resume.py`, `log_resume.py`, `POST /api/jd`); the skills own writing and are driven by explicit script calls (no hook, no MCP, no Word). Personalization is fully owner-controlled: free text in every request plus an optional `data/resume/personalization.md`. Everything generated lives under git-ignored `data/output/<Company>/`.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `http.server`/`csv`, `selectolax` not required, vanilla JS, pytest, node (optional), Playwright + pypdf (optional `resume` extra).

**Spec:** `docs/superpowers/specs/2026-09-26-resume-outreach-skills-design.md` (read §3 decisions, §6 components, §6.8 personalization first). The source material is `data/temp/Job-Op-Resume/` (scratch copy; the owner deletes it later — **nothing in the repo may reference it**, and the skill text below is self-contained).

**Before starting:** the spec is committed on branch `resume-outreach-skills` (cut from `dev`). Commit this plan there first and work on that branch.

## Global Constraints

- Python `>=3.11`; **no new base dependency**. The `resume` extra (`playwright`, `pypdf`) is optional; nothing in the default test suite may require it (tests that need a browser **skip** cleanly). `uv run ruff check .` must stay clean (baseline: clean).
- Baseline before this work: **565 passed, 2 failed** (`tests/test_collector.py::test_no_default_cap_without_keywords`, `::test_keyword_search_overrides_profile_terms` — pre-existing, unrelated; report separately, never "fix" here).
- **Static radar output must stay byte-identical** (`tests/test_render_radar.py::test_static_render_is_byte_identical_to_the_pre_live_golden` passes **unmodified**; never touch `tests/fixtures/radar_static_golden.html`). New row slots are **inline** (`{feedback_buttons}{app_chip}{resume_btn}` on one line) and empty in static mode.
- Never put company/title into row-root `data-*` attributes.
- **Personal data never enters git:** dated resumes, `personalization.md`, contact details, JDs and generated CVs/letters live only under `data/` (git-ignored) or the git-ignored `config/candidate_profile.yaml`. No test, fixture, doc or skill text may contain a real person's name, email, phone or employer; use obviously fake values (`Jane Doe`, `jane@example.com`).
- **Never write to the real `data/`** from tests or smoke runs: use `tmp_path` / a temp project. Server tests bind port 0 and use `monkeypatch.chdir(tmp_path)` where a project root matters; none may depend on `$JOB_HUNTER_ROOT`.
- The `POST /api/jd` route must never return or log an absolute path, never accept a path/text from the browser (only `source_key`, `job_id`), and reuses every existing POST guard (Host, Origin, Content-Type, size, drain, `extra="forbid"`).
- Filename components are sanitized to `[A-Za-z0-9_-]` (no path separators, no leading/trailing `_`/`-`); output root is fixed; writes are atomic (`atomic_write_text`).
- Integrity rules in the skills are non-overridable (no fabrication; no invented recipient names or contact details; nothing contradicting the master resume; stay inside the job-hunter output folder). Format rules are overridable defaults. See the skill text below and spec §6.8.
- Skills follow this repo's conventions: frontmatter `name`, `version`, `description`, `compatibility`, `metadata.job_hunter.stage`, `metadata.hermes.tags` (no `license:`, **no `model:`**), a `## Contract` section, `--project` on every command, and no runtime-specific tooling (no MCP/`ToolSearch`/hooks/`@file`). Any later behavior change to a SKILL.md bumps its `version`.
- End every commit message with the line `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (second `-m`). Stage files by name; never `git add -A` (an untracked `.superpowers/` dir exists).
- Tests run with `uv run pytest`; none touch the network.

## Review Focus

Failure modes this feature implies that a user is most likely to hit (each pinned by a test in the named task):

1. **Newest resume chosen by filename date, not mtime**, invalid/garbage names ignored, and **never the repo's example resume** for generation — Task 1.
2. **Missing/placeholder contact details** must stop the skills with exact instructions, never let them invent a name/email — Task 2.
3. **Hostile job text** (title/company with `../`, `..\`, quotes, unicode, huge length; HTML descriptions with scripts/entities) must not escape the output folder or corrupt the JD file — Task 3.
4. **Clicking Resume twice / after the description changed / after `cleanup` removed the job** — reuse vs new dated `_2` file, 404, 409, all with visible notices and no partial files — Tasks 3, 5, 6.
5. **Missing Playwright/Chromium** must produce the exact install commands, not a traceback — Task 4.

## File Structure

| File | Responsibility |
|---|---|
| `src/job_hunter/resume_source.py` (create) | Newest-dated master resume resolver + personalization/cover-sample/review-evidence finders. |
| `src/job_hunter/config.py`, `config/candidate_profile.example.yaml` (modify) | `ContactInfo` + `CandidateProfile.contact` + placeholder detection. |
| `src/job_hunter/jd_export.py` (create) | JD export (HTML→text, filename sanitization, idempotent write). |
| `src/job_hunter/storage.py` (modify) | `get_job_for_jd`. |
| `src/job_hunter/cli.py` (modify) | `resume-files`, `contact`, `export-jd`. |
| `scripts/review_with_lm_studio.py` (modify) | Use the shared resolver. |
| `scripts/measure_resume.py`, `scripts/log_resume.py` (create) | HTML→PDF measure (pure `analyze`) and CSV log. |
| `pyproject.toml`, `uv.lock` (modify) | Optional `resume` extra. |
| `scripts/serve_radar.py` (modify) | `POST /api/jd`. |
| `scripts/templates/radar_live_core.js`, `radar_live_ui.js` (modify) | `resumeNotice` + Resume button handler. |
| `scripts/render_radar.py` (modify) | Resume button markup + style (live only). |
| `skills/resume-generator/SKILL.md`, `skills/outreach-writer/SKILL.md` (create) | The skills. |
| `scripts/install_skill.sh`, `.claude/skills/*` symlinks, `tests/test_install_skill.py` (modify/create) | Install the two skills everywhere. |
| `docs/SPEC.md`, `README.md`, `CLAUDE.md`, `skills/job-radar/SKILL.md` (modify) | Docs + job-radar version bump. |
| tests | `tests/test_resume_source.py`, `tests/test_contact.py`, `tests/test_jd_export.py`, `tests/test_measure_resume.py`, `tests/test_log_resume.py`, plus additions to `tests/test_cli.py`, `tests/test_review_with_lm_studio.py`, `tests/test_serve_radar.py`, `tests/test_render_radar.py`, `tests/js/radar_live_core.test.js`. |

---

### Task 1: Newest-resume resolver, `resume-files` CLI, reviewer uses it

**Files:**
- Create: `src/job_hunter/resume_source.py`
- Modify: `src/job_hunter/cli.py`, `scripts/review_with_lm_studio.py`
- Test: `tests/test_resume_source.py` (create), `tests/test_cli.py`, `tests/test_review_with_lm_studio.py`

**Interfaces:**
- Consumes: `config.CandidateProfile` (`resume_path`), `config.load_profile`.
- Produces:
  - `resume_source.RESUME_DIR = Path("data/resume")`, `PERSONALIZATION_NAME = "personalization.md"`
  - `resume_source.ResolvedResume(path: Path, source: Literal["explicit","dated","profile"])` (frozen dataclass; `path` absolute)
  - `resume_source.resolve_master_resume(root: Path, *, explicit: Path | None = None, profile: CandidateProfile | None = None) -> ResolvedResume` — raises `FileNotFoundError`
  - `resume_source.find_personalization(root) -> Path | None`, `find_cover_sample(root) -> Path | None`, `find_review_evidence(root) -> Path | None`
  - `resume_source.is_example(path: Path) -> bool` (`".example." in path.name`)
  - CLI `job-hunter resume-files [--resume PATH]` → stdout JSON `{master_resume, master_resume_source, personalization, cover_letter_sample, review_evidence}` (absolute paths or `null`); exit 2 + stderr message when no master resume, or when the only candidate is an example-file profile fallback.
  - `review_with_lm_studio.resolve_resume(profile, root) -> tuple[CandidateProfile, str]` — returns the profile with `resume_path` replaced by the resolved file, and that file's text; raises `FileNotFoundError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resume_source.py`:

```python
import os
import time
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile
from job_hunter.resume_source import (
    find_cover_sample,
    find_personalization,
    find_review_evidence,
    is_example,
    resolve_master_resume,
)


def _resume_dir(root: Path) -> Path:
    directory = root / "data" / "resume"
    directory.mkdir(parents=True)
    return directory


def test_newest_dated_file_wins_by_filename_not_mtime(tmp_path):
    directory = _resume_dir(tmp_path)
    old = directory / "main_resume_2025-01-05.md"
    new = directory / "main_resume_2026-03-09.md"
    new.write_text("new")
    time.sleep(0.01)
    old.write_text("old")  # older date but *newer* mtime: must still lose
    os.utime(new, (1, 1))
    resolved = resolve_master_resume(tmp_path)
    assert resolved.path == new.resolve()
    assert resolved.source == "dated"


def test_names_that_do_not_match_or_are_not_real_dates_are_ignored(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "main_resume_2026-13-45.md").write_text("bad month")
    (directory / "main_resume_latest.md").write_text("no date")
    (directory / "resume_2026-01-01.md").write_text("wrong prefix")
    (directory / "main_resume_2026-01-02.md.bak").write_text("wrong suffix")
    (directory / "main_resume_2026-02-02.md").mkdir()  # a directory, not a file
    good = directory / "main_resume_2024-06-01.md"
    good.write_text("ok")
    assert resolve_master_resume(tmp_path).path == good.resolve()


def test_explicit_path_beats_everything_and_must_exist(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "main_resume_2026-03-09.md").write_text("dated")
    explicit = tmp_path / "elsewhere.md"
    explicit.write_text("explicit")
    resolved = resolve_master_resume(tmp_path, explicit=explicit)
    assert (resolved.path, resolved.source) == (explicit.resolve(), "explicit")
    with pytest.raises(FileNotFoundError, match="elsewhere-missing.md"):
        resolve_master_resume(tmp_path, explicit=tmp_path / "elsewhere-missing.md")


def test_profile_resume_path_is_only_a_fallback(tmp_path):
    fallback = tmp_path / "config" / "my_resume.md"
    fallback.parent.mkdir()
    fallback.write_text("profile")
    profile = CandidateProfile(resume_path=Path("config/my_resume.md"))  # relative to root
    resolved = resolve_master_resume(tmp_path, profile=profile)
    assert (resolved.path, resolved.source) == (fallback.resolve(), "profile")
    # a dated file appears -> it wins without touching the profile
    directory = _resume_dir(tmp_path)
    dated = directory / "main_resume_2026-01-01.md"
    dated.write_text("dated")
    assert resolve_master_resume(tmp_path, profile=profile).path == dated.resolve()


def test_nothing_found_explains_how_to_fix_it(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"data/resume/main_resume_<YYYY-MM-DD>\.md"):
        resolve_master_resume(tmp_path)
    profile = CandidateProfile(resume_path=Path("config/missing.md"))
    with pytest.raises(FileNotFoundError):
        resolve_master_resume(tmp_path, profile=profile)


def test_is_example_flags_placeholder_filenames():
    assert is_example(Path("config/resume.example.md"))
    assert not is_example(Path("data/resume/main_resume_2026-01-01.md"))


def test_optional_files_are_found_or_none(tmp_path):
    assert find_personalization(tmp_path) is None
    assert find_cover_sample(tmp_path) is None
    assert find_review_evidence(tmp_path) is None
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text("tone: plain")
    (directory / "cover_letter_2025-01-01.md").write_text("old")
    (directory / "cover_letter_2026-02-02.md").write_text("new")
    (directory / "Review_Evidence_2026-04-04.md").write_text("ieee")
    assert find_personalization(tmp_path) == (directory / "personalization.md").resolve()
    assert find_cover_sample(tmp_path) == (directory / "cover_letter_2026-02-02.md").resolve()
    assert find_review_evidence(tmp_path) == (directory / "Review_Evidence_2026-04-04.md").resolve()
```

Append to `tests/test_cli.py` (imports `json`, `main`, `pytest` already present):

```python
def _bare_project(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        f"database_path: {tmp_path}/data/jobs.sqlite3\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOB_HUNTER_ROOT", raising=False)


def test_resume_files_prints_all_paths_as_json(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    directory = tmp_path / "data" / "resume"
    directory.mkdir(parents=True)
    (directory / "main_resume_2026-01-01.md").write_text("r")
    (directory / "personalization.md").write_text("p")
    assert main(["resume-files"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["master_resume"].endswith("main_resume_2026-01-01.md")
    assert payload["master_resume_source"] == "dated"
    assert payload["personalization"].endswith("personalization.md")
    assert payload["cover_letter_sample"] is None and payload["review_evidence"] is None


def test_resume_files_exits_2_with_instructions_when_no_resume(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    assert main(["resume-files"]) == 2
    assert "main_resume_<YYYY-MM-DD>.md" in capsys.readouterr().err


def test_resume_files_refuses_the_example_resume_fallback(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    (tmp_path / "config" / "candidate_profile.yaml").write_text("resume_path: config/resume.example.md\n")
    (tmp_path / "config" / "resume.example.md").write_text("placeholder")
    assert main(["resume-files"]) == 2
    err = capsys.readouterr().err
    assert "example" in err and "data/resume/main_resume_" in err


def test_resume_files_explicit_resume_flag(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    mine = tmp_path / "mine.md"
    mine.write_text("x")
    assert main(["resume-files", "--resume", str(mine)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["master_resume_source"] == "explicit"
```

Append to `tests/test_review_with_lm_studio.py` (add `resolve_resume` to the `from review_with_lm_studio import (...)` list):

```python
def test_resolve_resume_prefers_the_newest_dated_resume_over_the_profile_path(tmp_path):
    (tmp_path / "old.md").write_text("profile resume")
    directory = tmp_path / "data" / "resume"
    directory.mkdir(parents=True)
    (directory / "main_resume_2026-05-05.md").write_text("newest resume")
    profile = CandidateProfile(resume_path=Path("old.md"))
    updated, text = resolve_resume(profile, tmp_path)
    assert text == "newest resume"
    assert updated.resume_path == (directory / "main_resume_2026-05-05.md").resolve()
    assert profile.resume_path == Path("old.md")  # the original model is not mutated


def test_resolve_resume_falls_back_to_the_profile_path_and_errors_when_neither_exists(tmp_path):
    (tmp_path / "old.md").write_text("profile resume")
    profile = CandidateProfile(resume_path=Path("old.md"))
    assert resolve_resume(profile, tmp_path)[1] == "profile resume"
    with pytest.raises(FileNotFoundError):
        resolve_resume(CandidateProfile(), tmp_path / "empty")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_resume_source.py tests/test_cli.py tests/test_review_with_lm_studio.py -q`
Expected: collection errors (`ModuleNotFoundError: job_hunter.resume_source`, `ImportError: resolve_resume`).

- [ ] **Step 3: Implement the resolver**

Create `src/job_hunter/resume_source.py`:

```python
"""Which files feed the resume/outreach skills, resolved deterministically (spec section 6.1).

The master resume is the newest `data/resume/main_resume_<YYYY-MM-DD>.md`, chosen by the date in
the *filename* — never by mtime, so touching or re-saving an older file can't silently promote it.
The profile's `resume_path` stays only as a fallback so an existing setup keeps working. The same
function serves the local-LLM reviewer, so scoring and resume generation always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from .config import CandidateProfile

RESUME_DIR = Path("data/resume")
PERSONALIZATION_NAME = "personalization.md"

_MASTER = re.compile(r"^main_resume_(\d{4})-(\d{2})-(\d{2})\.md$")
_COVER = re.compile(r"^cover_letter_(\d{4})-(\d{2})-(\d{2})\.md$")
_EVIDENCE = re.compile(r"^Review_Evidence_(\d{4})-(\d{2})-(\d{2})\.md$")

NO_RESUME_MESSAGE = (
    "No master resume found. Add data/resume/main_resume_<YYYY-MM-DD>.md "
    "(the newest date wins) or pass --resume PATH."
)


@dataclass(frozen=True)
class ResolvedResume:
    path: Path
    source: Literal["explicit", "dated", "profile"]


def _newest_dated(directory: Path, pattern: re.Pattern[str]) -> Path | None:
    if not directory.is_dir():
        return None
    best_key: tuple[date, str] | None = None
    best_path: Path | None = None
    for entry in directory.iterdir():
        match = pattern.match(entry.name)
        if not match or not entry.is_file():
            continue
        try:
            when = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:  # e.g. 2026-13-45 matches the shape but is not a date
            continue
        key = (when, entry.name)
        if best_key is None or key > best_key:
            best_key, best_path = key, entry
    return best_path


def resolve_master_resume(
    root: Path, *, explicit: Path | None = None, profile: CandidateProfile | None = None
) -> ResolvedResume:
    """Precedence: explicit path > newest dated file in data/resume/ > profile.resume_path."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"--resume file not found: {path}")
        return ResolvedResume(path.resolve(), "explicit")
    found = _newest_dated(root / RESUME_DIR, _MASTER)
    if found is not None:
        return ResolvedResume(found.resolve(), "dated")
    if profile is not None and profile.resume_path is not None:
        fallback = profile.resume_path if profile.resume_path.is_absolute() else root / profile.resume_path
        if fallback.is_file():
            return ResolvedResume(fallback.resolve(), "profile")
    raise FileNotFoundError(NO_RESUME_MESSAGE)


def is_example(path: Path) -> bool:
    """The repo ships `config/resume.example.md`; generation must never use placeholder text."""
    return ".example." in path.name


def find_personalization(root: Path) -> Path | None:
    path = root / RESUME_DIR / PERSONALIZATION_NAME
    return path.resolve() if path.is_file() else None


def find_cover_sample(root: Path) -> Path | None:
    found = _newest_dated(root / RESUME_DIR, _COVER)
    return found.resolve() if found else None


def find_review_evidence(root: Path) -> Path | None:
    found = _newest_dated(root / RESUME_DIR, _EVIDENCE)
    return found.resolve() if found else None
```

- [ ] **Step 4: Implement the CLI command and the reviewer change**

In `src/job_hunter/cli.py`: add `from . import resume_source` to the imports; after `sub.add_parser("export-applications")` add

```python
    resume_files = sub.add_parser("resume-files")
    resume_files.add_argument("--resume", type=Path, default=None)
```

and in `main()`, immediately after the `export-applications` block (before the big `if args.command in {...}`), add

```python
        if args.command == "resume-files":
            root = Path.cwd()
            try:
                profile = load_profile()
            except FileNotFoundError:
                profile = None  # a bare project with no profile file: dated resumes still resolve
            try:
                resolved = resume_source.resolve_master_resume(root, explicit=args.resume, profile=profile)
            except FileNotFoundError as exc:
                print(f"job-hunter: {exc}", file=sys.stderr)
                return 2
            if resolved.source == "profile" and resume_source.is_example(resolved.path):
                print(
                    "job-hunter: only the example resume was found (profile resume_path points at "
                    f"{resolved.path.name}). Add your own data/resume/main_resume_<YYYY-MM-DD>.md "
                    "so a resume is never generated from placeholder text.",
                    file=sys.stderr,
                )
                return 2
            def _s(path: Path | None) -> str | None:
                return str(path) if path else None
            print(_json({
                "master_resume": str(resolved.path),
                "master_resume_source": resolved.source,
                "personalization": _s(resume_source.find_personalization(root)),
                "cover_letter_sample": _s(resume_source.find_cover_sample(root)),
                "review_evidence": _s(resume_source.find_review_evidence(root)),
            }))
            return 0
```

(Define the nested `_s` above the `print` as shown, or hoist it to module level as `_path_or_none` — either is fine; keep it a plain helper.)

In `scripts/review_with_lm_studio.py`: change the config import to `from job_hunter.config import CandidateProfile, load_profile, load_settings`, add `from job_hunter.resume_source import resolve_master_resume`, add above `main()`:

```python
def resolve_resume(profile: CandidateProfile, root: Path) -> tuple[CandidateProfile, str]:
    """The resume text to score against: the newest dated data/resume/main_resume_<date>.md if there
    is one, else the profile's resume_path (today's behavior). The returned profile carries the
    resolved path so recorded assessments name the file actually used. Raises FileNotFoundError."""
    resolved = resolve_master_resume(root, profile=profile)
    return (
        profile.model_copy(update={"resume_path": resolved.path}),
        resolved.path.read_text(encoding="utf-8"),
    )
```

and replace the four lines in `main()`

```python
    profile = load_profile()
    if not profile.resume_path or not profile.resume_path.exists():
        print("job-hunter: no resume found (set resume_path in candidate_profile.yaml)", file=sys.stderr)
        return 2
    resume = profile.resume_path.read_text(encoding="utf-8")
```

with

```python
    try:
        profile, resume = resolve_resume(load_profile(), Path.cwd())
    except FileNotFoundError:
        print(
            "job-hunter: no resume found (add data/resume/main_resume_<YYYY-MM-DD>.md or set "
            "resume_path in candidate_profile.yaml)",
            file=sys.stderr,
        )
        return 2
```

(`Path` is already imported in that script; if not, add `from pathlib import Path`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_resume_source.py tests/test_cli.py tests/test_review_with_lm_studio.py -q && uv run ruff check src scripts tests`
Expected: PASS; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/resume_source.py src/job_hunter/cli.py scripts/review_with_lm_studio.py tests/test_resume_source.py tests/test_cli.py tests/test_review_with_lm_studio.py
git commit -m "feat(resume): newest-dated master resume resolver, resume-files CLI, shared with the reviewer" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Contact block, placeholder detection, `contact` CLI

**Files:**
- Modify: `src/job_hunter/config.py`, `config/candidate_profile.example.yaml`, `src/job_hunter/cli.py`
- Test: `tests/test_contact.py` (create), `tests/test_cli.py`

**Interfaces:**
- Consumes: `config.CandidateProfile`, `config.load_profile`.
- Produces:
  - `config.ContactInfo` (`name, email, phone, linkedin, github: str | None`; properties `first_name`, `last_name` — first/last whitespace token of `name`, `None` when no name)
  - `CandidateProfile.contact: ContactInfo = ContactInfo()`
  - `config.contact_problems(contact: ContactInfo) -> list[str]` — human-readable problems for `name` and `email` (required: missing or placeholder) and for any *filled* optional field that is a placeholder; `[]` = fine.
  - CLI `job-hunter contact` → stdout JSON of the non-empty fields plus `first_name`/`last_name`; exit 2 + stderr listing every problem plus the file to edit when there are any.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_contact.py`:

```python
from job_hunter.config import CandidateProfile, ContactInfo, contact_problems


def _ok(**over):
    values = {"name": "Jane Doe", "email": "jane.doe@mail.test", "phone": "+1 313 555 0142"}
    values.update(over)
    return ContactInfo(**values)


def test_name_parts_use_the_first_and_last_token():
    contact = ContactInfo(name="  Jane Q. Doe ")
    assert (contact.first_name, contact.last_name) == ("Jane", "Doe")
    assert ContactInfo(name="Cher").last_name == "Cher"
    assert ContactInfo().first_name is None and ContactInfo().last_name is None


def test_a_complete_contact_has_no_problems():
    assert contact_problems(_ok()) == []
    assert contact_problems(_ok(linkedin="https://www.linkedin.com/in/jd", github="https://github.com/jd")) == []


def test_missing_required_fields_are_reported():
    problems = contact_problems(ContactInfo())
    assert any(p.startswith("name:") for p in problems)
    assert any(p.startswith("email:") for p in problems)
    assert not any(p.startswith("phone:") for p in problems)  # optional


def test_placeholder_values_are_reported_even_when_optional():
    problems = contact_problems(ContactInfo(
        name="Your Name", email="you@example.com", phone="+1 555 555 0100",
        linkedin="https://www.linkedin.com/in/your-handle", github="https://github.com/your-handle",
    ))
    joined = "\n".join(problems)
    for field in ("name", "email", "phone", "linkedin", "github"):
        assert f"{field}:" in joined, field
    assert all("placeholder" in p for p in problems)


def test_whitespace_only_counts_as_missing():
    assert any(p.startswith("name:") for p in contact_problems(_ok(name="   ")))


def test_profile_has_an_empty_contact_by_default_and_parses_a_yaml_block():
    assert CandidateProfile().contact == ContactInfo()
    profile = CandidateProfile.model_validate({"contact": {"name": "Jane Doe", "email": "j@mail.test"}})
    assert profile.contact.email == "j@mail.test"
```

Append to `tests/test_cli.py` (uses the `_bare_project` helper from Task 1):

```python
def test_contact_prints_json_with_derived_name_parts(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    (tmp_path / "config" / "candidate_profile.yaml").write_text(
        "contact:\n  name: Jane Q. Doe\n  email: jane@mail.test\n  phone: '+1 313 555 0142'\n"
    )
    assert main(["contact"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "name": "Jane Q. Doe", "email": "jane@mail.test", "phone": "+1 313 555 0142",
        "first_name": "Jane", "last_name": "Doe",
    }


def test_contact_exits_2_and_lists_every_problem(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    (tmp_path / "config" / "candidate_profile.yaml").write_text(
        "contact:\n  name: Your Name\n  email: you@example.com\n"
    )
    assert main(["contact"]) == 2
    err = capsys.readouterr().err
    assert "name:" in err and "email:" in err and "config/candidate_profile.yaml" in err


def test_contact_with_no_profile_file_at_all_exits_2(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    assert main(["contact"]) == 2
    assert "contact:" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contact.py tests/test_cli.py -q`
Expected: FAIL (`ImportError: cannot import name 'ContactInfo'`).

- [ ] **Step 3: Implement**

In `src/job_hunter/config.py`, above `class CandidateProfile`, add:

```python
_PLACEHOLDER_MARKERS = ("example.com", "your name", "your-handle", "555 555 0100", "xxxx")


class ContactInfo(BaseModel):
    """Applicant contact details for generated resumes/letters. Lives in the git-ignored
    `config/candidate_profile.yaml`; `name` and `email` are required for generation, the rest are
    optional and simply omitted from the header when absent."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    github: str | None = None

    @property
    def first_name(self) -> str | None:
        parts = (self.name or "").split()
        return parts[0] if parts else None

    @property
    def last_name(self) -> str | None:
        parts = (self.name or "").split()
        return parts[-1] if parts else None


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def contact_problems(contact: ContactInfo) -> list[str]:
    """Empty list = fine. `name`/`email` are required; a filled optional field that still holds
    the example placeholder is reported too (it would otherwise ship on a real resume)."""
    problems: list[str] = []
    for field in ("name", "email", "phone", "linkedin", "github"):
        value = (getattr(contact, field) or "").strip()
        required = field in ("name", "email")
        if not value:
            if required:
                problems.append(f"{field}: missing")
        elif _is_placeholder(value):
            problems.append(f"{field}: still the example placeholder ({value!r})")
    return problems
```

Add `contact: ContactInfo = Field(default_factory=ContactInfo)` as the last field of `CandidateProfile`.

In `config/candidate_profile.example.yaml` append:

```yaml
# Applicant contact details for the resume-generator / outreach-writer skills. This file is
# git-ignored; fill in your own values. `name` and `email` are required; phone/linkedin/github are
# optional and omitted from the resume header when left out.
contact:
  name: "Your Name"
  email: "you@example.com"
  phone: "+1 555 555 0100"
  linkedin: "https://www.linkedin.com/in/your-handle"
  github: "https://github.com/your-handle"
```

In `src/job_hunter/cli.py`: extend the config import with `contact_problems`; after the `resume-files` parser add `sub.add_parser("contact")`; and in `main()`, after the `resume-files` block, add

```python
        if args.command == "contact":
            try:
                contact = load_profile().contact
            except FileNotFoundError:
                contact = None
            problems = contact_problems(contact) if contact else ["contact: no candidate_profile.yaml found"]
            if problems:
                print(
                    "job-hunter: contact details are not ready — edit the `contact:` block in "
                    "config/candidate_profile.yaml:\n  " + "\n  ".join(problems),
                    file=sys.stderr,
                )
                return 2
            payload = {k: v for k, v in contact.model_dump().items() if v}
            payload["first_name"] = contact.first_name
            payload["last_name"] = contact.last_name
            print(_json(payload))
            return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contact.py tests/test_cli.py tests/test_diff_profile.py tests/test_config.py -q && uv run ruff check src tests`
Expected: PASS. (`test_diff_profile.py` is included because `scripts/diff_profile.py` iterates `CandidateProfile.model_fields`; if a diff test fails because of the new `contact` field, make `diff_profile.py` skip `contact` exactly the way it skips other non-filter fields, and mention it in your report — do not weaken the test.)

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/config.py src/job_hunter/cli.py config/candidate_profile.example.yaml tests/test_contact.py tests/test_cli.py
git commit -m "feat(resume): contact block in the profile with placeholder detection and a contact CLI" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: JD export (`jd_export.py`, `Storage.get_job_for_jd`, `export-jd` CLI)

**Files:**
- Create: `src/job_hunter/jd_export.py`
- Modify: `src/job_hunter/storage.py`, `src/job_hunter/cli.py`
- Test: `tests/test_jd_export.py` (create), `tests/test_cli.py`

**Interfaces:**
- Consumes: `Storage`, `atomic.atomic_write_text`.
- Produces:
  - `Storage.get_job_for_jd(source_key, job_id) -> dict | None` with keys `source_key, job_id, company, title, department, location_raw, posted_at, url, salary_evidence, description`.
  - `jd_export.JobNotFound(LookupError)`, `jd_export.JobHasNoDescription(ValueError)`
  - `jd_export.ExportResult(path: Path, relative_path: str, created: bool, prompt: str)` (frozen; `path` absolute, `relative_path` POSIX-style relative to the project root)
  - `jd_export.html_to_text(value: str) -> str`, `jd_export.sanitize(text: str | None, fallback: str) -> str`, `jd_export.render_jd_text(job: dict, description_text: str) -> str`
  - `jd_export.export_jd(storage, source_key, job_id, *, project_root: Path, output_root: Path, today: date) -> ExportResult`
  - CLI `job-hunter export-jd <source_key> <job_id>` → stdout JSON `{path, relative_path, created, prompt}`; exit 1 + stderr message for a missing job / no description.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jd_export.py`:

```python
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from job_hunter.jd_export import (
    JobHasNoDescription,
    JobNotFound,
    export_jd,
    html_to_text,
    render_jd_text,
    sanitize,
)
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage

TODAY = date(2026, 9, 26)


def _job(job_id="42", title="ADAS Systems Engineer II (Hybrid)", company="Acme Motors, Inc.",
         description="<p>Build <b>ADAS</b> features.</p><ul><li>C++</li><li>Python &amp; ROS</li></ul>",
         **over):
    values = dict(
        source_key="acme", source_platform="test", company=company, job_id=job_id, title=title,
        url=f"https://example.com/jobs/{job_id}", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, description=description, content_hash="h",
        location_raw="Detroit, MI", department="Autonomy",
        posted_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    values.update(over)
    return Job(**values)


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "data" / "jobs.sqlite3")
    yield storage, tmp_path
    storage.close()


def _export(storage, root, job_id="42", today=TODAY):
    return export_jd(
        storage, "acme", job_id, project_root=root, output_root=root / "data" / "output", today=today
    )


# --- pure helpers ---------------------------------------------------------------------------

def test_html_to_text_keeps_paragraphs_and_bullets_and_decodes_entities():
    text = html_to_text("<p>Build <b>ADAS</b> features.</p><ul><li>C++</li><li>Python &amp; ROS</li></ul><p>A&nbsp;B</p>")
    assert text == "Build ADAS features.\n\n- C++\n- Python & ROS\n\nA B"


def test_html_to_text_drops_scripts_and_styles_and_keeps_plain_text_unchanged():
    assert html_to_text("Hello<script>alert(1)</script><style>p{}</style> world") == "Hello world"
    assert html_to_text("Salary < 100k and > 50k\nSecond line") == "Salary < 100k and > 50k\nSecond line"
    assert html_to_text("a<br>b<br/>c") == "a\nb\nc"


def test_sanitize_allows_only_safe_filename_characters():
    assert sanitize("Acme Motors, Inc.", "Company") == "Acme_Motors_Inc"
    assert sanitize("../../etc/passwd", "Job") == "etcpasswd"
    assert sanitize("..\\..\\windows", "Job") == "windows"
    assert sanitize("C++ / C# Engineer", "Job") == "C_C_Engineer"
    assert sanitize("   ", "Job") == "Job"
    assert sanitize("---___", "Job") == "Job"
    assert sanitize("Ünïcödé Rôle", "Job") == "ncd_Rle"
    assert "/" not in sanitize("a/b", "x") and "\\" not in sanitize("a\\b", "x")


def test_render_jd_text_has_the_documented_layout():
    job = {"title": "ADAS Engineer", "location_raw": "Detroit, MI", "department": "Autonomy",
           "posted_at": "2026-09-20T00:00:00+00:00", "job_id": "42", "url": "https://x.test/42",
           "company": "Acme", "source_key": "acme", "salary_evidence": "$100,000 - $120,000"}
    text = render_jd_text(job, "Build things.")
    assert text == (
        "ADAS Engineer\nDetroit, MI\nAutonomy\n\nSummary\nPosted: 2026-09-20\nJob ID: 42\n"
        "Job URL: https://x.test/42\nSource: Acme (acme)\n\nDescription\nBuild things.\n\n"
        "Pay & Benefits\n$100,000 - $120,000\n"
    )
    bare = render_jd_text({**job, "department": None, "salary_evidence": None, "posted_at": None,
                           "location_raw": None}, "Body")
    assert bare.startswith("ADAS Engineer\nNot specified\n\nSummary\nPosted: Not specified\n")
    assert "Pay & Benefits" not in bare and bare.endswith("Body\n")


# --- export ---------------------------------------------------------------------------------

def test_export_writes_the_file_and_returns_a_relative_prompt(env):
    storage, root = env
    storage.upsert_job(_job())
    result = _export(storage, root)
    assert result.created is True
    assert result.path == (root / "data" / "output" / "Acme_Motors_Inc"
                           / "JD_Acme_Motors_Inc_ADAS_Systems_Engineer_II_2026-09-26.txt").resolve()
    assert result.relative_path == "data/output/Acme_Motors_Inc/JD_Acme_Motors_Inc_ADAS_Systems_Engineer_II_2026-09-26.txt"
    assert result.prompt == f"Use the resume-generator skill on {result.relative_path}"
    body = result.path.read_text(encoding="utf-8")
    assert body.startswith("ADAS Systems Engineer II (Hybrid)\nDetroit, MI\nAutonomy\n")
    assert "Posted: 2026-09-20" in body and "Job ID: 42" in body
    assert "- C++\n- Python & ROS" in body and "<" not in body.split("Description")[1]
    assert str(root) not in result.relative_path


def test_reexport_of_identical_content_reuses_the_file(env):
    storage, root = env
    storage.upsert_job(_job())
    first = _export(storage, root)
    second = _export(storage, root)
    assert (second.created, second.path) == (False, first.path)
    assert len(list(first.path.parent.glob("JD_*.txt"))) == 1


def test_changed_description_the_same_day_gets_a_numbered_file_and_keeps_the_old_one(env):
    storage, root = env
    storage.upsert_job(_job())
    first = _export(storage, root)
    original = first.path.read_text(encoding="utf-8")
    storage.connection.execute(
        "UPDATE jobs SET description='<p>Completely different role now.</p>' WHERE job_id='42'"
    )
    storage.connection.commit()
    second = _export(storage, root)
    assert second.created is True and second.path.name.endswith("_2026-09-26_2.txt")
    assert first.path.read_text(encoding="utf-8") == original  # the JD an earlier CV used is intact
    third = _export(storage, root)
    assert (third.created, third.path) == (False, second.path)


def test_a_different_day_gets_its_own_dated_file(env):
    storage, root = env
    storage.upsert_job(_job())
    a = _export(storage, root, today=date(2026, 9, 26))
    b = _export(storage, root, today=date(2026, 9, 27))
    assert a.path != b.path and b.created is True


def test_missing_job_and_empty_description_write_nothing(env):
    storage, root = env
    with pytest.raises(JobNotFound):
        _export(storage, root, job_id="nope")
    storage.upsert_job(_job(job_id="7", description="   "))
    with pytest.raises(JobHasNoDescription):
        _export(storage, root, job_id="7")
    storage.upsert_job(_job(job_id="8", description="<p> </p><br>"))  # markup with no real text
    with pytest.raises(JobHasNoDescription):
        _export(storage, root, job_id="8")
    assert not (root / "data" / "output").exists()


def test_hostile_titles_and_companies_cannot_escape_the_output_folder(env):
    storage, root = env
    storage.upsert_job(_job(job_id="9", company="../../evil", title="..\\..\\x/y\\z <script> " + "A" * 400))
    result = _export(storage, root, job_id="9")
    output_root = (root / "data" / "output").resolve()
    assert output_root in result.path.resolve().parents
    assert len(result.path.name) < 200
    assert all(part.replace("_", "").replace("-", "").replace(".", "").isalnum()
               for part in result.path.relative_to(output_root).parts)


def test_pay_section_appears_only_when_the_job_has_salary_evidence(env):
    storage, root = env
    storage.upsert_job(_job())
    storage.connection.execute("UPDATE jobs SET salary_evidence='$90,000 - $110,000' WHERE job_id='42'")
    storage.connection.commit()
    assert "Pay & Benefits\n$90,000 - $110,000" in _export(storage, root).path.read_text(encoding="utf-8")


def test_get_job_for_jd_is_the_only_reader_of_description(env):
    storage, _ = env
    storage.upsert_job(_job())
    row = storage.get_job_for_jd("acme", "42")
    assert row["description"].startswith("<p>Build")
    assert "description" not in storage.get_job_snapshot("acme", "42")
    assert storage.get_job_for_jd("acme", "missing") is None
```

Append to `tests/test_cli.py` (add `from datetime import UTC, datetime` if absent and `from job_hunter.models import Job, LocationConfidence`):

```python
def test_export_jd_writes_the_file_and_prints_json(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    with Storage(tmp_path / "data" / "jobs.sqlite3") as storage:
        storage.upsert_job(Job(
            source_key="acme", source_platform="t", company="Acme", job_id="1", title="Engineer",
            url="https://example.com/1", us_eligible=True, location_confidence=LocationConfidence.HIGH,
            description="<p>Do work.</p>", content_hash="h",
        ))
    assert main(["export-jd", "acme", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["relative_path"].startswith("data/output/Acme/JD_Acme_Engineer_")
    assert (tmp_path / payload["relative_path"]).read_text(encoding="utf-8").startswith("Engineer\n")
    assert payload["prompt"].startswith("Use the resume-generator skill on data/output/Acme/")


def test_export_jd_unknown_job_exits_1(tmp_path, monkeypatch, capsys):
    _bare_project(tmp_path, monkeypatch)
    assert main(["export-jd", "acme", "nope"]) == 1
    assert "not found" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jd_export.py tests/test_cli.py -q`
Expected: collection error (`ModuleNotFoundError: job_hunter.jd_export`).

- [ ] **Step 3: Implement the storage method**

In `src/job_hunter/storage.py`, directly after `get_job_snapshot`:

```python
    def get_job_for_jd(self, source_key: str, job_id: str) -> dict[str, Any] | None:
        """The one reader of `description` for JD export — the fields a resume/outreach skill needs
        to tailor against a job, by primary key."""
        row = self.connection.execute(
            """SELECT source_key, job_id, company, title, department, location_raw, posted_at,
               canonical_url AS url, salary_evidence, description
               FROM jobs WHERE source_key=? AND job_id=?""",
            (source_key, job_id),
        ).fetchone()
        return dict(row) if row else None
```

- [ ] **Step 4: Implement `jd_export.py`**

Create `src/job_hunter/jd_export.py`:

```python
"""Export one collected job's description as the plain-text JD file the resume/outreach skills
consume (spec section 6.3). Python owns this retrieval; the skills never read SQLite.

The file is a deliberate subset of the format the standalone scraper used: header fields plus the
whole description as plain text. Sections that need an LLM to split (Minimum/Preferred
Qualifications) are *not* fabricated here; the skills read the full description.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .atomic import atomic_write_text
from .storage import Storage


class JobNotFound(LookupError):
    pass


class JobHasNoDescription(ValueError):
    pass


@dataclass(frozen=True)
class ExportResult:
    path: Path
    relative_path: str
    created: bool
    prompt: str


_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BREAK = re.compile(r"<br\b[^>]*>", re.I)
_LI_START = re.compile(r"<li\b[^>]*>", re.I)
_BLOCK_END = re.compile(r"</(?:p|div|h[1-6]|ul|ol|table|tr|section|article|blockquote)\s*>", re.I)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_PART = 60


def html_to_text(value: str) -> str:
    """Readable plain text from an HTML (or already-plain) job description: paragraph/list/line
    breaks are kept, bullets become '- ', entities are decoded, scripts/styles are dropped. A bare
    '<' that isn't a tag (e.g. 'salary < 100k') is left alone."""
    text = _SCRIPT_STYLE.sub("", value)
    text = _BREAK.sub("\n", text)
    text = _LI_START.sub("\n- ", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def sanitize(text: str | None, fallback: str) -> str:
    """A filename component: whitespace -> '_', everything outside [A-Za-z0-9_-] dropped, runs of
    '_' collapsed, edge '_'/'-' trimmed, length-capped. Never contains a path separator; `fallback` if nothing is left."""
    cleaned = re.sub(r"_+", "_", _UNSAFE.sub("", re.sub(r"\s+", "_", (text or "").strip())))
    return cleaned[:_MAX_PART].strip("_-") or fallback


def _first_words(title: str | None, count: int = 4) -> str:
    return " ".join((title or "").split()[:count])


def render_jd_text(job: dict, description_text: str) -> str:
    posted = (job.get("posted_at") or "")[:10]
    posted = posted if _ISO_DAY.fullmatch(posted) else "Not specified"
    header = [job.get("title") or "Not specified", job.get("location_raw") or "Not specified"]
    if job.get("department"):
        header.append(job["department"])
    parts = [
        "\n".join(header),
        "Summary\n"
        f"Posted: {posted}\n"
        f"Job ID: {job['job_id']}\n"
        f"Job URL: {job.get('url') or 'Not specified'}\n"
        f"Source: {job['company']} ({job['source_key']})",
        f"Description\n{description_text}",
    ]
    if job.get("salary_evidence"):
        parts.append(f"Pay & Benefits\n{job['salary_evidence']}")
    return "\n\n".join(parts) + "\n"


def export_jd(
    storage: Storage, source_key: str, job_id: str, *, project_root: Path, output_root: Path,
    today: date,
) -> ExportResult:
    """Write (or reuse) `<output_root>/<Company>/JD_<Company>_<Title>_<YYYY-MM-DD>[_N].txt`.

    Identical content the same day reuses the existing file; a *changed* description gets the next
    numbered file so the JD an earlier tailored CV was written against is never overwritten."""
    job = storage.get_job_for_jd(source_key, job_id)
    if job is None:
        raise JobNotFound(f"job {source_key}/{job_id} not found")
    body = html_to_text(job.get("description") or "")
    if not body:
        raise JobHasNoDescription(f"job {source_key}/{job_id} has no description text")
    text = render_jd_text(job, body)
    company = sanitize(job["company"], "Company")
    base = f"JD_{company}_{sanitize(_first_words(job['title']), 'Job')}_{today.isoformat()}"
    directory = Path(output_root) / company
    number = 1
    while True:
        path = directory / (f"{base}.txt" if number == 1 else f"{base}_{number}.txt")
        if not path.exists():
            atomic_write_text(path, text)
            created = True
            break
        if path.read_text(encoding="utf-8") == text:
            created = False
            break
        number += 1
    relative = Path(os.path.relpath(path.resolve(), Path(project_root).resolve())).as_posix()
    return ExportResult(path.resolve(), relative, created, f"Use the resume-generator skill on {relative}")
```

- [ ] **Step 5: Implement the CLI command**

In `src/job_hunter/cli.py`: add `from .jd_export import JobHasNoDescription, JobNotFound, export_jd` to the imports and `from datetime import date`; add after the `contact` parser

```python
    export_jd_cmd = sub.add_parser("export-jd")
    export_jd_cmd.add_argument("source_key")
    export_jd_cmd.add_argument("job_id")
```

and in `main()`, after the `contact` block:

```python
        if args.command == "export-jd":
            try:
                with Storage(settings.database_path) as storage:
                    result = export_jd(
                        storage, args.source_key, args.job_id, project_root=Path.cwd(),
                        output_root=settings.database_path.parent / "output", today=date.today(),
                    )
            except (JobNotFound, JobHasNoDescription) as exc:
                print(f"job-hunter: {exc}", file=sys.stderr)
                return 1
            print(_json({
                "path": str(result.path), "relative_path": result.relative_path,
                "created": result.created, "prompt": result.prompt,
            }))
            return 0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_jd_export.py tests/test_cli.py tests/test_storage.py -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/jd_export.py src/job_hunter/storage.py src/job_hunter/cli.py tests/test_jd_export.py tests/test_cli.py
git commit -m "feat(resume): export a collected job's description as a JD text file" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: `measure_resume.py`, `log_resume.py`, optional `resume` extra

**Files:**
- Create: `scripts/measure_resume.py`, `scripts/log_resume.py`
- Modify: `pyproject.toml`, `uv.lock`
- Test: `tests/test_measure_resume.py`, `tests/test_log_resume.py` (create)

**Interfaces:**
- Consumes: `rootutil.add_project_argument`/`chdir_to_project_root`, `applications_export.csv_safe`.
- Produces:
  - `measure_resume.analyze(content_height: int, page_count: int, target_pages: float) -> dict` (pure) with keys `status` (`ok|underflow|overflow`), `pages`, `target_pages`, `last_page_fill_pct`, `fill_pct`, `content_height_px`, `page_height_px`, `delta_px`, `delta_lines`, `guidance`.
  - `measure_resume.measure(html_path: Path, pdf_output: Path | None = None, target_pages: float = 1.0) -> dict` (uses Playwright; raises `MissingDependency` with the fix text).
  - CLI `uv run python scripts/measure_resume.py <html> [--target-pages 1|1.5|2] [--save-pdf OUT.pdf] [--project P]` → JSON on stdout; missing extra/Chromium → install commands on stderr + exit 2; missing file → JSON error + exit 1.
  - `log_resume.HEADERS`, `log_resume.append_row(path: Path, row: dict) -> None`, CLI `uv run python scripts/log_resume.py --file F --company C [--role R] [--url U] [--fill N] [--pages N] [--iterations N] --date D [--project P]` appending to `data/output/resume_log.csv`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_measure_resume.py`:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from measure_resume import PAGE_HEIGHT_PX, analyze  # noqa: E402


def test_one_page_target_ok_when_the_last_page_is_well_filled():
    result = analyze(900, 1, 1.0)
    assert result["status"] == "ok"
    assert result["last_page_fill_pct"] == 93.8
    assert result["pages"] == 1 and result["target_pages"] == 1.0


def test_one_page_target_underflow_reports_lines_to_add():
    result = analyze(500, 1, 1.0)
    assert result["status"] == "underflow"
    assert result["last_page_fill_pct"] == 52.1
    assert "Add ~" in result["guidance"]


def test_one_page_target_overflow_when_content_spills_to_a_second_page():
    result = analyze(1200, 2, 1.0)
    assert result["status"] == "overflow"
    assert "OVERFLOW" in result["guidance"] and result["delta_px"] == 1200 - PAGE_HEIGHT_PX


def test_half_page_target_uses_the_35_to_70_percent_band():
    assert analyze(PAGE_HEIGHT_PX + 480, 2, 1.5)["status"] == "ok"          # last page 50%
    assert analyze(PAGE_HEIGHT_PX + 100, 2, 1.5)["status"] == "underflow"   # ~10%
    assert analyze(PAGE_HEIGHT_PX + 800, 2, 1.5)["status"] == "overflow"    # ~83%
    assert analyze(2 * PAGE_HEIGHT_PX + 200, 3, 1.5)["status"] == "overflow"  # a third page


def test_two_page_target_needs_a_nearly_full_second_page():
    assert analyze(PAGE_HEIGHT_PX + 890, 2, 2.0)["status"] == "ok"          # last page ~92.7%
    assert analyze(PAGE_HEIGHT_PX + 600, 2, 2.0)["status"] == "underflow"   # ~62.5%
    assert analyze(700, 1, 2.0)["status"] == "underflow"                    # never reached page 2


def test_legacy_fill_pct_is_raw_content_over_one_page():
    assert analyze(1440, 2, 1.5)["fill_pct"] == 150.0


def test_a_real_render_when_playwright_and_chromium_are_available(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("pypdf")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch().close()
    except Exception:  # noqa: BLE001 - no browser installed on this machine
        pytest.skip("Chromium is not installed (run: uv run playwright install chromium)")
    from measure_resume import measure

    html = tmp_path / "r.html"
    html.write_text("<html><body><p>" + "word " * 50 + "</p></body></html>")
    out = tmp_path / "r.pdf"
    result = measure(html, out, 1.0)
    assert out.exists() and result["pages"] == 1
    assert result["status"] == "underflow"  # a tiny document is far under the 88% band
```

Create `tests/test_log_resume.py`:

```python
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from log_resume import HEADERS, append_row  # noqa: E402


def _row(**over):
    row = {"Date": "2026-09-26", "Company": "Acme", "Role": "ADAS Engineer", "Job_URL": "https://x.test/1",
           "Fill_Pct": "93.8", "Pages": "1", "Iterations": "2", "Resume_File": "Doe_CV_Acme_ADAS_2026-09-26.html"}
    row.update(over)
    return row


def test_header_is_written_once_and_rows_append(tmp_path):
    log = tmp_path / "data" / "output" / "resume_log.csv"
    append_row(log, _row())
    append_row(log, _row(Company="Beta", Iterations="1"))
    rows = list(csv.reader(log.open(encoding="utf-8")))
    assert rows[0] == HEADERS == ["Date", "Company", "Role", "Job_URL", "Fill_Pct", "Pages", "Iterations", "Resume_File"]
    assert [r[1] for r in rows[1:]] == ["Acme", "Beta"]


def test_cells_that_look_like_spreadsheet_formulas_are_neutralised(tmp_path):
    log = tmp_path / "resume_log.csv"
    append_row(log, _row(Role="=HYPERLINK(\"http://evil\")", Company="+cmd"))
    row = list(csv.DictReader(log.open(encoding="utf-8")))[0]
    assert row["Role"].startswith("'=") and row["Company"] == "'+cmd"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_measure_resume.py tests/test_log_resume.py -q`
Expected: collection error (`ModuleNotFoundError: measure_resume`).

- [ ] **Step 3: Implement `scripts/measure_resume.py`**

```python
#!/usr/bin/env python3
"""Render an HTML resume (or cover letter) in headless Chromium and report whether it fits the
target US Letter page count (0.5" margins on all sides); optionally save the PDF.

Usage:
    uv run python scripts/measure_resume.py <file.html> [--target-pages 1|1.5|2] [--save-pdf OUT.pdf]

Prints one JSON object (status ok|underflow|overflow, pages, last_page_fill_pct, guidance, ...) that
the resume-generator / outreach-writer skills read to decide whether to trim, expand or accept.
Needs the optional `resume` extra plus a one-time Chromium download:
    uv sync --extra resume && uv run playwright install chromium
Fill percentages are measured on the machine that renders the PDF — fonts differ between
operating systems, so re-measure per machine rather than assuming a result is portable.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

from job_hunter.rootutil import add_project_argument, chdir_to_project_root

# US Letter at 96 dpi with 0.5in margins: content 960 px tall, 720 px wide.
PAGE_HEIGHT_PX = 960
LINE_HEIGHT_PX = 18   # ~10.5pt body at 1.2-1.25 line-height in Chromium
GOOD_FILL_MIN = 88    # % — below this a full-page target shows visible white space at the bottom
# For a half-page target (1.5 pages) the last page is deliberately only partly filled.
HALF_PAGE_FILL_BAND = (35, 70)

INSTALL_HELP = (
    "PDF rendering needs the optional resume dependencies. Install them with:\n"
    "    uv sync --extra resume\n"
    "    uv run playwright install chromium\n"
    "(on a bare Linux host add system libraries with: uv run playwright install --with-deps chromium)"
)


class MissingDependency(RuntimeError):
    pass


def analyze(content_height: int, page_count: int, target_pages: float) -> dict:
    """Pure fill/status logic (no browser): how the rendered document compares with the target."""
    expected_pages = math.ceil(target_pages)
    is_half_target = (target_pages * 2) % 2 == 1
    fill_pct = round(content_height / PAGE_HEIGHT_PX * 100, 1)  # legacy: raw height over one page
    last_page_content_px = content_height - (page_count - 1) * PAGE_HEIGHT_PX
    last_page_fill_pct = round(max(last_page_content_px, 0) / PAGE_HEIGHT_PX * 100, 1)
    delta_px = content_height - expected_pages * PAGE_HEIGHT_PX
    delta_lines = round(abs(delta_px) / LINE_HEIGHT_PX)

    if page_count > expected_pages:
        status = "overflow"
        guidance = (
            f"OVERFLOW: rendered {page_count} pages but target is {target_pages} "
            f"(expected {expected_pages}). Content is {abs(delta_px)}px too tall "
            f"(~{delta_lines} lines over). Trim bullets or shorten existing ones to fit."
        )
    elif page_count < expected_pages:
        status = "underflow"
        guidance = (
            f"UNDERFLOW: rendered {page_count} page(s) but target is {target_pages} "
            f"(expected {expected_pages}). Content is {abs(delta_px)}px short "
            f"(~{delta_lines} lines) of even reaching the target page count. Expand bullets or add content."
        )
    else:
        band_low, band_high = HALF_PAGE_FILL_BAND if is_half_target else (GOOD_FILL_MIN, 100)
        if last_page_fill_pct < band_low:
            status = "underflow"
            short_px = round(band_low / 100 * PAGE_HEIGHT_PX - last_page_content_px)
            guidance = (
                f"UNDERFLOW: {page_count} page(s), last page only {last_page_fill_pct}% full "
                f"(target band {band_low}-{band_high}% for a {target_pages}-page resume). "
                f"Add ~{round(short_px / LINE_HEIGHT_PX)} lines to the last page."
            )
        elif last_page_fill_pct > band_high:
            status = "overflow"
            over_px = round(last_page_content_px - band_high / 100 * PAGE_HEIGHT_PX)
            guidance = (
                f"OVERFLOW: {page_count} page(s), last page {last_page_fill_pct}% full, over the "
                f"{band_high}% band for a {target_pages}-page resume by ~{round(over_px / LINE_HEIGHT_PX)} lines. Trim slightly."
            )
        else:
            status = "ok"
            guidance = (
                f"OK: {page_count} page(s) matching the {target_pages}-page target, "
                f"last page {last_page_fill_pct}% full (good fill, no changes needed)."
            )
    return {
        "status": status, "pages": page_count, "target_pages": target_pages,
        "last_page_fill_pct": last_page_fill_pct, "fill_pct": fill_pct,
        "content_height_px": content_height, "page_height_px": PAGE_HEIGHT_PX,
        "delta_px": delta_px, "delta_lines": delta_lines, "guidance": guidance,
    }


def measure(html_path: Path, pdf_output: Path | None = None, target_pages: float = 1.0) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependency(INSTALL_HELP) from exc

    delete_pdf = pdf_output is None
    pdf_path = str(pdf_output) if pdf_output else tempfile.mktemp(suffix=".pdf")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # Height=1 makes scrollHeight the true content height; width 816 = 8.5in at 96 dpi.
            page = browser.new_page(viewport={"width": 816, "height": 1})
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            content_height = page.evaluate("document.documentElement.scrollHeight")
            page.pdf(
                path=pdf_path, format="Letter",
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                print_background=True,
            )
            browser.close()
    except Exception as exc:  # noqa: BLE001 - Playwright raises its own Error type
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            raise MissingDependency(INSTALL_HELP) from exc
        raise
    page_count = len(PdfReader(pdf_path).pages)
    if delete_pdf:
        Path(pdf_path).unlink(missing_ok=True)
    return analyze(int(content_height), page_count, target_pages)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("html", help="path to the HTML file")
    parser.add_argument("--save-pdf", metavar="PDF", help="also save the rendered PDF here")
    parser.add_argument("--target-pages", type=float, default=1.0, help="intended page count: 1, 1.5 or 2 (default 1)")
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    html_path = Path(args.html)
    if not html_path.exists():
        print(json.dumps({"error": f"File not found: {html_path}"}))
        return 1
    try:
        result = measure(html_path, Path(args.save_pdf) if args.save_pdf else None, args.target_pages)
    except MissingDependency as exc:
        print(f"job-hunter: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Implement `scripts/log_resume.py`**

```python
#!/usr/bin/env python3
"""Append one row to data/output/resume_log.csv after a resume is generated.

Usage:
    uv run python scripts/log_resume.py --file Doe_CV_Acme_ADAS_2026-09-26.html --company Acme \\
        --role "ADAS Engineer" --url https://... --fill 93.8 --pages 1 --iterations 2 --date 2026-09-26

`--fill` is the *last page's* fill percentage from measure_resume.py (`last_page_fill_pct`).
Cells that look like spreadsheet formulas are prefixed with ' so the CSV is safe to open in Excel.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from job_hunter.applications_export import csv_safe
from job_hunter.rootutil import add_project_argument, chdir_to_project_root

HEADERS = ["Date", "Company", "Role", "Job_URL", "Fill_Pct", "Pages", "Iterations", "Resume_File"]
LOG_PATH = Path("data/output/resume_log.csv")


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if write_header:
            writer.writerow(HEADERS)
        writer.writerow([csv_safe(row.get(column, "")) for column in HEADERS])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", default="Not specified")
    parser.add_argument("--url", default="Not specified")
    parser.add_argument("--fill", default="?")
    parser.add_argument("--pages", default="?")
    parser.add_argument("--iterations", default="?")
    parser.add_argument("--date", required=True)
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    append_row(LOG_PATH, {
        "Date": args.date, "Company": args.company, "Role": args.role, "Job_URL": args.url,
        "Fill_Pct": args.fill, "Pages": args.pages, "Iterations": args.iterations, "Resume_File": args.file,
    })
    print(f"Logged: {args.company} | {args.role} | {args.fill}% | {args.iterations} iteration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Add the optional extra**

In `pyproject.toml`, inside `[project.optional-dependencies]` (after `stealth`), add:

```toml
# Only needed for the resume-generator / outreach-writer skills' PDF output: renders the HTML
# resume in headless Chromium and counts pages. Install with `uv sync --extra resume`, then run
# `uv run playwright install chromium` once to fetch the browser.
resume = ["playwright>=1.45,<2", "pypdf>=5,<7"]
```

Run `uv lock` (network required) and commit the updated `uv.lock`. Verify the default suite does not need the extra: `uv sync --dev` then `uv run pytest -q tests/test_measure_resume.py` must pass with the render test **skipped**.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_measure_resume.py tests/test_log_resume.py -q && uv run ruff check scripts tests`
Expected: PASS (the browser test skips without Chromium). If Chromium is installed locally (`uv sync --extra resume && uv run playwright install chromium`) also run it un-skipped once and note the result in the report.

- [ ] **Step 7: Commit**

```bash
git add scripts/measure_resume.py scripts/log_resume.py pyproject.toml uv.lock tests/test_measure_resume.py tests/test_log_resume.py
git commit -m "feat(resume): HTML resume measure/PDF and log scripts behind an optional resume extra" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: `POST /api/jd` on the live server

**Files:**
- Modify: `scripts/serve_radar.py`
- Test: `tests/test_serve_radar.py`

**Interfaces:**
- Consumes: `jd_export.export_jd`/`JobNotFound`/`JobHasNoDescription` (Task 3).
- Produces: `POST /api/jd` body `{source_key, job_id}` (`extra="forbid"`, no `client_ts`) → `200 {ok, path, created, prompt}` (`path` project-relative, never absolute) | `404 {ok:false, error:"unknown job"}` | `409 {ok:false, error:"job has no description"}` | the existing 400/403/411/413/415/500/503 responses. `serve_radar.JdExport`, `serve_radar.write_jd(storage, req, *, project_root, output_root, today) -> tuple[int, dict]`. The output root is `settings.database_path.parent / "output"`; the project root is the server's cwd (already the project root after `chdir_to_project_root`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_serve_radar.py` (add `import pytest` if absent). The new fixture chdirs into the temp project so relative paths are meaningful:

```python
@pytest.fixture
def jd_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    e = Env(tmp_path)
    yield e
    e.close()


def post_jd(env, job_id="1", **extra):
    body = {"source_key": "acme", "job_id": job_id}
    body.update(extra)
    return env.request("POST", "/api/jd", body)


def test_jd_export_writes_the_file_and_returns_a_relative_path_and_prompt(jd_env):
    status, _, body = post_jd(jd_env)
    assert status == 200 and body["ok"] is True and body["created"] is True
    assert body["path"].startswith("data/output/Acme/JD_Acme_ADAS_Engineer_")
    assert not body["path"].startswith("/") and str(jd_env.root) not in json.dumps(body)
    assert body["prompt"] == f"Use the resume-generator skill on {body['path']}"
    text = (jd_env.root / body["path"]).read_text(encoding="utf-8")
    assert text.startswith("ADAS Engineer\n") and "secret body" in text and "Job ID: 1" in text


def test_a_second_click_reuses_the_file_and_a_changed_description_gets_a_new_one(jd_env):
    first = post_jd(jd_env)[2]
    second = post_jd(jd_env)[2]
    assert (second["created"], second["path"]) == (False, first["path"])
    with Storage(jd_env.db) as storage:
        storage.connection.execute("UPDATE jobs SET description='A new posting body.' WHERE job_id='1'")
        storage.connection.commit()
    third = post_jd(jd_env)[2]
    assert third["created"] is True and third["path"].endswith("_2.txt")
    assert (jd_env.root / first["path"]).read_text(encoding="utf-8").count("secret body") == 1


def test_jd_export_unknown_job_is_404_and_empty_description_is_409_with_no_file(jd_env):
    assert post_jd(jd_env, job_id="nope")[0] == 404
    with Storage(jd_env.db) as storage:
        storage.connection.execute("UPDATE jobs SET description='' WHERE job_id='2'")
        storage.connection.commit()
    status, _, body = post_jd(jd_env, job_id="2")
    assert status == 409 and body["error"] == "job has no description"
    assert not list((jd_env.root / "data" / "output").glob("**/JD_*Other*"))


def test_jd_export_uses_the_same_request_guards(jd_env):
    assert post_jd(jd_env, client_ts="2026-01-01T00:00:00+00:00")[0] == 400  # extra="forbid"
    assert jd_env.request("POST", "/api/jd", {"source_key": " ", "job_id": "1"})[0] == 400
    assert jd_env.request("POST", "/api/jd", {"source_key": "acme"})[0] == 400
    payload = json.dumps({"source_key": "acme", "job_id": "1"})
    assert jd_env.request("POST", "/api/jd", raw=payload, headers={"Content-Type": "text/plain"})[0] == 415
    bad_origin = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert jd_env.request("POST", "/api/jd", raw=payload, headers=bad_origin)[0] == 403
    assert jd_env.request("POST", "/api/jd", body={"source_key": "acme", "job_id": "1"},
                          headers={"Host": "evil.example"})[0] == 403
    assert jd_env.request("GET", "/api/jd")[0] == 404  # POST-only route


def test_a_write_failure_is_a_generic_500_that_leaks_no_path(jd_env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError(f"[Errno 28] No space left on device: '{jd_env.root}/secret/file.txt'")

    monkeypatch.setattr(serve_radar, "export_jd", boom)
    status, _, body = post_jd(jd_env)
    assert status == 500 and body == {"ok": False, "error": "internal error"}
```

(`Env`'s seeded job 1 is titled "ADAS Engineer" at company "Acme" with description "secret body"; job 2 is "Other Role".)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_serve_radar.py -q`
Expected: the five new tests FAIL (404 for `/api/jd`; `serve_radar.export_jd` missing).

- [ ] **Step 3: Implement**

In `scripts/serve_radar.py`:

1. Add to the imports: `from job_hunter.jd_export import JobHasNoDescription, JobNotFound, export_jd`.
2. Split the key fields out of `_KeyedWrite` so `JdExport` can reuse the validation without `client_ts`:

```python
class _JobKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=500)

    @field_validator("source_key", "job_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class _KeyedWrite(_JobKey):
    client_ts: datetime

    @field_validator("client_ts")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("client_ts must include a UTC offset")
        return value.astimezone(UTC)


class JdExport(_JobKey):
    """Only the two identifiers: the JD text always comes from the database, never the browser."""
```

(Replace the existing `_KeyedWrite` definition with the two classes above; `FeedbackWrite`/`ApplicationWrite` keep inheriting `_KeyedWrite` unchanged.)

3. Add after `write_application`:

```python
def write_jd(
    storage: Storage, req: JdExport, *, project_root: Path, output_root: Path, today: date
) -> tuple[int, dict[str, Any]]:
    """Export one job's JD file (spec section 6.7). Only project-relative paths ever leave the
    server; an unexpected failure (e.g. disk full) propagates to the generic 500 handler."""
    try:
        result = export_jd(
            storage, req.source_key, req.job_id, project_root=project_root,
            output_root=output_root, today=today,
        )
    except JobNotFound:
        return 404, {"ok": False, "error": "unknown job"}
    except JobHasNoDescription:
        return 409, {"ok": False, "error": "job has no description"}
    return 200, {"ok": True, "path": result.relative_path, "created": result.created, "prompt": result.prompt}
```

4. Add `"/api/jd": JdExport` to `_POST_ROUTES`.
5. In `do_POST`'s dispatch, restructure the `if isinstance(req, FeedbackWrite): ... else: <application>` into:

```python
                if isinstance(req, FeedbackWrite):
                    status, response = write_feedback(storage, req, now=datetime.now(UTC))
                elif isinstance(req, JdExport):
                    status, response = write_jd(
                        storage, req, project_root=Path.cwd(),
                        output_root=self.server.cfg.settings.database_path.parent / "output",
                        today=date.today(),
                    )
                else:
                    <the existing write_application + export-refresh block, unchanged>
```

6. Update the module docstring's first paragraph to mention JD export.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_serve_radar.py tests/test_render_radar.py -q && uv run ruff check scripts tests`
Expected: PASS (all earlier server tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add scripts/serve_radar.py tests/test_serve_radar.py
git commit -m "feat(radar): POST /api/jd exports a job's description file for the resume skill" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Client — `resumeNotice` and the Resume button handler (against a fixed DOM contract)

**Files:**
- Modify: `scripts/templates/radar_live_core.js`, `scripts/templates/radar_live_ui.js`, `tests/js/radar_live_core.test.js`

**Interfaces:**
- Consumes: `POST /api/jd` (Task 5): `200 {ok, path, created, prompt}`, `404`, `409`, others.
- **DOM contract Task 7 renders:** a `button.resume-btn` inside `.row-end` of every live row (both `details.row` and `div.plain-row`), after the Track chip; the script reads `data-source-key`/`data-job-id` from the row root.
- Produces: `RadarLive.resumeNotice(status, json, copied) -> string`; in the UI, a delegated click handler for `.resume-btn`; `notice(message, ms)` gains an optional duration (default 8000).

- [ ] **Step 1: Write the failing JS tests**

Append to `tests/js/radar_live_core.test.js`:

```js
test('resumeNotice: success copies the prompt or shows it for manual copying', () => {
  const json = { ok: true, path: 'data/output/Acme/JD_Acme_X_2026-09-26.txt', prompt: 'Use the resume-generator skill on data/output/Acme/JD_Acme_X_2026-09-26.txt' };
  const copied = core.resumeNotice(200, json, true);
  assert.match(copied, /Copied to clipboard/);
  assert.match(copied, /Claude Code or Hermes/);
  assert.ok(copied.includes(json.path));
  const manual = core.resumeNotice(200, json, false);
  assert.match(manual, /Copy this/);
  assert.ok(manual.includes(json.prompt));
});

test('resumeNotice: each failure status has its own clear message', () => {
  assert.match(core.resumeNotice(404, null, false), /no longer in the database/);
  assert.match(core.resumeNotice(409, null, false), /no description/);
  assert.match(core.resumeNotice(403, null, false), /rejected .*403/);
  assert.match(core.resumeNotice(415, null, false), /rejected .*415/);
  assert.match(core.resumeNotice(0, null, false), /Could not reach/);
  assert.match(core.resumeNotice(500, null, false), /Could not reach/);
  assert.match(core.resumeNotice(503, { error: 'x' }, false), /Could not reach/);
});

test('resumeNotice never throws on a malformed success body', () => {
  assert.match(core.resumeNotice(200, null, true), /Could not reach|unexpected/i);
  assert.match(core.resumeNotice(200, {}, true), /Could not reach|unexpected/i);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test tests/js/radar_live_core.test.js`
Expected: FAIL (`core.resumeNotice is not a function`).

- [ ] **Step 3: Implement the pure notice text**

In `scripts/templates/radar_live_core.js`, add above `var api = {`:

```js
  // Text for the notice shown after clicking a row's Resume button. `copied` says whether the
  // clipboard write succeeded; without it the prompt is shown so it can be copied by hand.
  function resumeNotice(status, json, copied) {
    if (status === 200) {
      if (!json || typeof json.prompt !== 'string' || typeof json.path !== 'string') {
        return 'Could not reach the radar server (unexpected reply); try again.';
      }
      return copied
        ? 'Copied to clipboard \u2014 paste it into Claude Code or Hermes. JD saved: ' + json.path
        : 'Copy this and paste it into Claude Code or Hermes: ' + json.prompt;
    }
    if (status === 404) return 'This job is no longer in the database, so its description can\u2019t be exported.';
    if (status === 409) return 'This job has no description to tailor a resume against.';
    if (status === 400 || status === 403 || status === 415 || status === 411 || status === 413) {
      return 'The server rejected the request (HTTP ' + status + ').';
    }
    return 'Could not reach the radar server; try again.';
  }
```

and add `resumeNotice: resumeNotice` to the `api` object.

- [ ] **Step 4: Implement the UI handler**

In `scripts/templates/radar_live_ui.js`:

1. Change `notice` to take an optional duration:

```js
  var noticeTimer = null;
  function notice(message, ms) {
    var el = $('live-notice');
    if (!el) return;
    el.textContent = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { el.textContent = ''; }, ms || 8000);
  }
```

2. Add after the Track-chip click/change handlers (before `pullApplications`):

```js
  // ---- Resume button: export the job's JD file, copy a ready-to-paste prompt -----------------
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return false; });
    }
    return Promise.resolve(false);
  }
  document.addEventListener('click', function (evt) {
    var btn = evt.target.closest ? evt.target.closest('.resume-btn') : null;
    if (!btn) return;
    // Nested inside <summary>: keep the click from toggling the row.
    evt.preventDefault();
    evt.stopPropagation();
    var row = btn.closest(ROW_SELECTOR);
    if (!row || btn.disabled) return;
    btn.disabled = true;
    function done() { btn.disabled = false; }
    fetch('/api/jd', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_key: row.dataset.sourceKey, job_id: row.dataset.jobId })
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (json) {
        return { status: res.status, json: json };
      });
    }).then(function (res) {
      if (res.status !== 200 || !res.json || typeof res.json.prompt !== 'string') {
        notice(L.resumeNotice(res.status, res.json, false));
        return;
      }
      return copyText(res.json.prompt).then(function (copied) {
        // A failed clipboard write keeps the prompt on screen longer so it can be copied by hand.
        notice(L.resumeNotice(200, res.json, copied), copied ? 8000 : 30000);
      });
    }, function () {
      notice(L.resumeNotice(0, null, false));
    }).then(done, done);
  });
```

(The Resume request is deliberately **not** queued through the offline outbox: it needs the server now, and a failed click simply reports and can be clicked again.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `node --check scripts/templates/radar_live_ui.js && node --check scripts/templates/radar_live_core.js && node --test tests/js/*.test.js && uv run pytest tests/test_radar_live_js.py -q`
Expected: PASS. (No automated DOM test exists for the UI file; the DOM is rendered by Task 7 and exercised by the controller's jsdom run and the manual pass in Task 10.)

- [ ] **Step 6: Commit**

```bash
git add scripts/templates/radar_live_core.js scripts/templates/radar_live_ui.js tests/js/radar_live_core.test.js
git commit -m "feat(radar): Resume button click handler and notice text in the live client" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Renderer — the Resume button (live mode only)

**Files:**
- Modify: `scripts/render_radar.py`
- Test: `tests/test_render_radar.py`

**Interfaces:**
- Consumes: the DOM contract in Task 6.
- Produces: in live output, `button.resume-btn` after the Track chip in `.row-end` of both row types; live-only CSS; **static output unchanged**.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_render_radar.py` (uses the `_live_apps_html` / `_golden_inputs` helpers from the Phase B tests):

```python
def test_static_render_has_no_resume_button(tmp_path):
    html, _ = render(**_golden_inputs(tmp_path))
    assert "resume-btn" not in html


def test_live_rows_get_a_resume_button_after_the_track_chip_in_both_row_types(tmp_path):
    html = _live_apps_html(tmp_path)
    scored = re.search(r'<details class="row[^>]*data-job-id="1"[^>]*>.*?</details>', html, re.S).group(0)
    summary = scored.split("</summary>")[0]
    assert summary.count('class="resume-btn"') == 1
    row_end = summary.split('class="row-end"')[1]
    assert row_end.index('class="app-chip"') < row_end.index('class="resume-btn"') < row_end.index('class="apply-link"')
    assert ">Resume</button>" in summary
    plain = re.search(r'<div class="plain-row[^>]*data-job-id="3"[^>]*>.*?\n    </div>', html, re.S).group(0)
    assert plain.count('class="resume-btn"') == 1
    assert 'data-source-key="x"' in plain.split(">")[0] or 'data-source-key="x"' in plain[:300]


def test_the_resume_button_slot_adds_no_whitespace_between_neighbours(tmp_path):
    html = _live_apps_html(tmp_path)
    assert re.search(r"</button><button type=\"button\" class=\"resume-btn\"", html)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render_radar.py -q`
Expected: the new live tests FAIL; static tests and the golden test pass.

- [ ] **Step 3: Implement**

In `scripts/render_radar.py`:

1. Add near `_app_chip_html`:

```python
_RESUME_BTN = (
    '<button type="button" class="resume-btn" '
    'title="Save this job\'s description and copy a prompt for the resume-generator skill">Resume</button>'
)
```

2. In `_row_html` and `_never_reviewed_row_html`, next to where `app_chip` is computed add `resume_btn = _RESUME_BTN if live else ""`, and make the **inline** edit on the existing slot line so no whitespace is added: `{feedback_buttons}{app_chip}` → `{feedback_buttons}{app_chip}{resume_btn}` (in both builders; the line must stay a single line).
3. Append to `_LIVE_STYLE`:

```
  .resume-btn { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--ink-soft); cursor: pointer; }
  .resume-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  .resume-btn:disabled { opacity: 0.6; cursor: progress; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_render_radar.py tests/test_render_applications.py -q && uv run ruff check scripts tests`
Expected: PASS — including the golden static test. If the golden test fails, an inline slot gained whitespace: fix the slot, never the golden fixture.

- [ ] **Step 5: Commit**

```bash
git add scripts/render_radar.py tests/test_render_radar.py
git commit -m "feat(radar): Resume button on live rows" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: `resume-generator` skill

**Files:**
- Create: `skills/resume-generator/SKILL.md`

**Interfaces:** the skill calls only: `job-hunter resume-files`, `job-hunter contact`, `scripts/measure_resume.py`, `scripts/log_resume.py` (all built in Tasks 1–4). It reads a JD text file (from the radar button / `job-hunter export-jd`) or pasted JD text.

- [ ] **Step 1: Create the skill file**

Write `skills/resume-generator/SKILL.md` with exactly this content:

````markdown
---
name: resume-generator
version: 1.0.0
description: Generate a tailored, ATS-friendly US Letter resume (1, 1.5 or 2 pages) as HTML and PDF from the user's newest master resume and a job description (a JD file exported from the job-hunter radar, or pasted text). Use whenever asked to create, write, tailor or customize a resume or CV for a company or role, or to prepare a job application — "generate a resume for [company]", "tailor my resume", "2 page resume for this JD", or any request that includes a job description and asks for a resume. Free-text instructions in the request (page size, emphasis, what to drop, tone) are always honored. Always invoke this skill; never write a resume without it.
compatibility: Requires uv and Python 3.11+. PDF output needs `uv sync --extra resume` and a one-time `uv run playwright install chromium` (no Microsoft Word needed; works on Windows, macOS and Linux). Runs in Claude Code, Hermes and OpenCode.
metadata:
  job_hunter:
    stage: resume
  hermes:
    tags: [jobs, resume, documents]
---

# Resume Generator

Use this skill to turn one job description plus the user's master resume into a tailored resume,
saved as `.html` and `.pdf`. Python (the `job-hunter` CLI and two scripts) resolves files and
measures pages; **you** do the keyword mapping and the writing.

Changelog: 1.0.0 — first release in job-hunter (ported from a standalone resume workflow: HTML draft +
measured page fill; no Word/`.docx` path, no hook).

## Examples

- `Use the resume-generator skill on data/output/Acme/JD_Acme_ADAS_Engineer_2026-09-26.txt` (what the
  radar's **Resume** button copies)
- `/resume-generator data/output/Acme/JD_Acme_ADAS_Engineer_2026-09-26.txt, 2 page, lead with functional safety`
- `/resume-generator` with a job description pasted below the command
- `tailor my resume for the Ford ADAS role, keep it to one page and drop the older roles`

## Contract

Input:
- a JD: a path to a `JD_*.txt` file, or pasted JD text (a JD is required — if none is given, ask)
- optional free-text instructions in the same request (see **Personalization**)
- project path (`--project`; defaults to `$JOB_HUNTER_ROOT`/cwd)

Output (all under the JD file's folder, else `data/output/<Company_Name>/`):
- `<LastName>_CV_<Company>_<RoleToken>_<PageSuffix><YYYY-MM-DD>.html` and its `.pdf`
- one row appended to `data/output/resume_log.csv`
- a short report: file paths, page count and fill, and which personalization you applied

Next command: `outreach-writer` for an outreach email and/or cover letter for the same job.

## Personalization (owner-controlled)

The user can steer every run, exactly as they choose:

1. **In the request.** Read the *whole* request before applying any default. Anything beyond the JD
   is an instruction: page size ("1.5 page", "2 page"), emphasis ("lead with functional safety",
   "feature the Ford role first"), omissions ("leave out the older roles"), tone or length changes,
   extra company context, or pasted JD text. Apply it; do not ignore or re-interpret it.
2. **Standing preferences.** Step 0 loads `data/resume/personalization.md` if it exists. Apply the
   `## all` and `## resume-generator` sections on every run.
3. **Precedence (highest first):** integrity rules (below) > the current request > `personalization.md` >
   this skill's defaults. A request instruction wins for that run only; never edit
   `personalization.md` yourself.
4. **Format rules are defaults, not laws.** Page size, bullet counts, summary length, the Technical
   Skills block, the six-year role cutoff and similar are defaults the user may change by asking or in
   `personalization.md`. If they do, measure against the changed target and report honestly if it
   does not fit.
5. **Integrity rules are never overridable:** no fabricated skills, tools, certifications, metrics,
   employers, dates or experience (everything must come from the master resume); no claims that
   contradict it; no invented contact details; nothing written outside the job-hunter output folder.
   If an instruction would break one, say so in one sentence and do the honest version instead
   (e.g. use the closest real experience).
6. In your final report, state which parts of the request and which `personalization.md` sections
   you applied.

## Procedure

Every `job-hunter` and script command takes `--project "$CLAUDE_PROJECT_DIR"` (Claude Code) — or the
equivalent workspace path for another runtime such as Hermes — so the skill works regardless of the
calling process's working directory.

### Step 0 — Resolve files and load personalization

```bash
uv run job-hunter resume-files --project "$CLAUDE_PROJECT_DIR"
uv run job-hunter contact --project "$CLAUDE_PROJECT_DIR"
```

- `resume-files` prints JSON: `master_resume` (the newest dated `data/resume/main_resume_<date>.md`),
  `personalization`, `cover_letter_sample`, `review_evidence` (each a path or `null`). If it exits
  non-zero, stop and relay its message (typically: add `data/resume/main_resume_<YYYY-MM-DD>.md`).
- `contact` prints the applicant's name/email/phone/LinkedIn/GitHub plus `first_name`/`last_name`.
  If it exits non-zero, stop and relay exactly which fields to fill in the `contact:` block of
  `config/candidate_profile.yaml`. **Never invent or guess contact details.** Omit phone/LinkedIn/GitHub
  from the header when they are absent.
- If `personalization` is not `null`, read that file now.
- If the context is IEEE, SAE, a journal, editorial board, program committee or technical
  committee, also read `review_evidence` (ask the user before proceeding if it is `null`).

### Step 1 — Gather inputs

- **JD:** read the JD file the user named, or use the pasted text. The company name, role title and
  job ID/role number come from it (a `JD_*.txt` exported by job-hunter has the title on line 1 and
  `Job ID:` / `Job URL:` / `Source:` lines under `Summary`).
- **Output directory:** the JD file's own folder if it lives under `data/output/<Company_Name>/`;
  otherwise `data/output/<Company_Name>/` (company name with spaces → underscores, special
  characters stripped).
- **Page size:** no mention → **1 page**. "1.5 page" / "one and a half" → 1.5. "2 page" / "two page" → 2.
  (Or whatever the user or `personalization.md` specifies.)

### Step 2 — Read and parse the master resume

Read the file named by `master_resume`. Extract every role (title, company, location, dates,
bullets), education (degrees, institutions, dates, GPA), skills/tools/certifications, and projects
or other sections. Contact details come from `contact`, not from the resume file. Compute the
**past-6-years cutoff** (today minus 6 years) and classify each role as recent or older.

### Step 3 — Extract the top 10 keywords

List the 10 most important keywords and required skills from the JD: domain technologies and tools,
methodologies/standards/certifications, domain terminology, and seniority signals ("lead",
"manage", "architect", "hands-on"). State them explicitly before continuing.

### Step 4 — Map keywords to the person's real experience

For each keyword find matching experience in the master resume. Be honest about gaps — never fill a
gap with fabricated content. Use accurate language for partial matches.

### Step 5 — Core rules for bullets

**Formula:** action verb → what was done → quantifiable result or scope. Start every bullet with a
strong verb (*Developed, Led, Designed, Implemented, Resolved, Directed, Validated, Reduced,
Increased, Deployed, Coordinated, Spearheaded, Established, Managed, Delivered*). Use metrics the
master resume provides; otherwise use scope or scale ("across 5 product lines").

**Avoid:** passive language ("responsible for", "assisted with", "helped", "was involved in",
"worked on"); paragraphs (bullets only, one concise line each); task lists (accomplishments, not
duties); filler ("results-driven", "dynamic", "passionate", "proven track record", "team player",
"strong communicator"); em dashes (—) in bullet or summary text.

**Tailoring:** include JD keywords naturally where real experience supports them, mirror the JD's
exact terminology, and put the most JD-relevant accomplishment first within each role.

### Step 6 — Select content by page size (defaults)

> **Measured capacity:** each 25-word bullet renders to ~50px in Chromium. A 3-role resume at 4+4+3
> = 11 bullets fills ~97% of one page; 5+5+4 = 14 overflows. **Start the 1-page draft at 4 bullets
> per recent role and 3 for the oldest included role**, and expand only after measuring
> `"underflow"`. Never start with 5+ and trim.

| Role recency | 1 page | 1.5 pages | 2 pages |
|---|---|---|---|
| Recent (past 6 years) | 4 bullets to start; 5–6 only after underflow | 4–6 | 4–6 |
| 6–10 years old | only if directly relevant; bullets optional | if relevant, 2–3 | 2–4 |
| 10+ years old | omit unless uniquely relevant | only if relevant, no bullets required | if relevant, 1–2 |

- **1 page:** Summary 4 bullets (fixed); bullets average 20–28 words (short bullets are the main
  cause of white space — elaborate with scope, method or outcome rather than adding bullets);
  ~560–660 words total for a 3-recent-role resume; Education = degrees only; a **Technical Skills**
  block of at most 2 lines (`**Category:** Tool1, Tool2`) containing only tools genuinely in the
  master resume and relevant to the JD; drop older roles freely; no Projects section.
- **1.5 pages:** Summary 4–5 bullets; ~750–950 words; degrees plus directly relevant certifications;
  older roles with bullets if relevant; optional Projects (2–3).
- **2 pages:** Summary 4–5 bullets; ~1000–1300 words; degrees plus relevant certifications/courses;
  all relevant roles with full bullets; Projects (3–5); relevant extracurricular/competition work.

### Step 7 — Draft the resume as HTML

Write the full resume as a self-contained HTML file. **Do not write the final file yet** — the
draft is measured first (Step 8). Use this template; fill the bracketed placeholders and do not
change the CSS unless the user asked for a different look (spacing is calibrated for US Letter fill).
Omit the contact items you do not have.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: Arial, "Liberation Sans", Helvetica, sans-serif;
    font-size: 10.5pt;
    line-height: 1.2;
    color: #000;
    width: 7.5in;
  }
  .name { font-size: 17pt; font-weight: bold; text-align: center; margin-bottom: 2pt; }
  .contact { text-align: center; font-size: 9.5pt; margin-bottom: 5pt; }
  .section {
    font-size: 10.5pt; font-weight: bold; text-transform: uppercase;
    border-bottom: 1pt solid #000; margin-top: 5pt; margin-bottom: 2pt; letter-spacing: 0.4pt;
  }
  .role-line {
    display: flex; justify-content: space-between; font-weight: bold;
    font-size: 10.5pt; margin-top: 2pt; margin-bottom: 1pt;
  }
  .company-line { font-style: italic; font-size: 10pt; margin-bottom: 1pt; }
  ul { margin-left: 13pt; margin-bottom: 0; }
  li { margin-bottom: 1.5pt; font-size: 10.5pt; text-align: justify; }
  .edu-line { display: flex; justify-content: space-between; margin-bottom: 1.5pt; }
  .skills-line { font-size: 10pt; margin-bottom: 1.5pt; }
</style>
</head>
<body>

<div class="name">[FULL NAME IN CAPS]</div>
<div class="contact">[email] | [phone] | [LinkedIn URL] | [GitHub URL]</div>

<div class="section">Summary</div>
<ul>
  <li>[Summary bullet 1 — most relevant credential]</li>
  <li>[Summary bullet 2 — key technical skill matching a top JD keyword]</li>
  <li>[Summary bullet 3 — accomplishment or domain expertise]</li>
  <li>[Summary bullet 4 — differentiator relevant to this role]</li>
</ul>

<div class="section">Professional Experience</div>

<div class="role-line"><span>[Job Title]</span><span>[Start Month Year] – [End Month Year or Present]</span></div>
<div class="company-line">[Company Name], [State], [Country]</div>
<ul>
  <li>[Most JD-relevant bullet — action verb + outcome]</li>
  <li>[Bullet 2]</li>
  <li>[Bullet 3]</li>
  <li>[Bullet 4]</li>
</ul>

<div class="role-line"><span>[Job Title]</span><span>[Start Month Year] – [End Month Year]</span></div>
<div class="company-line">[Company Name], [State], [Country]</div>
<ul>
  <li>[Bullet 1]</li>
  <li>[Bullet 2]</li>
  <li>[Bullet 3]</li>
</ul>

<div class="section">Education</div>
<div class="edu-line">
  <span><strong>[Degree]</strong>, <em>[University, State, Country]</em></span>
  <span>[Month Year] (GPA – X.XX/X.XX)</span>
</div>

<div class="section">Technical Skills</div>
<div class="skills-line"><strong>[Category]:</strong> [Tool1], [Tool2], [Tool3], [Tool4]</div>
<div class="skills-line"><strong>[Category]:</strong> [Tool1], [Tool2], [Tool3], [Tool4]</div>

</body>
</html>
```

Content rules inside the HTML: each `<li>` is 20–28 words, strong verb first, accomplishment +
outcome; no passive language; recent roles start at 4 bullets; Technical Skills is exactly 2
`skills-line` divs with JD-relevant tools only; escape `&`, `<` and `>` in text as HTML entities.

### Step 8 — Measure page fill (feedback loop)

Save the draft to `<output dir>/_draft.html` with the Write tool, then run (start an iteration
counter at **1**; increment it each time you re-run after an adjustment):

```bash
uv run python scripts/measure_resume.py "<output dir>/_draft.html" --target-pages <1|1.5|2> --project "$CLAUDE_PROJECT_DIR"
```

**Always pass `--target-pages`** matching the chosen page size — without it a 1.5/2-page draft is
misreported as an overflow. The script exits 2 with the exact install commands if the optional
`resume` extra or Chromium is missing; relay that message and stop. Read `status`,
`last_page_fill_pct` and `guidance` (ignore the legacy `fill_pct` unless the target is 1 page):

| `status` | Meaning | Action |
|---|---|---|
| `ok` | page count matches the target and the last page is in its band (88–100% for a whole-page target; ~35–70% for a `.5` target) | go to Step 9 |
| `overflow` | too many pages, or the last page is over its band | trim per `guidance` |
| `underflow` | too few pages, or the last page is under its band | add content per `guidance` |

**Adjust at most once.** Overflow: remove the least JD-relevant bullets from the oldest included
role first and shorten bullets over 28 words; do not remove bullets from the most recent role.
Underflow: expand existing bullets with scope, method or outcome; add a bullet to the oldest included
role if all are at minimum; for a 1.5/2-page target prefer folding in real, still-unused bullets
from the master resume over stretching wording. Overwrite `_draft.html`, re-run the same command,
and increment the counter. If the status is still not `ok` after one adjustment, continue to Step 9
and tell the user exactly what remains (quote `last_page_fill_pct`; do not average pages).

Quality check while measuring: no fabricated skills/tools/certifications/metrics; no passive
language; every `<li>` starts with a strong verb; JD terminology mirrored verbatim where the
experience is real; Education = degrees only for 1 page; Technical Skills ≤ 2 lines, all present in
the master resume.

### Step 9 — Save the final files

Write the final HTML with the Write tool to:

```
<output dir>/<LastName>_CV_<Company>_<RoleToken>_<PageSuffix><YYYY-MM-DD>.html
```

- `<LastName>` is `last_name` from `contact`.
- `<Company>` is the company name from the JD (shortened if long, spaces/special characters removed).
- `<RoleToken>` is **always present**: a compact tag from the JD title so two roles at one company on
  one day never collide. Take the significant words in the first 2–3 words of the title (drop
  "a/the/of/and/for"; stop at the first comma, pipe or dash that introduces a sub-title), then:
  (1) prefer a recognized short role acronym (`TPM`, `STE`, `SWE`, `PM`, `QE`); (2) otherwise keep
  any all-caps domain acronym (`ADAS`) and truncate every other significant word to ~3 letters,
  capitalizing the first (`Tes`, `Eng`), concatenated (`ADASTesEng`). Keep it ~3–10 characters; if
  a same-name file already exists append `2`, `3`, …
- `<PageSuffix>`: none for 1 page; `1p5_` for 1.5 pages; `2p_` for 2 pages (inserted right before
  the date). Examples: `Doe_CV_Honda_ADASTesEng_2026-05-10.html`, `Doe_CV_Honda_ADASTesEng_1p5_2026-05-10.html`,
  `Doe_CV_OpenAI_TPM_2p_2026-09-08.html`.

Then generate the PDF explicitly (there is no hook):

```bash
uv run python scripts/measure_resume.py "<final .html>" --target-pages <1|1.5|2> --save-pdf "<final .pdf>" --project "$CLAUDE_PROJECT_DIR"
```

### Step 10 — Log the run

```bash
uv run python scripts/log_resume.py --project "$CLAUDE_PROJECT_DIR" \
  --file "<final .html file name>" --company "<Company>" --role "<JD job title>" --url "<Job URL from the JD, or 'Not specified'>" \
  --fill <last_page_fill_pct from the final measurement> --pages <pages> --iterations <N> --date <YYYY-MM-DD>
```

Report back: the `.html` and `.pdf` paths, page count and last-page fill (e.g. "1 page, 94% full"),
any remaining fit issue, and the personalization you applied. Suggest `outreach-writer` as the next step.

## Hard rules

Integrity rules (never overridable):
1. **No fabrication** — never add skills, tools, certifications, metrics or experience that are not
   in the master resume.
2. **No invented contact details** — name/email/phone/links come only from `job-hunter contact`.
3. **Always tailored** — every resume is tied to the specific role or context provided; never
   produce a one-size-fits-all resume.
4. **Role-bullet contract** — a role either has bullets or is not listed; a heading with zero
   bullets is never acceptable.
5. **Accomplishments over duties** — every bullet describes an outcome or achievement.
6. **Stay inside `data/output/`** — write only under the job-hunter output folder.

Defaults (the user may change any of these by asking, or in `personalization.md`): 1 page unless
another size is requested; recency priority (past 6 years first and with more bullets); the bullet
counts, word ranges and Technical Skills block above; no em dashes.
````

- [ ] **Step 2: Sanity-check the skill file**

Run:

```bash
head -12 skills/resume-generator/SKILL.md
grep -c "resume-files\|job-hunter contact\|measure_resume.py\|log_resume.py" skills/resume-generator/SKILL.md
! grep -n -i "model:\|CLAUDE.local\|ToolSearch\|mcp__\|PostToolUse\|@output\|D:\\\\\|<owner-first-name>\|<owner-surname-fragment>\|Job-Op-Resume" skills/resume-generator/SKILL.md
```

Expected: frontmatter shows `name`, `version: 1.0.0`, `description`, `compatibility`, `metadata`; the grep count is ≥ 6; the last command prints nothing and exits 0 (none of the forbidden runtime-specific or personal strings appear).

- [ ] **Step 3: Commit**

```bash
git add skills/resume-generator/SKILL.md
git commit -m "feat(skills): resume-generator skill (HTML draft, measured page fill, owner-controlled personalization)" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: `outreach-writer` skill

**Files:**
- Create: `skills/outreach-writer/SKILL.md`

**Interfaces:** calls `job-hunter resume-files`, `job-hunter contact`, `scripts/measure_resume.py` (cover letter PDF). Reads the tailored CV and the JD file from the company folder.

- [ ] **Step 1: Create the skill file**

Write `skills/outreach-writer/SKILL.md` with exactly this content:

````markdown
---
name: outreach-writer
version: 1.0.0
description: Write a short Dale Carnegie–style outreach email to a hiring manager or recruiter, and/or a tailored cover letter, from the applicant's tailored resume and a job description. Use whenever asked to "write an email to the hiring manager/recruiter", "draft an outreach email", "write a cover letter", "generate a cover letter for [company]", or any request to reach out about a job application. Free-text instructions in the request (recipient name, angle, tone, length, structure) are always honored. Always invoke this skill; never hand-write outreach copy without it.
compatibility: Requires uv and Python 3.11+. The cover-letter PDF needs `uv sync --extra resume` and a one-time `uv run playwright install chromium` (no Microsoft Word needed). Runs in Claude Code, Hermes and OpenCode.
metadata:
  job_hunter:
    stage: outreach
  hermes:
    tags: [jobs, outreach, documents]
---

# Outreach Writer

Use this skill to produce, from one tailored resume and one job description, either or both of:

1. an **outreach email** — short, Dale Carnegie–style, to a hiring manager or recruiter
   (default 200–250 words, 3 points), saved as plain text so it pastes cleanly into an email client;
2. a **cover letter** — a formal letter tied to the resume and JD (default: exactly 5 bold-labeled
   bullets, ~220 words), rendered to PDF (HTML kept as a build artifact) plus a plain-text copy.

Changelog: 1.0.0 — first release in job-hunter (ported from a standalone resume workflow;
cover-letter filenames use the applicant's name from the profile; format rules are overridable
defaults; no hook).

## Examples

- `/outreach-writer` — then say "email", "cover letter" or "both"
- `/outreach-writer for data/output/Acme/JD_Acme_ADAS_Engineer_2026-09-26.txt, both, address it to Dana Lee`
- `write a cover letter for the Ford role, keep it to four bullets and mention my relocation`
- `draft an outreach email to the recruiter, warmer tone, under 180 words`

## Contract

Input:
- the company/role: a JD file path, or the company folder under `data/output/`, or pasted JD text (a JD is required — ask if none)
- which deliverable(s): email, cover letter, or both (ask if ambiguous)
- optional free-text instructions (see **Personalization**), e.g. recipient name, angle, tone, length
- project path (`--project`; defaults to `$JOB_HUNTER_ROOT`/cwd)

Output (in `data/output/<Company_Name>/`):
- `<LastName>_Email_<Company>_<YYYY-MM-DD>.txt`
- `<FirstName>_CL-<Company>-<RoleToken>_<YYYY-MM-DD>.pdf` (+ `.html` build artifact + `.txt` plain copy)
- a short report of what was written, word counts, and which personalization you applied

## Personalization (owner-controlled)

1. **In the request.** Read the whole request first. Recipient name and title, an angle to lead with,
   tone ("warmer", "more formal"), length changes, extra facts about the user's situation (relocation,
   availability, referral), which points to feature, "email only" / "both" — apply all of it.
2. **Standing preferences.** Step 0 loads `data/resume/personalization.md` if present; apply its
   `## all` and `## outreach-writer` sections (tone, phrases to avoid or prefer, sign-off style,
   standing facts the user is happy to state in outreach).
3. **Precedence (highest first):** integrity rules > the current request > `personalization.md` > this
   skill's defaults. A request instruction wins for that run only; never edit `personalization.md`.
4. **Format rules are defaults, not laws.** The email's word range and 3-point structure, the cover
   letter's word range and 5-bullet structure, the sign-off, and similar can be changed by asking or in
   `personalization.md`. Apply the change and keep the rest of the writing principles.
5. **Integrity rules are never overridable:** every claim comes from the tailored resume or master
   resume (no invented metrics, employers, dates, availability or relationships); never invent a
   recipient name; never invent contact details; do not stretch a skill to the applicant's total years
   of experience; write only under `data/output/`. If an instruction would break one, say so in one
   sentence and do the honest version.
6. State in your final report which parts of the request and which `personalization.md` sections you applied.

## Procedure

Every `job-hunter` and script command takes `--project "$CLAUDE_PROJECT_DIR"` (Claude Code) — or the
equivalent workspace path for another runtime such as Hermes.

### Step 0 — Resolve files, contact and personalization

```bash
uv run job-hunter resume-files --project "$CLAUDE_PROJECT_DIR"
uv run job-hunter contact --project "$CLAUDE_PROJECT_DIR"
```

`contact` gives name, email, phone, LinkedIn, GitHub, `first_name`, `last_name`; if it exits non-zero,
stop and relay which `contact:` fields to fix in `config/candidate_profile.yaml` (never invent
details). `resume-files` gives `master_resume`, `personalization`, `cover_letter_sample` (each a path
or `null`); if `personalization` is set, read it.

### Step 1 — Determine what to generate

"email", "outreach email", "message to the hiring manager/recruiter" → email only. "cover letter" →
cover letter only. "both", or genuinely ambiguous phrasing → ask which one(s) before continuing.

### Step 2 — Gather inputs

- **Company folder:** `data/output/<Company_Name>/` (the JD file's folder).
- **Tailored resume (preferred source):** the newest `*_CV_*.html` (or `.md`) in that folder — already
  JD-tailored, so it is the primary source of points. If none exists, fall back to `master_resume`
  and tell the user a tailored resume does not exist yet (offer to run `resume-generator` first, but
  continue with the master resume if they prefer).
- **JD:** the newest `JD_*.txt` in that folder, or pasted text. If neither exists, ask for it.
- **Recipient:** the hiring manager/recruiter name from the JD or the conversation. If unknown, ask
  the user once. If they do not know either, use "Dear Hiring Team," (email) or "Dear Hiring
  Manager," (cover letter) — **never invent a name**.
- **Company, role title, Job ID/Role Number:** from the JD (`Job ID:` line under `Summary`).

### Step 3 — Extract shared value points

Compare the tailored resume with the JD and identify 4–6 candidate points where the applicant's real,
documented experience answers a JD requirement or priority. Rank by relevance. The email uses the top
3, the cover letter the top 5 (or however many the request/personalization asks for). Never use a
point that is not backed by the resume.

### Step 4 — Dale Carnegie principles (both documents)

1. **Talk in terms of the other person's interests** — frame every point as a benefit to their team.
2. **Do not open with "I"** in the email — lead with their company, team or need.
3. **Make the other person feel important** — a specific, genuine reference drawn from the JD, not flattery.
4. **Arouse an eager want** — close forward-looking and low-pressure, never demanding.
5. **Be short, sincere and specific** — no "results-driven", "dynamic", "passionate", "proven track
   record", "team player", "hardworking", "synergy" or similar stock phrases.
6. **Every claim is true and drawn from the resume/JD.**

### Step 5A — Draft the email (if requested)

Default: 200–250 words, exactly 3 points, prose paragraphs.

```
Subject: [Role Title] (Role/Job ID: [ID, if known]) – [Applicant Full Name]

Dear [Hiring Manager Name / Hiring Team],

[Opening, 1–2 sentences: name the exact role title and the Job ID (if the JD gives one), tie it to something
specific about their team's focus, and state plainly that the applicant is confident they can bring value to
that role — not "I am applying for..." and not a bare title drop]

[Point 1: 1–2 sentences — a documented skill/achievement mapped to a top JD need, phrased as value to them,
with concrete detail (tools, scope, outcome)]

[Point 2: same pattern, different JD need]

[Point 3: same pattern, different JD need]

[Closing: 1–2 sentences, forward-looking, low-pressure call to action]

Sincerely,
[Applicant Full Name]
[Phone] | [Email] | [LinkedIn]
```

Rules: the opening must name the exact role title, include the Job ID when known, and state confidence
in bringing value. **Avoid invented-sounding specificity:** never construct a number or label that is
not in the resume (do not compress a list of team names into an invented headcount). **Do not stretch a
skill's timeframe** to the applicant's total years of experience — attribute a skill to the specific
company/role it came from ("At [Company], I...") or state it without a timeframe.

Count the body words (excluding the signature) with a quick word count and adjust **once** if outside
the target range (add concrete detail if under; trim the least JD-relevant clause if over). Do not pad.

Save to `data/output/<Company_Name>/<LastName>_Email_<Company>_<YYYY-MM-DD>.txt` — plain text, exactly the
structure above, no markdown or HTML.

### Step 5B — Draft the cover letter (if requested)

Format reference: if `cover_letter_sample` is not `null`, read it and match its structure and voice
(header block, right-aligned date, three-line recipient block, opening paragraph naming the role and job
ID, a transition sentence, bold-labeled bullets, a two-paragraph closing, and a bare sign-off). Otherwise use
the structure below.

Default: exactly **5** bullets, each with a bolded category label; ~220 words (200–260) across the opening,
bullets and closing.

```
[Applicant Full Name]
[Phone] | [Email] | [LinkedIn] | [GitHub]

[Today's date, right-aligned, e.g. Month Day, Year]

[Hiring Manager Name, or "Hiring Manager" if unknown]
[Company Name]
[Location — city/state or country from the JD, if available]

Dear [Hiring Manager Name / "Hiring Manager"],

[Opening paragraph, 3–4 sentences: "I am writing to express my interest in the [Role Title] position ([Job ID, if
known]) at [Company]. With over [N years from the resume] years of hands-on experience in [domain], I am confident
in my ability to contribute effectively to your team. My background in [2–3 areas from the resume] aligns well with
the qualifications and responsibilities outlined for this role."]

My professional experience and technical expertise make me a strong candidate for this position:

• **[Category Label 1]:** [1–2 sentences — resume-backed skill/achievement mapped to a JD requirement]
• **[Category Label 2]:** ...
• **[Category Label 3]:** ...
• **[Category Label 4]:** ...
• **[Category Label 5]:** ...

[Closing paragraph 1, 1–2 sentences: enthusiasm about contributing to something specific from the JD/company mission]

[Closing paragraph 2, 1–2 sentences: thanks, and looking forward to discussing how the applicant's background aligns
with [Company]'s goals]

Sincerely,
[Applicant Full Name]
```

The category labels are short JD-derived phrases naming the requirement each bullet answers (e.g. "Extensive ADAS
Experience"), not generic headers. Draft the plain-text body first and count words across the opening,
transition sentence, bullets and closing paragraphs (excluding header, date, recipient block and signature);
adjust once if outside the range.

**Render as HTML** with this template (change the CSS only if the user asked for a different look), then save:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: Arial, "Liberation Sans", Helvetica, sans-serif;
    font-size: 11pt; line-height: 1.35; color: #000; width: 7.5in;
  }
  .sender-name { font-size: 16pt; font-weight: bold; text-align: center; margin-bottom: 2pt; }
  .sender-contact { font-size: 10pt; text-align: center; margin-bottom: 18pt; }
  .date { text-align: right; margin-bottom: 14pt; }
  .recipient { margin-bottom: 14pt; }
  p { margin-bottom: 10pt; text-align: justify; }
  ul { margin-left: 18pt; margin-bottom: 10pt; }
  li { margin-bottom: 6pt; }
  .signoff { margin-top: 10pt; }
</style>
</head>
<body>

<div class="sender-name">[APPLICANT FULL NAME IN CAPS]</div>
<div class="sender-contact">[Email] | [Phone] | [LinkedIn URL] | [GitHub URL]</div>

<div class="date">[Month Day, Year]</div>

<div class="recipient">
  [Hiring Manager Name / Hiring Manager]<br>
  [Company Name]<br>
  [Location]
</div>

<p>Dear [Hiring Manager Name / Hiring Manager],</p>

<p>[Opening paragraph]</p>

<p>My professional experience and technical expertise make me a strong candidate for this position:</p>

<ul>
  <li><strong>[Category Label 1]:</strong> [Bullet 1 text]</li>
  <li><strong>[Category Label 2]:</strong> [Bullet 2 text]</li>
  <li><strong>[Category Label 3]:</strong> [Bullet 3 text]</li>
  <li><strong>[Category Label 4]:</strong> [Bullet 4 text]</li>
  <li><strong>[Category Label 5]:</strong> [Bullet 5 text]</li>
</ul>

<p>[Closing paragraph 1]</p>

<p>[Closing paragraph 2]</p>

<p class="signoff">Sincerely,<br>[Applicant Full Name]</p>

</body>
</html>
```

Escape `&`, `<` and `>` in text as HTML entities. Omit contact items you do not have.

**Filenames** (in `data/output/<Company_Name>/`):

```
<FirstName>_CL-<Company>-<RoleToken>_<YYYY-MM-DD>.html
<FirstName>_CL-<Company>-<RoleToken>_<YYYY-MM-DD>.pdf
<FirstName>_CL-<Company>-<RoleToken>_<YYYY-MM-DD>.txt
```

`<FirstName>` is `first_name` from `contact`. `<Company>` uses the same sanitization as the JD filename
(spaces → underscores, special characters stripped). `<RoleToken>` is the **exact token already used in that
role's tailored CV filename** (reuse it so the CV and letter stay visibly paired; if no tailored CV exists,
derive one by the resume-generator rule: a recognized role acronym such as `TPM`/`STE`/`SWE`/`PM`/`QE`, else
all-caps domain acronyms kept and other significant words truncated to ~3 letters, e.g. `ADASTesEng`).
`<YYYY-MM-DD>` is today's date.

Generate the PDF explicitly (there is no hook):

```bash
uv run python scripts/measure_resume.py "<.html path>" --save-pdf "<.pdf path>" --project "$CLAUDE_PROJECT_DIR"
```

A ~220-word letter will almost always report `"underflow"` (well under a full page) — that is expected;
**do not pad** to raise the fill. Act only on `"overflow"` (more than one page): trim the least
JD-relevant bullet or shorten the closing paragraphs. If the script exits 2, relay its install
instructions and stop after saving the `.html`/`.txt`.

**Plain-text copy:** also save the same content as plain text to the `.txt` path: same header block, date,
recipient block, salutation, opening, transition sentence, bullets, closing paragraphs and sign-off, with each
bullet rendered as `- <Category Label>: <bullet text>` (no bold markup). This is a plain Write, not a
conversion step.

### Step 6 — Report back

Say what was generated and where, with word counts, e.g.:

```
Email saved: data/output/Acme/Doe_Email_Acme_2026-09-26.txt (231 words)
Cover letter saved: data/output/Acme/Jane_CL-Acme-ADASTesEng_2026-09-26.pdf (224 words)
Cover letter (plain text): data/output/Acme/Jane_CL-Acme-ADASTesEng_2026-09-26.txt
Personalization applied: request — addressed to Dana Lee; personalization.md — "## outreach-writer": warmer tone
```

## Hard rules

Integrity rules (never overridable):
1. **No fabrication** — every point, skill or achievement exists in the tailored or master resume; never
   invent metrics, availability dates, locations or relationships the user did not state.
2. **No invented recipient names or contact details** — if the name is unknown and the user cannot
   supply it, use the generic salutation.
3. **Always tied to a real JD** — never generate generic, un-tailored outreach copy.
4. **Stay inside `data/output/`.**

Defaults (the user may change any of these by asking, or in `personalization.md`): email 200–250
words with exactly 3 points; cover letter ~220 words (200–260) with exactly 5 bold-labeled bullets;
Carnegie tone in the email (benefit-to-them framing, no opening with "I", no filler adjectives,
low-pressure close) with the cover letter's more traditional opening ("I am writing to express my
interest...") while keeping every other Carnegie principle; email always plain `.txt`; cover letter
always rendered to `.pdf` via the HTML intermediate and also saved as a plain `.txt` copy.
````

- [ ] **Step 2: Sanity-check the skill file**

Run:

```bash
head -12 skills/outreach-writer/SKILL.md
grep -c "resume-files\|job-hunter contact\|measure_resume.py" skills/outreach-writer/SKILL.md
! grep -n -i "model:\|CLAUDE.local\|ToolSearch\|mcp__\|PostToolUse\|@output\|D:\\\\\|<owner-first-name>\|<owner-surname-fragment>\|Job-Op-Resume" skills/outreach-writer/SKILL.md
```

Expected: valid frontmatter; grep count ≥ 4; the last command prints nothing and exits 0.

- [ ] **Step 3: Commit**

```bash
git add skills/outreach-writer/SKILL.md
git commit -m "feat(skills): outreach-writer skill (Carnegie email, cover letter, owner-controlled personalization)" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: Installer, symlinks, docs, skill version, verification

**Files:**
- Modify: `scripts/install_skill.sh`, `tests/test_install_skill.py`, `README.md`, `CLAUDE.md`, `docs/SPEC.md`, `skills/job-radar/SKILL.md`
- Create: `.claude/skills/resume-generator`, `.claude/skills/outreach-writer` (symlinks)

**Interfaces:** none new; documentation, installation and verification.

- [ ] **Step 1: Write the failing installer test change**

In `tests/test_install_skill.py`: rename the `SIX_SKILLS` set to `ALL_SKILLS` and add `"resume-generator"`, `"outreach-writer"` (eight names); update every use (`for name in ALL_SKILLS`), the function name `test_dry_run_installs_all_six_skills_including_onboard_source` → `..._all_eight_skills_...`, and the comment "All six skills were the manifest's only entries" → "All eight". Add:

```python
def test_dry_run_lists_the_resume_and_outreach_skills(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(["--dry-run", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    for name in ("resume-generator", "outreach-writer"):
        assert f"-> {home}/.claude/skills/{name} [dry-run]" in proc.stdout
```

Run `uv run pytest tests/test_install_skill.py -q` — expected: FAIL (the two new names are not installed yet).

- [ ] **Step 2: Update the installer and link the skills**

In `scripts/install_skill.sh`: set `SKILL_NAMES="job-hunter job-scout job-reviewer job-radar job-feedback onboard-source resume-generator outreach-writer"`; in the `usage` heredoc change "all six job-hunter skills" to "all eight job-hunter skills" and add one sentence: "`resume-generator` and `outreach-writer` write tailored resumes and outreach copy (PDF output needs `uv sync --extra resume`)."

Create the repo-local symlinks exactly like the existing ones:

```bash
ln -s ../../skills/resume-generator .claude/skills/resume-generator
ln -s ../../skills/outreach-writer .claude/skills/outreach-writer
git add .claude/skills/resume-generator .claude/skills/outreach-writer
```

Run `uv run pytest tests/test_install_skill.py -q` — expected: PASS.

- [ ] **Step 3: Update `skills/job-radar/SKILL.md`**

Bump `version: 1.4.0` → `1.5.0` and, in the live-option note that already mentions application tracking, add one sentence: "Each live row also has a **Resume** button: it saves that job's description under `data/output/<Company>/` and copies a prompt for the `resume-generator` skill (then `outreach-writer`)."

- [ ] **Step 4: Update the docs (accurate to what shipped; read the code where needed)**

- `docs/SPEC.md`: add a new section "Resume and outreach (skills + JD export)" covering — the data layout (`data/resume/`, `data/output/<Company>/`, `resume_log.csv`), the resolver rules and `resume-files` JSON, the `contact:` block and `contact` command, `export-jd` and the JD file format, `POST /api/jd` (request, replies 200/404/409, project-relative path only), `measure_resume.py`/`log_resume.py` (JSON fields, `--project`, the optional `resume` extra and its install commands, fonts caveat), the personalization model (request text + `data/resume/personalization.md`; precedence; integrity rules vs overridable defaults), and both skills' contracts. Update the CLI-reference table (`resume-files`, `contact`, `export-jd`), the scripts table (`measure_resume.py`, `log_resume.py`), the `serve_radar.py` routes row (`POST /api/jd`), and change the installer row's "all six skill directories" list to the eight names.
- `README.md`: a "Resume and outreach" section — setup (`uv sync --extra resume`, `uv run playwright install chromium`, fill `contact:` in `config/candidate_profile.yaml`, add `data/resume/main_resume_<YYYY-MM-DD>.md`, optional `personalization.md`), the workflow (radar **Resume** button → paste the copied prompt into Claude Code or Hermes → `outreach-writer`), where outputs land, that free-text instructions work on every run, and portability (no Word; Windows/macOS/Linux; fonts note). Change "installs all six skills" to eight.
- `CLAUDE.md`: add architecture bullets for `resume_source.py`, `jd_export.py`, the `contact` block, `measure_resume.py`/`log_resume.py` (optional extra), `POST /api/jd`, and the two skills; add to "Safe testing": generated resumes/JDs/`data/resume/*` and the `contact:` block are personal data — never commit them or paste them into tests/docs (use obviously fake values); change "six independently-invocable skills" to eight (leave the unrelated "six filtering fields" wording alone).

- [ ] **Step 5: Full verification**

Run:

```bash
uv run ruff check .
uv run pytest -q
node --test tests/js/*.test.js
node --check scripts/templates/radar_live_ui.js && node --check scripts/templates/radar_live_core.js
grep -rn -i "data/temp\|Job-Op-Resume" --include='*.py' --include='*.md' --include='*.sh' --include='*.js' --include='*.yaml' . | grep -v "^./data/" | grep -v "docs/superpowers/"
```

Expected: ruff clean; pytest = only the 2 baseline `test_collector.py` failures (report them separately); node tests pass; the final grep prints **nothing** (no reference to the scratch directory outside `data/` and the spec/plan docs, which describe the source material).

- [ ] **Step 6: Manual smoke on a temp project (never the real `data/`)**

```bash
export SMOKE=$(mktemp -d)
uv run python <path-to>/seed_smoke_project.py "$SMOKE"        # the seed script from the live-radar work (11 jobs)
mkdir -p "$SMOKE/data/resume"
printf '# **JANE DOE**\n\n* Built things.\n' > "$SMOKE/data/resume/main_resume_2026-09-01.md"
uv run job-hunter resume-files --project "$SMOKE"               # master_resume points at the dated file
uv run job-hunter contact --project "$SMOKE"                    # exits 2 listing the placeholder fields
uv run job-hunter export-jd --project "$SMOKE" ford 1           # (use a seeded source_key/job_id) prints JSON, writes the JD file
uv run python scripts/serve_radar.py --project "$SMOKE" --open  # click Resume on a row
```

Check, ticking each off: [ ] `resume-files` chooses the newest dated file (add an older-dated file and a newer-*mtime* one — filename date still wins); [ ] `contact` refuses the example placeholders and lists them; [ ] clicking **Resume** on a scored row and on an unreviewed row shows "Copied to clipboard — paste it into Claude Code or Hermes. JD saved: data/output/…" and the clipboard holds `Use the resume-generator skill on data/output/<Company>/JD_….txt`; [ ] the JD file exists with the documented layout; [ ] clicking again reuses it (no duplicate), and after changing that job's description in SQLite a numbered `_2` file appears; [ ] deleting the job row in SQLite then clicking shows "This job is no longer in the database…"; [ ] the static report (`uv run python scripts/render_radar.py --project "$SMOKE" --output "$SMOKE/static.html"`) has no Resume button; [ ] with `uv sync --extra resume` and Chromium installed, `uv run python scripts/measure_resume.py <a small html> --target-pages 1 --save-pdf out.pdf` prints JSON and writes the PDF; without them it prints the install commands and exits 2. Then run one real end-to-end pass yourself in Claude Code (and, if you use it, Hermes): paste the copied prompt and confirm the resume and PDF appear under `data/output/<Company>/`, then ask for an outreach email; try a free-text instruction (e.g. "2 page, lead with X") and a `personalization.md` line to confirm both are applied and reported. Record the outcome in the PR description; remove the temp project.

- [ ] **Step 7: Commit**

```bash
git add scripts/install_skill.sh tests/test_install_skill.py README.md CLAUDE.md docs/SPEC.md skills/job-radar/SKILL.md
git commit -m "feat(resume): install both skills everywhere, docs, job-radar mentions the Resume button" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review (spec coverage, done inline)

- **Spec §2 in scope 1–7:** skills → Tasks 8–9; `export-jd`/`POST /api/jd` → Tasks 3, 5; button → Tasks 6–7; resolver + reviewer → Task 1; `contact:` → Task 2; scripts + extra → Task 4; installer/docs/version → Task 10.
- **§3 decisions:** D1 (no job-scraper: nothing ported), D2/D3 (JD file on click; nothing AI on the server: Tasks 3, 5, 6), D4 (`data/output/`), D5 (newest dated resume, profile fallback, reviewer shared: Task 1), D6 (HTML→PDF only: Task 4), D7 (no runtime-specific text; both installers: Tasks 8–10), D8 (contact block: Task 2), D9 (no Outreach button), D10 (personalization: skills' **Personalization** sections + Task 1 `personalization` path).
- **§6.1** example-resume guard, `review_evidence`, `resume-files` JSON → Task 1. **§6.2** required/optional contact fields, placeholders, derived names → Task 2. **§6.3** JD format, filename, idempotence/`_2`, sanitization, prompt, CLI → Task 3. **§6.4** `analyze`, `--project`, log columns, missing-extra guidance, font stack → Tasks 4 and the skills' HTML templates. **§6.7** button markup/behavior/notices/no-outbox/relative paths/guards → Tasks 5–7. **§8/§9** error table and tests → Tasks 1–7 (each named Review Focus item is pinned: 1 → T1, 2 → T2, 3 → T3, 4 → T3/T5/T6, 5 → T4).
- **Placeholder scan:** none — every code/test step has full content; the two skills are complete texts. (The `<path-to>/seed_smoke_project.py` in the manual smoke refers to the seed script created during the live-radar work; if it is unavailable, any temp project with a seeded DB works.)
- **Type/name consistency:** `ResolvedResume(path, source)`, `resolve_master_resume(root, *, explicit, profile)`, `find_personalization/find_cover_sample/find_review_evidence`, `is_example`, `ContactInfo.first_name/last_name`, `contact_problems`, `Storage.get_job_for_jd`, `ExportResult(path, relative_path, created, prompt)`, `export_jd(storage, source_key, job_id, *, project_root, output_root, today)`, `analyze(content_height, page_count, target_pages)`, `append_row(path, row)`, `write_jd(storage, req, *, project_root, output_root, today)`, `resumeNotice(status, json, copied)`, and the `resume-btn` class are used identically across Tasks 1–10.
- **Known small friction to watch:** Task 3's `sanitize` intentionally drops non-ASCII letters (a filename-safety choice; accents are removed, not transliterated) — the test pins this; Task 8 is a long file, so reviewers should read it as prose against the spec's §6.6/§6.8 rather than by diff hunks.
