# Missed-job diagnostics and near-miss discovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a job the prefilter rejected explainable on demand (`job-hunter why-missed`), surface likely-relevant rejected jobs and vocabulary gaps periodically (`job-hunter near-misses`), and apply vocabulary fixes through the existing human-gated flow.

**Architecture:** Three small pure modules over read-only SQLite: `active_pool.find_jobs` (resolve a user's reference to stored jobs), `vocabulary.py` (title phrases and "how many rejected jobs would this term admit" using the real `evaluate_prefilter`), then `why_missed.py` and `near_miss.py` built on them, each with a thin CLI glue function wired into `cli.py`. The filtering gate itself is unchanged.

**Tech Stack:** Python >= 3.11, pydantic models already in the repo, sqlite3 (read-only URI connections), stdlib `html`/`csv`/`re`, pytest, ruff, `uv`.

**Spec:** `docs/superpowers/specs/2026-10-01-missed-job-diagnostics-design.md`

## Spec adjustments made while planning (read first)

1. **URL lookup limit.** A careers-site URL only resolves when its path carries an id this project stored. The user's Ford link (`careers.ford.com/job/-/-/48560/101370456832`) does **not** contain Ford's stored id (`71202`; the stored `canonical_url` is the Oracle page). `why-missed` therefore matches URLs by exact `canonical_url` or by digit tokens found in the URL path against `job_id` / `canonical_url`, and when nothing matches it prints a hint to use the job id, `source_key:job_id`, or the title. The Ford case is reproduced with `ford:71202` or the title.
2. **Read-only access.** `find_jobs` and the small status lookups use `sqlite3.connect("file:...?mode=ro", uri=True)`. The rejected-job pool reuses the existing `raw_active_jobs()` (which opens `Storage` but writes no rows), exactly as `refilter_archive.py` does.
3. **Preview command syntax.** `scripts/diff_profile.py` takes `--add field:term` (colon), so every suggested preview is `uv run python scripts/diff_profile.py --add "target_title_terms:<term>"`.
4. **`StoredJob`.** `find_jobs` returns `StoredJob(job, status, missing_count)` because the `Job` model has no `status`/`missing_count`.
5. **Task 7 is human-gated** (applying vocabulary needs the user's confirmation twice, per `skills/job-feedback`); the controller runs it with the user, not a subagent.

## Global Constraints

- Python `>=3.11`; run everything through `uv run`; baseline is the full suite green (`uv run pytest -q -m "not live"`, about 889 tests, 1 skipped for missing `pypdf`) and `uv run ruff check .` clean. Keep both green after every task.
- Work on branch `feature/missed-job-diagnostics` (created in Task 0 from `dev`). Commit steps run on that branch only; the user merges manually. End commit messages with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` as a separate `-m` paragraph (never on the subject line).
- **Never touch real data in tests:** tests use `tmp_path` and `monkeypatch.chdir(tmp_path)`; never write the real `data/` or `config/`. Manual smoke runs use `--output-dir "$(mktemp -d)"` and `--no-state`.
- Both new commands are **read-only with respect to the profile, the DB, archives and the radar**: they never edit `candidate_profile.yaml`, never write `data/searches/` or `data/radar/`, and near-misses are never LLM-scored or added to `candidates`.
- Python owns retrieval/filtering; skills only orchestrate and gate on user confirmation. Do not move any judgment into code beyond deterministic counting.
- Positive matching in the real gate is a **substring test on `"{title} {department}".lower()`** (see `prefilter.evaluate_prefilter`), not word-boundary matching. Suggested terms and gains must use `evaluate_prefilter` itself so semantics cannot drift.
- `--project` must work **after** the subcommand (`job-hunter why-missed ford:71202 --project X`): register the new subparsers before `cli.py`'s existing `for subparser in sub.choices.values(): add_project_argument(...)` loop, and add a parser test.
- Any content change to a `skills/*/SKILL.md` bumps its frontmatter `version` (minor for a new capability). Skills stay generic: no employer names or personal data (`tests/test_skills_portable.py`, `tests/test_no_owner_pii_in_shareable_files.py`).
- Reports are written with `atomic_write_text`; timestamps in filenames are local time.

## Review Focus

1. A careers-site URL whose ids do not match the stored `canonical_url` (the real Ford link) must produce the helpful "not found" message and exit 2, never a crash or a wrong job. (Task 1, Task 4)
2. Suggested terms must be real substrings of the job's own lowercased title and must actually admit the job under `evaluate_prefilter` (the `&` in "Vehicle Calibration & Test Supervisor" must not yield a phantom `calibration test`). (Tasks 2-3)
3. A suggestion that admits many other jobs must be flagged "broad" with its exact count, and a soft-excluded job must not be reported as admitted. (Tasks 2-3)
4. A job that passes the filter now but is absent from the resolved archive because its source failed in that archive must say the radar can only show it through the unscored stale-source fallback. (Task 3)
5. Near-miss scoring must ignore generic terms, count a term at most once per job, ignore text that is recurring per-source boilerplate, and return zero rows (not an error) when the profile has no strong terms. (Task 5)
6. Near-miss state must advance only after the report was written successfully; `--all`, `--no-state` and the first-run cap must behave as specified; an unwritable output location exits 2 with a message. (Task 6)
7. Reports must HTML-escape titles/departments (a title containing `<script>` or `&`). (Task 6)

---

## Task 0: Branch and baseline

**Files:** none modified (the spec and this plan are committed).

- [ ] **Step 1: Confirm the starting state**

Run: `git status --short && git branch --show-current`
Expected: branch `dev`; only `docs/superpowers/specs/2026-10-01-missed-job-diagnostics-design.md` and this plan are untracked. If anything else is modified, stop and report.

- [ ] **Step 2: Create the feature branch**

Run: `git checkout -b feature/missed-job-diagnostics`
Expected: `Switched to a new branch 'feature/missed-job-diagnostics'`.

- [ ] **Step 3: Baseline**

Run: `uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all pass, ruff clean. If red, stop and report.

- [ ] **Step 4: Commit the spec and plan**

```bash
git add docs/superpowers/specs/2026-10-01-missed-job-diagnostics-design.md docs/superpowers/plans/2026-10-01-missed-job-diagnostics.md
git commit -m "docs: spec and plan for missed-job diagnostics and near-miss discovery" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 1: `find_jobs` — resolve a user reference to stored jobs

**Files:**
- Modify: `src/job_hunter/active_pool.py` (add `StoredJob`, `find_jobs`; add `re` and `urlsplit` imports)
- Test: `tests/test_active_pool.py` (append)

**Interfaces:**
- Produces (used by Task 4): `StoredJob(job: Job, status: str, missing_count: int)` frozen dataclass; `find_jobs(database_path: Path, ref: str, *, limit: int = 50) -> list[StoredJob]`. Resolution order, first non-empty wins: `source_key:job_id` (when `ref` has no `://`); for `http(s)://` refs: exact `canonical_url`, then each digit run of >= 4 digits in the URL path (longest first) matched against `job_id` or as a substring of `canonical_url`; for refs without spaces: exact `job_id` across sources; finally case-insensitive title substring (newest `last_seen_at` first). Any status, any eligibility.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_active_pool.py`; it already imports `make_job`, `Storage`)

```python
from job_hunter.active_pool import StoredJob, find_jobs


def _seed_lookup(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(
                source_key="ford", job_id="71202", title="Vehicle Calibration & Test Supervisor",
                url="https://efds.example/hcmUI/job/71202",
            )
        )
        storage.upsert_job(
            make_job(
                source_key="ford", job_id="71203", title="Calibration Engineer",
                url="https://efds.example/hcmUI/job/71203",
            )
        )
        storage.upsert_job(
            make_job(source_key="abb", job_id="71202", title="Test Supervisor", url="https://abb.example/j/71202")
        )
    return db


def test_find_jobs_by_source_and_id(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "ford:71202")
    assert [(s.job.source_key, s.job.job_id) for s in found] == [("ford", "71202")]
    assert isinstance(found[0], StoredJob)
    assert (found[0].status, found[0].missing_count) == ("active", 0)


def test_find_jobs_by_exact_canonical_url(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "https://efds.example/hcmUI/job/71203")
    assert [s.job.job_id for s in found] == ["71203"]


def test_find_jobs_by_an_id_token_inside_a_different_url_shape(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "https://careers.example.com/job/-/-/48560/71203")
    assert [(s.job.source_key, s.job.job_id) for s in found] == [("ford", "71203")]


def test_find_jobs_url_with_no_matching_id_returns_nothing(tmp_path):
    """The real Ford careers link carries ids that are not the stored job id (71202)."""
    ref = "https://www.careers.ford.com/job/-/-/48560/101370456832"
    assert find_jobs(_seed_lookup(tmp_path), ref) == []


def test_find_jobs_bare_id_can_match_several_sources(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "71202")
    assert sorted(s.job.source_key for s in found) == ["abb", "ford"]


def test_find_jobs_by_title_substring_is_case_insensitive(tmp_path):
    db = _seed_lookup(tmp_path)
    assert sorted(s.job.source_key for s in find_jobs(db, "test supervisor")) == ["abb", "ford"]
    assert [s.job.job_id for s in find_jobs(db, "VEHICLE CALIBRATION & TEST")] == ["71202"]


def test_find_jobs_includes_closed_and_ineligible_jobs(tmp_path):
    db = _seed_lookup(tmp_path)
    with Storage(db) as storage:
        storage.connection.execute(
            "UPDATE jobs SET status='closed', missing_count=3, us_eligible=0 "
            "WHERE source_key='ford' AND job_id='71203'"
        )
        storage.connection.commit()
    (found,) = find_jobs(db, "ford:71203")
    assert (found.status, found.missing_count, found.job.us_eligible) == ("closed", 3, False)


def test_find_jobs_blank_unknown_and_wildcard_refs_return_nothing(tmp_path):
    db = _seed_lookup(tmp_path)
    assert find_jobs(db, "") == []
    assert find_jobs(db, "   ") == []
    assert find_jobs(db, "nothing like this anywhere") == []
    assert find_jobs(db, "%") == []  # a literal percent sign, not a SQL wildcard


def test_find_jobs_title_with_a_colon_and_space_is_not_read_as_source_id(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(make_job(job_id="9", title="Engineer: Perception Systems"))
    assert [s.job.job_id for s in find_jobs(db, "Engineer: Perception")] == ["9"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_active_pool.py -q -k find_jobs`
Expected: collection error / FAIL (`cannot import name 'find_jobs'`).

- [ ] **Step 3: Implement** — in `src/job_hunter/active_pool.py` add `import re`, `from dataclasses import dataclass`, `from urllib.parse import urlsplit` to the imports, then append:

```python
@dataclass(frozen=True)
class StoredJob:
    """A stored job plus the two storage columns the `Job` model does not carry."""

    job: Job
    status: str
    missing_count: int


_SOURCE_AND_ID = re.compile(r"^([A-Za-z0-9_]+):([^/\s].*)$")


def _lookups(conn: sqlite3.Connection, ref: str, limit: int):
    """Successive attempts to resolve `ref`; `find_jobs` returns the first non-empty one."""
    if "://" not in ref:
        match = _SOURCE_AND_ID.match(ref)
        if match:
            yield conn.execute(
                "SELECT * FROM jobs WHERE source_key=? AND job_id=?", (match[1], match[2])
            ).fetchall()
    if ref.lower().startswith(("http://", "https://")):
        yield conn.execute("SELECT * FROM jobs WHERE canonical_url=?", (ref,)).fetchall()
        tokens = sorted(set(re.findall(r"\d{4,}", urlsplit(ref).path)), key=len, reverse=True)
        for token in tokens:
            yield conn.execute(
                "SELECT * FROM jobs WHERE job_id=? OR instr(canonical_url, ?) > 0 LIMIT ?",
                (token, token, limit),
            ).fetchall()
        return
    if " " not in ref:
        yield conn.execute("SELECT * FROM jobs WHERE job_id=? LIMIT ?", (ref, limit)).fetchall()
    yield conn.execute(
        "SELECT * FROM jobs WHERE instr(lower(title), ?) > 0 ORDER BY last_seen_at DESC LIMIT ?",
        (ref.lower(), limit),
    ).fetchall()


def find_jobs(database_path: Path, ref: str, *, limit: int = 50) -> list[StoredJob]:
    """Resolve a user's reference to stored jobs (any status, any eligibility), read-only.

    Order, first non-empty wins: `source_key:job_id`; for URLs, the exact `canonical_url` then
    digit tokens from the URL path matched against `job_id`/`canonical_url`; a bare `job_id`;
    finally a case-insensitive title substring (`instr`, so `%`/`_` are literal). A careers-site
    URL whose ids were never stored legitimately resolves to nothing."""
    ref = ref.strip()
    if not ref:
        return []
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        for rows in _lookups(conn, ref, limit):
            if rows:
                return [StoredJob(_row_to_job(r), r["status"], r["missing_count"]) for r in rows]
        return []
    finally:
        conn.close()
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_active_pool.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/active_pool.py tests/test_active_pool.py
git commit -m "feat: find_jobs resolves a reference to stored jobs read-only" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 2: `vocabulary.py` — title phrases and term gain

**Files:**
- Create: `src/job_hunter/vocabulary.py`
- Test: `tests/test_vocabulary.py` (create)

**Interfaces:**
- Consumes: `raw_active_jobs` (`active_pool.py`), `evaluate_prefilter`/`passes_recency` (`prefilter.py`), `CandidateProfile`, `PrefilterRule`.
- Produces (used by Tasks 3 and 5): `GENERIC_TITLE_TOKENS: frozenset[str]`; `title_phrases(title: str, max_words: int = 3) -> list[str]`; `rejected_pool(database_path: Path, profile: CandidateProfile, max_age_days: int, *, now: datetime | None = None) -> list[Job]` (active, U.S.-eligible, recency-passing jobs whose rule is `NO_POSITIVE_MATCH`); `PhraseGain(count: int, samples: tuple[str, ...])`; `phrase_gain(profile, phrase, pool, *, sample_size: int = 3) -> PhraseGain` (jobs in `pool` that pass the real gate once `phrase` is appended to `target_title_terms`).

- [ ] **Step 1: Write the failing tests** — create `tests/test_vocabulary.py`:

```python
from datetime import UTC, datetime

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage
from job_hunter.vocabulary import (
    GENERIC_TITLE_TOKENS,
    PhraseGain,
    phrase_gain,
    rejected_pool,
    title_phrases,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def make_job(**updates):
    values = dict(
        source_key="ford", source_platform="test", company="Ford", job_id="1",
        title="Vehicle Calibration & Test Supervisor", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
        posted_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    values.update(updates)
    return Job(**values)


def test_title_phrases_drop_ampersands_and_order_by_size_then_position():
    phrases = title_phrases("Vehicle Calibration & Test Supervisor")
    assert phrases[:4] == ["vehicle", "calibration", "test", "supervisor"]
    assert "vehicle calibration" in phrases and "test supervisor" in phrases
    # a window spanning the ampersand is still produced here; callers keep only phrases that are
    # real substrings of the title (see why_missed.suggest_terms)
    assert "calibration test" in phrases


def test_title_phrases_skip_windows_made_only_of_generic_tokens():
    assert title_phrases("Senior Engineer") == []
    assert "senior" in GENERIC_TITLE_TOKENS
    assert "senior perception" in title_phrases("Senior Perception Engineer")
    assert "senior" not in title_phrases("Senior Perception Engineer")


def test_title_phrases_are_unique():
    phrases = title_phrases("Test Test Supervisor")
    assert len(phrases) == len(set(phrases))


def test_rejected_pool_holds_only_no_positive_match_jobs_that_are_eligible_and_recent(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(make_job(job_id="1", title="Vehicle Calibration & Test Supervisor"))
        storage.upsert_job(make_job(job_id="2", title="Robotics Engineer"))            # passes the gate
        storage.upsert_job(make_job(job_id="3", title="Calibration Intern"))           # exclude_title_terms
        storage.upsert_job(
            make_job(job_id="4", title="Calibration Lead", posted_at=datetime(2020, 1, 1, tzinfo=UTC))
        )                                                                               # not recent
        storage.upsert_job(make_job(job_id="5", title="Chef", us_eligible=False))      # not eligible
    profile = CandidateProfile(target_title_terms=["robotics"], exclude_title_terms=["intern"])
    pool = rejected_pool(db, profile, 30, now=NOW)
    assert [j.job_id for j in pool] == ["1"]


def _pool():
    return [
        make_job(job_id="a", title="Vehicle Calibration & Test Supervisor"),
        make_job(job_id="b", source_key="apple", title="Camera Calibration and Test Engineer"),
        make_job(job_id="c", source_key="abb", title="Test Supervisor"),
        make_job(job_id="d", title="Chef"),
    ]


def test_phrase_gain_counts_pool_jobs_the_real_gate_would_admit():
    profile = CandidateProfile(target_title_terms=["robotics"])
    gain = phrase_gain(profile, "calibration", _pool())
    assert isinstance(gain, PhraseGain)
    assert gain.count == 2
    assert gain.samples == (
        "ford: Vehicle Calibration & Test Supervisor",
        "apple: Camera Calibration and Test Engineer",
    )
    assert phrase_gain(profile, "test supervisor", _pool()).count == 2
    assert phrase_gain(profile, "no such phrase", _pool()) == PhraseGain(0, ())


def test_phrase_gain_respects_soft_excludes_exactly_like_production():
    profile = CandidateProfile(target_title_terms=["robotics"], soft_exclude_terms=["supervisor"])
    assert phrase_gain(profile, "test supervisor", _pool()).count == 0


def test_phrase_gain_samples_are_capped():
    pool = [make_job(job_id=str(i), title=f"Calibration Role {i}") for i in range(10)]
    gain = phrase_gain(CandidateProfile(target_title_terms=["robotics"]), "calibration", pool, sample_size=3)
    assert gain.count == 10 and len(gain.samples) == 3
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_vocabulary.py -q`
Expected: collection error (`No module named 'job_hunter.vocabulary'`).

- [ ] **Step 3: Implement** — create `src/job_hunter/vocabulary.py`:

```python
"""Shared helpers for the missed-job diagnostics (`why_missed.py`) and the periodic near-miss scan
(`near_miss.py`): what a job title can contribute as a candidate profile term, and how many
already-rejected jobs a term would admit.

Gains are computed with the real `evaluate_prefilter` on a widened copy of the profile, so the
substring-on-title+department semantics and the soft-exclude rule cannot drift from production.
Nothing here writes anything."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .active_pool import raw_active_jobs
from .config import CandidateProfile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency

#: Tokens too common to justify a profile term on their own (a candidate phrase must contain at
#: least one token that is not in this set and has >= 3 characters).
GENERIC_TITLE_TOKENS = frozenset(
    {
        "senior", "sr", "staff", "principal", "lead", "manager", "engineer", "engineering",
        "specialist", "director", "associate", "analyst", "jr", "junior", "head", "vp",
        "contract", "temporary", "ii", "iii", "iv", "i", "of", "and", "the", "for", "to",
        "in", "at", "with", "a", "an",
    }
)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*")


def title_phrases(title: str, max_words: int = 3) -> list[str]:
    """Contiguous 1..max_words-word windows of the lower-cased title (ampersands dropped), skipping
    windows made only of generic/short tokens, unique, ordered by window size then position."""
    tokens = _TOKEN.findall(title.lower().replace("&", " "))
    seen: set[str] = set()
    phrases: list[str] = []
    for size in range(1, max_words + 1):
        for start in range(len(tokens) - size + 1):
            window = tokens[start : start + size]
            if not any(len(t) >= 3 and t not in GENERIC_TITLE_TOKENS for t in window):
                continue
            phrase = " ".join(window)
            if phrase not in seen:
                seen.add(phrase)
                phrases.append(phrase)
    return phrases


def rejected_pool(
    database_path: Path,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    now: datetime | None = None,
) -> list[Job]:
    """Active, U.S.-eligible, recency-passing jobs the positive-term gate rejected
    (`no_positive_match`) — deliberate rejections (exclude/soft-exclude rules) are not included."""
    return [
        job
        for job in raw_active_jobs(database_path)
        if passes_recency(job, max_age_days, now=now)
        and evaluate_prefilter(job, profile).rule is PrefilterRule.NO_POSITIVE_MATCH
    ]


@dataclass(frozen=True)
class PhraseGain:
    count: int
    samples: tuple[str, ...]


def _gate_text(job: Job) -> str:
    return f"{job.title} {job.department or ''}".lower()


def phrase_gain(
    profile: CandidateProfile, phrase: str, pool: list[Job], *, sample_size: int = 3
) -> PhraseGain:
    """How many jobs in `pool` pass the real gate once `phrase` is added to `target_title_terms`.
    Jobs whose title+department lack the phrase cannot change outcome, so only the others are
    evaluated (the result is identical, just faster)."""
    widened = profile.model_copy(
        update={"target_title_terms": [*profile.target_title_terms, phrase]}
    )
    needle = phrase.lower()
    admitted = [
        job for job in pool if needle in _gate_text(job) and evaluate_prefilter(job, widened).passes
    ]
    samples = tuple(f"{job.source_key}: {job.title}" for job in admitted[:sample_size])
    return PhraseGain(len(admitted), samples)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_vocabulary.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/vocabulary.py tests/test_vocabulary.py
git commit -m "feat: vocabulary helpers - title phrases and term gain over rejected jobs" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 3: `why_missed.py` core — explain, suggest, render

**Files:**
- Create: `src/job_hunter/why_missed.py`
- Test: `tests/test_why_missed.py` (create)

**Interfaces:**
- Consumes (Tasks 1-2): `StoredJob`, `title_phrases`, `phrase_gain`; `evaluate_prefilter`, `passes_recency`, `PrefilterRule`.
- Produces (used by Task 4): `ArchiveInfo(path: str, in_candidates: bool, source_status: str | None)`; `Stage(name: str, ok: bool | None, detail: str)` (`ok=None` means not applicable); `TermSuggestion(term: str, gain: int, broad: bool, samples: tuple[str, ...])`; `WhyMissed(source_key, job_id, title, stages, verdict, suggestions, preview_commands)` with `to_dict()`; `suggest_terms(job, profile, pool, *, max_suggestions=5, broad_threshold=40) -> list[TermSuggestion]`; `explain(stored, *, profile, max_age_days, source_health, archive, assessment, pool, keywords=None, now=None, broad_threshold=40, max_suggestions=5) -> WhyMissed` where `pool` is a zero-argument callable returning the rejected pool (evaluated lazily, only when suggestions are needed); `render_text(result) -> str`.

Stage order and names (tests rely on them): `collected`, `U.S.-eligible`, `recency`, `prefilter`, `in archive`, `assessed`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_why_missed.py`:

```python
import json
from datetime import UTC, datetime

from job_hunter.active_pool import StoredJob
from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.why_missed import ArchiveInfo, explain, render_text, suggest_terms

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PROFILE = CandidateProfile(
    target_title_terms=["vehicle test", "robotics"], target_domains=["validation", "ADAS"]
)


def make_job(**updates):
    values = dict(
        source_key="ford", source_platform="test", company="Ford", job_id="71202",
        title="Vehicle Calibration & Test Supervisor", url="https://example.com/71202",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
        location_evidence="structured U.S. country", department=None,
        posted_at=datetime(2026, 9, 30, tzinfo=UTC),
        first_seen_at=datetime(2026, 9, 30, 17, 28, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 1, 3, 24, tzinfo=UTC),
        description="validation validation verification vehicle", content_hash="h1",
    )
    values.update(updates)
    return Job(**values)


def stored(job=None, status="active", missing=0):
    return StoredJob(job or make_job(), status, missing)


def pool():
    return [
        make_job(),
        make_job(job_id="2", source_key="apple", title="Camera Calibration and Test Engineer"),
        make_job(job_id="3", source_key="abb", title="Test Supervisor"),
    ]


def run(job=None, **overrides):
    kwargs = dict(
        profile=PROFILE, max_age_days=30, now=NOW,
        source_health={"last_status": "ok", "last_job_count": 842},
        archive=ArchiveInfo("data/searches/default_2026-09-30.json", False, "ok"),
        assessment=None, pool=pool,
    )
    kwargs.update(overrides)
    return explain(stored(job), **kwargs)


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def test_a_gate_rejection_is_reported_with_its_rule_and_the_empty_department():
    result = run()
    assert [s.name for s in result.stages] == [
        "collected", "U.S.-eligible", "recency", "prefilter", "in archive", "assessed",
    ]
    prefilter = stage(result, "prefilter")
    assert prefilter.ok is False
    assert "no_positive_match" in prefilter.detail and "department is empty" in prefilter.detail
    assert result.verdict.startswith("Stopped at: prefilter")
    assert stage(result, "collected").ok is True
    assert "ok (842 jobs)" in stage(result, "collected").detail


def test_suggestions_list_real_substrings_with_exact_gain_and_preview_commands():
    result = run()
    terms = {s.term: s for s in result.suggestions}
    assert "test supervisor" in terms and "calibration" in terms
    assert terms["test supervisor"].gain == 2      # this job and the ABB one
    assert terms["calibration"].gain == 2          # this job and the Apple one
    assert "calibration test" not in terms         # not a substring of the real title ('&' between)
    assert all(s.term in "vehicle calibration & test supervisor" for s in result.suggestions)
    assert result.suggestions == sorted(result.suggestions, key=lambda s: (s.gain, -len(s.term), s.term))
    assert 'uv run python scripts/diff_profile.py --add "target_title_terms:test supervisor"' in result.preview_commands


def test_broad_suggestions_are_flagged():
    result = run(pool=pool)
    broad = suggest_terms(make_job(), PROFILE, pool(), broad_threshold=1)
    assert any(s.broad for s in broad)
    assert not any(s.broad for s in result.suggestions)


def test_no_suggestions_for_other_rules_or_with_a_keyword_override():
    assert run(job=make_job(title="Robotics Supervisor")).suggestions == []
    assert run(keywords=["adas"]).suggestions == []


def test_a_closed_job_stops_at_collected():
    result = explain(
        stored(status="closed", missing=3), profile=PROFILE, max_age_days=30, now=NOW,
        source_health=None, archive=None, assessment=None, pool=pool,
    )
    assert result.verdict.startswith("Stopped at: collected")
    assert "missed 3 run(s)" in stage(result, "collected").detail


def test_stale_posting_and_ineligible_location_are_reported():
    old = run(job=make_job(posted_at=datetime(2020, 1, 1, tzinfo=UTC)))
    assert stage(old, "recency").ok is False
    foreign = run(job=make_job(us_eligible=False, location_evidence="Germany"))
    assert stage(foreign, "U.S.-eligible").ok is False and "Germany" in stage(foreign, "U.S.-eligible").detail


def test_job_that_passes_but_is_missing_from_an_archive_whose_source_failed():
    job = make_job(title="Robotics Supervisor")
    result = run(job=job, archive=ArchiveInfo("data/searches/default_2026-09-30.json", False, "failed"))
    assert stage(result, "prefilter").ok is True
    assert stage(result, "in archive").ok is False
    assert "stale-source fallback" in stage(result, "in archive").detail
    assert result.verdict.startswith("Stopped at: in archive")


def test_assessment_states():
    job = make_job(title="Robotics Supervisor")
    in_archive = ArchiveInfo("a.json", True, "ok")
    scored = run(job=job, archive=in_archive, assessment={"score": 62, "content_hash": "h1"})
    assert stage(scored, "assessed").ok is True and "62" in stage(scored, "assessed").detail
    assert "should appear" in scored.verdict
    stale = run(job=job, archive=in_archive, assessment={"score": 62, "content_hash": "OLD"})
    assert stage(stale, "assessed").ok is False and "changed" in stage(stale, "assessed").detail
    missing = run(job=job, archive=in_archive, assessment=None)
    assert stage(missing, "assessed").ok is False and "not assessed" in stage(missing, "assessed").detail


def test_no_archive_is_not_applicable_not_a_failure():
    result = run(job=make_job(title="Robotics Supervisor"), archive=None, assessment={"score": 70, "content_hash": "h1"})
    assert stage(result, "in archive").ok is None
    assert "No search archive" in result.verdict


def test_to_dict_is_json_serializable_and_render_text_names_every_stage():
    result = run()
    json.dumps(result.to_dict())
    text = render_text(result)
    for name in ("collected", "U.S.-eligible", "recency", "prefilter", "in archive", "assessed"):
        assert name in text
    assert "Verdict: Stopped at: prefilter" in text
    assert "test supervisor" in text and "job-feedback" in text
    assert "Nothing was changed" in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_why_missed.py -q`
Expected: collection error (`No module named 'job_hunter.why_missed'`).

- [ ] **Step 3: Implement** — create `src/job_hunter/why_missed.py`:

```python
"""`job-hunter why-missed`: explain, stage by stage, why a stored job was or was not in the
candidates/radar, and for a gate rejection propose the smallest title terms that would admit it
(with how many other already-rejected jobs each would also admit).

Everything here is deterministic and read-only: it never edits the profile, the DB, an archive or
the radar. Suggestions only ever reach `candidate_profile.yaml` through the `job-feedback` skill,
which previews with `scripts/diff_profile.py` and stops for the user's confirmation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .active_pool import StoredJob
from .config import CandidateProfile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency
from .vocabulary import phrase_gain, title_phrases


@dataclass(frozen=True)
class ArchiveInfo:
    path: str
    in_candidates: bool
    source_status: str | None


@dataclass
class Stage:
    name: str
    ok: bool | None  # None: not applicable / unknown
    detail: str


@dataclass(frozen=True)
class TermSuggestion:
    term: str
    gain: int
    broad: bool
    samples: tuple[str, ...]


@dataclass
class WhyMissed:
    source_key: str
    job_id: str
    title: str
    stages: list[Stage]
    verdict: str
    suggestions: list[TermSuggestion] = field(default_factory=list)
    preview_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def suggest_terms(
    job: Job,
    profile: CandidateProfile,
    pool: list[Job],
    *,
    max_suggestions: int = 5,
    broad_threshold: int = 40,
) -> list[TermSuggestion]:
    """Title phrases that, appended to `target_title_terms`, admit `job` under the real gate,
    most precise first (smallest gain, then longest). A phrase that is not a substring of the
    job's own lower-cased title, or that a later rule (e.g. a soft-exclude) still rejects, is not
    offered. `gain` counts every pool job admitted, this one included when it is in the pool."""
    title_lower = job.title.lower()
    suggestions: list[TermSuggestion] = []
    for phrase in title_phrases(job.title):
        if phrase not in title_lower:
            continue
        widened = profile.model_copy(
            update={"target_title_terms": [*profile.target_title_terms, phrase]}
        )
        if not evaluate_prefilter(job, widened).passes:
            continue
        gain = phrase_gain(profile, phrase, pool)
        suggestions.append(TermSuggestion(phrase, gain.count, gain.count > broad_threshold, gain.samples))
    suggestions.sort(key=lambda s: (s.gain, -len(s.term), s.term))
    return suggestions[:max_suggestions]


def _day(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d") if moment else "?"


def _prefilter_stage(job: Job, profile: CandidateProfile, keywords: list[str] | None):
    decision = evaluate_prefilter(job, profile, keywords=keywords)
    if decision.passes:
        rescued = f" (rescued by {decision.rescued_by!r})" if decision.rescued_by else ""
        return decision, Stage("prefilter", True, f"matched {decision.term!r}{rescued}")
    if decision.rule is PrefilterRule.NO_POSITIVE_MATCH:
        department = "empty" if not job.department else repr(job.department)
        detail = (
            "no_positive_match: no target term appears in the title or department "
            f"(department is {department})"
        )
    elif decision.term:
        detail = f"{decision.rule.value}: matched {decision.term!r}"
    else:
        detail = decision.rule.value
    return decision, Stage("prefilter", False, detail)


def explain(
    stored: StoredJob,
    *,
    profile: CandidateProfile,
    max_age_days: int,
    source_health: dict | None,
    archive: ArchiveInfo | None,
    assessment: dict | None,
    pool: Callable[[], list[Job]],
    keywords: list[str] | None = None,
    now: datetime | None = None,
    broad_threshold: int = 40,
    max_suggestions: int = 5,
) -> WhyMissed:
    job = stored.job
    stages: list[Stage] = []

    collected = (
        f"status {stored.status}; first seen {_day(job.first_seen_at)}, last seen "
        f"{_day(job.last_seen_at)}, missed {stored.missing_count} run(s)"
    )
    if source_health:
        collected += (
            f"; source last run: {source_health.get('last_status')} "
            f"({source_health.get('last_job_count')} jobs)"
        )
    stages.append(Stage("collected", stored.status == "active", collected))
    stages.append(
        Stage("U.S.-eligible", bool(job.us_eligible), job.location_evidence or job.location_raw or "no evidence recorded")
    )
    recent = passes_recency(job, max_age_days, now=now)
    posted = f"posted {_day(job.posted_at)}" if job.posted_at else "no posted date (kept)"
    stages.append(Stage("recency", recent, f"{posted}; max age {max_age_days} days"))

    decision, prefilter = _prefilter_stage(job, profile, keywords)
    stages.append(prefilter)

    if archive is None:
        stages.append(Stage("in archive", None, "no search archive found to check"))
    elif archive.in_candidates:
        stages.append(Stage("in archive", True, f"yes, among the candidates in {archive.path}"))
    elif archive.source_status in ("failed", "unsupported"):
        stages.append(
            Stage(
                "in archive", False,
                f"no - {archive.path} has this source as {archive.source_status!r}, so the radar can "
                "only show its jobs through the stale-source fallback, which never scores them",
            )
        )
    else:
        stages.append(
            Stage(
                "in archive", False,
                f"no - not among the candidates in {archive.path} (collected after it was written, "
                "or filtered out when it was built)",
            )
        )

    if assessment is None:
        stages.append(Stage("assessed", False, "not assessed (the local review has not scored it)"))
    elif assessment.get("content_hash") != job.content_hash:
        stages.append(
            Stage("assessed", False, f"score {assessment.get('score')} is stale: the posting changed since it was scored")
        )
    else:
        stages.append(Stage("assessed", True, f"score {assessment.get('score')}"))

    failing = next((s for s in stages if s.ok is False), None)
    if failing is not None:
        verdict = f"Stopped at: {failing.name} - {failing.detail}"
    elif archive is None:
        verdict = "Passes every filter. No search archive was found to check membership."
    else:
        verdict = (
            "No blocking stage found: it is in the archive and assessed, so it should appear in "
            "the radar (check its section and score)."
        )

    suggestions: list[TermSuggestion] = []
    previews: list[str] = []
    if decision.rule is PrefilterRule.NO_POSITIVE_MATCH and keywords is None:
        suggestions = suggest_terms(
            job, profile, pool(), max_suggestions=max_suggestions, broad_threshold=broad_threshold
        )
        previews = [
            f'uv run python scripts/diff_profile.py --add "target_title_terms:{s.term}"'
            for s in suggestions
        ]
    return WhyMissed(job.source_key, job.job_id, job.title, stages, verdict, suggestions, previews)


def render_text(result: WhyMissed) -> str:
    marks = {True: "ok  ", False: "FAIL", None: "n/a "}
    lines = [f'Why was "{result.title}" ({result.source_key}:{result.job_id}) not in the radar?']
    lines += [f"  [{marks[s.ok]}] {s.name}: {s.detail}" for s in result.stages]
    lines.append(f"Verdict: {result.verdict}")
    if result.suggestions:
        lines.append("")
        lines.append(
            "Suggested title terms (each shows how many already-rejected jobs it would admit, "
            "including this one):"
        )
        for number, s in enumerate(result.suggestions, 1):
            broad = "  [broad]" if s.broad else ""
            lines.append(f'  {number}. "{s.term}" admits {s.gain} job(s){broad}')
            if s.samples:
                lines.append("       e.g. " + "; ".join(s.samples))
            lines.append(f"       preview: {result.preview_commands[number - 1]}")
    lines.append("")
    lines.append(
        "Nothing was changed. To add a term, use the job-feedback skill, which previews the exact "
        "jobs gained/lost and stops for your confirmation before writing the profile."
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_why_missed.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/why_missed.py tests/test_why_missed.py
git commit -m "feat: why-missed core - stage-by-stage explanation and term suggestions" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 4: `why-missed` CLI glue and wiring

**Files:**
- Modify: `src/job_hunter/why_missed.py` (append `cli_why_missed` and read helpers)
- Modify: `src/job_hunter/cli.py` (new subparser before the `--project` loop; dispatch after `settings = load_settings()`)
- Test: `tests/test_cli_missed_job.py` (create)

**Interfaces:**
- Consumes (Tasks 1-3): `find_jobs`, `explain`, `render_text`, `ArchiveInfo`, `rejected_pool`; `resolve_search_path` (`search_archive.py`); `load_profile`.
- Produces: `cli_why_missed(args, settings) -> int` — exit 0 explained; exit 2 for no match, several matches, or unreadable archive/profile. CLI: `job-hunter why-missed REF [--search PATH] [--keyword K] [--json]`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_cli_missed_job.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.cli import main, parser
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage

PROFILE_YAML = """\
target_title_terms: [vehicle test, robotics]
target_domains: [validation, ADAS]
strong_relevance_terms: [ADAS, perception, lidar, "sensor fusion", vehicle]
"""


def make_job(**updates):
    values = dict(
        source_key="ford", source_platform="test", company="Ford", job_id="71202",
        title="Vehicle Calibration & Test Supervisor",
        url="https://efds.example/hcmUI/job/71202", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, location_evidence="structured U.S. country",
        posted_at=datetime.now(UTC), description="validation verification vehicle",
    )
    values.update(updates)
    return Job(**values)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")
    (tmp_path / "config" / "candidate_profile.yaml").write_text(PROFILE_YAML)
    (tmp_path / "data" / "searches").mkdir(parents=True)
    with Storage(tmp_path / "data" / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job())
        storage.upsert_job(make_job(job_id="2", source_key="apple", title="Camera Calibration and Test Engineer"))
        storage.upsert_job(make_job(job_id="3", source_key="abb", title="Test Supervisor"))
    archive = {
        "summary": {}, "candidates": [],
        "source_health": [{"source_key": "ford", "company": "Ford", "status": "ok", "job_count": 842}],
    }
    (tmp_path / "data" / "searches" / "default_2026-09-30.json").write_text(json.dumps(archive))
    return tmp_path


def test_why_missed_explains_the_ford_case_end_to_end(project, capsys):
    exit_code = main(["why-missed", "ford:71202"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Stopped at: prefilter" in out and "no_positive_match" in out
    assert '"test supervisor"' in out and '"calibration"' in out
    assert "default_2026-09-30.json" in out
    assert "Nothing was changed" in out


def test_why_missed_accepts_a_bare_job_id_when_only_one_source_has_it(project, capsys):
    assert main(["why-missed", "71202", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["source_key"] == "ford"


def test_why_missed_by_title_and_json_shape(project, capsys):
    exit_code = main(["why-missed", "Vehicle Calibration & Test", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert data["source_key"] == "ford" and data["job_id"] == "71202"
    assert [s["name"] for s in data["stages"]][3] == "prefilter"
    assert {s["term"] for s in data["suggestions"]} >= {"test supervisor", "calibration"}


def test_the_real_ford_careers_url_is_not_found_with_a_helpful_hint(project, capsys):
    exit_code = main(["why-missed", "https://www.careers.ford.com/job/-/-/48560/101370456832"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "no stored job matches" in err and "job id" in err and "source-test" in err


def test_several_matches_ask_for_source_and_id(project, capsys):
    exit_code = main(["why-missed", "test supervisor"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "ford:71202" in err and "abb:3" in err


def test_keyword_override_suppresses_suggestions(project, capsys):
    assert main(["why-missed", "ford:71202", "--keyword", "ADAS", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["suggestions"] == []


def test_missing_archive_is_reported_not_fatal(project, capsys):
    (project / "data" / "searches" / "default_2026-09-30.json").unlink()
    assert main(["why-missed", "ford:71202", "--json"]) == 0
    stages = {s["name"]: s for s in json.loads(capsys.readouterr().out)["stages"]}
    assert stages["in archive"]["ok"] is None


def test_project_flag_works_after_the_new_subcommands():
    assert parser().parse_args(["why-missed", "ford:1", "--project", "/x"]).project == Path("/x")
    assert parser().parse_args(["--project", "/y", "why-missed", "ford:1"]).project == Path("/y")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_missed_job.py -q`
Expected: FAIL (`invalid choice: 'why-missed'`).

- [ ] **Step 3: Implement the glue** — append to `src/job_hunter/why_missed.py` (add `import json, sqlite3, sys` and `from pathlib import Path`, `from .active_pool import find_jobs`, `from .config import load_profile`, `from .search_archive import resolve_search_path`, `from .vocabulary import rejected_pool` to the imports):

```python
def _query_one(database_path: Path, sql: str, params: tuple) -> dict | None:
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _archive_info(search: Path | None, keyword: str | None, job: Job) -> ArchiveInfo | None:
    try:
        path = resolve_search_path(search=search, keyword=keyword)
    except FileNotFoundError:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    in_candidates = any(
        c.get("source_key") == job.source_key and c.get("job_id") == job.job_id
        for c in data.get("candidates", [])
    )
    status = next(
        (h.get("status") for h in data.get("source_health", []) if h.get("source_key") == job.source_key),
        None,
    )
    return ArchiveInfo(str(path), in_candidates, status)


def cli_why_missed(args, settings) -> int:
    ref = args.ref
    matches = find_jobs(settings.database_path, ref)
    if not matches:
        print(f"job-hunter: no stored job matches {ref!r}.", file=sys.stderr)
        print(
            "  Try the job id (e.g. 71202), source_key:job_id (e.g. ford:71202) or part of the title. "
            "A careers-site URL only matches when its path carries an id this project stored. If the "
            "job was never collected, check its source with `job-hunter source-test <key>`.",
            file=sys.stderr,
        )
        return 2
    if len(matches) > 1:
        print(
            f"job-hunter: {len(matches)} stored jobs match {ref!r}; re-run with source_key:job_id:",
            file=sys.stderr,
        )
        for number, match in enumerate(matches[:20], 1):
            print(
                f"  {number:>2}. {match.job.source_key}:{match.job.job_id}  {match.job.title}  [{match.status}]",
                file=sys.stderr,
            )
        return 2
    stored = matches[0]
    job = stored.job
    profile = load_profile()
    keywords = [t.strip() for t in args.keyword.split(",") if t.strip()] if args.keyword else None
    health = _query_one(
        settings.database_path,
        "SELECT last_status, last_job_count FROM source_health WHERE source_key=?", (job.source_key,),
    )
    assessment = _query_one(
        settings.database_path,
        "SELECT score, content_hash FROM assessments WHERE source_key=? AND job_id=?",
        (job.source_key, job.job_id),
    )
    result = explain(
        stored,
        profile=profile,
        max_age_days=settings.search.max_posting_age_days,
        source_health=health,
        archive=_archive_info(args.search, args.keyword, job),
        assessment=assessment,
        pool=lambda: rejected_pool(settings.database_path, profile, settings.search.max_posting_age_days),
        keywords=keywords,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_text(result))
    return 0
```

- [ ] **Step 4: Wire into `cli.py`** — after the `pipeline-status` subparser block (and before the `for subparser in sub.choices.values()` loop) add:

```python
    why_missed = sub.add_parser(
        "why-missed",
        help="explain why a stored job was not in the radar, and suggest title terms that would admit it",
    )
    why_missed.add_argument(
        "ref",
        help="job id, source_key:job_id, a URL (matched by exact URL or ids in its path) or part of the title",
    )
    why_missed.add_argument("--search", type=Path, default=None, help="check this archive instead of the newest")
    why_missed.add_argument("--keyword", default=None, help="evaluate against this keyword override, like `search --keyword`")
    why_missed.add_argument("--json", action="store_true")
```

and in `main()`, directly after `settings = load_settings()`:

```python
        if args.command == "why-missed":
            from .why_missed import cli_why_missed

            return cli_why_missed(args, settings)
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_cli_missed_job.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/why_missed.py src/job_hunter/cli.py tests/test_cli_missed_job.py
git commit -m "feat: job-hunter why-missed command" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 5: `near_miss.py` — scoring, boilerplate stripping, coverage, hints

**Files:**
- Create: `src/job_hunter/near_miss.py`
- Test: `tests/test_near_miss.py` (create)

**Interfaces:**
- Consumes (Tasks 1-2): `raw_active_jobs`, `evaluate_prefilter`, `passes_recency`, `PrefilterRule`, `title_phrases`, `phrase_gain`.
- Produces (used by Task 6): `GENERIC_STRONG_TERMS: frozenset[str]` = `{vehicle, behavior, behaviour, driving, chassis, camera, radar}`; `html_to_text(text) -> str`; `strip_boilerplate(jobs, *, threshold=0.3, min_postings=5, min_chars=40) -> dict[tuple[str, str], str]` (keyed `(source_key, job_id)`); `matched_terms(text, terms) -> list[str]`; `NearMiss` dataclass (`source_key, job_id, company, title, url, department, posted_at, first_seen_at, terms: tuple[str, ...], occurrences: int`, property `score = len(terms)`); `SourceCoverage(source_key, total, empty_department)`; `VocabularyHint(term, near_miss_jobs, gain, samples)`; `ScanResult(rows, pool_size, eligible_recent, empty_department, coverage, hints)`; `scan(database_path, profile, max_age_days, *, ignore_terms=GENERIC_STRONG_TERMS, min_terms=3, since=None, limit=None, now=None) -> ScanResult`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_near_miss.py`:

```python
from datetime import UTC, datetime, timedelta

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.near_miss import (
    GENERIC_STRONG_TERMS,
    html_to_text,
    matched_terms,
    scan,
    strip_boilerplate,
)
from job_hunter.storage import Storage

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PROFILE = CandidateProfile(
    target_title_terms=["robotics"],
    strong_relevance_terms=["ADAS", "perception", "lidar", "sensor fusion", "vehicle"],
)


def make_job(**updates):
    values = dict(
        source_key="acme", source_platform="test", company="Acme", job_id="1",
        title="Chef de Cuisine", url="https://example.com/1", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, posted_at=datetime(2026, 9, 30, tzinfo=UTC),
        description="",
    )
    values.update(updates)
    return Job(**values)


def seed(tmp_path, jobs, first_seen=None):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        for job in jobs:
            storage.upsert_job(job)
        for (source, job_id), stamp in (first_seen or {}).items():
            storage.connection.execute(
                "UPDATE jobs SET first_seen_at=? WHERE source_key=? AND job_id=?",
                (stamp.isoformat(), source, job_id),
            )
        storage.connection.commit()
    return db


def test_html_to_text_strips_tags_unescapes_and_keeps_block_breaks():
    text = html_to_text("<div><p>First &amp; second</p><p>Third</p><ul><li>a</li><li>b</li></ul></div>")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines == ["First & second", "Third", "a", "b"]


def test_matched_terms_use_word_boundaries_and_ignore_case():
    assert matched_terms("Works on ADAS and lidar; NOT adaptive", ["ADAS", "lidar", "adapt"]) == ["ADAS", "lidar"]
    assert matched_terms("sensor-fusion team", ["sensor fusion"]) == []
    assert matched_terms("", ["ADAS"]) == []


def test_default_generic_terms_are_documented():
    assert {"vehicle", "driving", "camera"} <= GENERIC_STRONG_TERMS


def test_strip_boilerplate_removes_paragraphs_recurring_across_a_source():
    blurb = "We build autonomous driving technology with ADAS perception lidar sensor fusion for everyone."
    jobs = [make_job(job_id=str(i), description=f"{blurb}\nUnique line {i} about chefs and kitchens here.") for i in range(6)]
    jobs.append(make_job(job_id="x", description=f"{blurb}\nThis role works on perception for real, with a long sentence."))
    cleaned = strip_boilerplate(jobs)
    assert "autonomous driving technology" not in cleaned[("acme", "x")]
    assert "works on perception" in cleaned[("acme", "x")]


def test_strip_boilerplate_keeps_everything_for_small_sources():
    blurb = "We build autonomous driving technology with ADAS perception lidar sensor fusion for everyone."
    jobs = [make_job(job_id=str(i), description=blurb) for i in range(3)]
    assert all(blurb in text for text in strip_boilerplate(jobs).values())


def test_scan_lists_only_rejected_jobs_with_enough_distinct_strong_terms(tmp_path):
    db = seed(tmp_path, [
        make_job(job_id="a", title="Chef de Cuisine", description="perception lidar sensor fusion ADAS vehicle vehicle"),
        make_job(job_id="b", title="Accountant", description="perception only"),
        make_job(job_id="c", title="Robotics Engineer", description="perception lidar sensor fusion ADAS"),
        make_job(job_id="d", title="Driver", description="vehicle vehicle vehicle driving"),
    ])
    result = scan(db, PROFILE, 30, now=NOW)
    assert [r.job_id for r in result.rows] == ["a"]
    assert result.rows[0].terms == ("ADAS", "perception", "lidar", "sensor fusion")
    assert result.rows[0].score == 4 and result.rows[0].occurrences == 4   # 'vehicle' is generic
    assert [r.job_id for r in scan(db, PROFILE, 30, min_terms=1, now=NOW).rows] == ["a", "b"]
    assert result.pool_size == 3                       # a, b, d rejected; c passed the gate


def test_scan_ignore_terms_are_configurable(tmp_path):
    db = seed(tmp_path, [make_job(job_id="a", description="perception vehicle")])
    # by default 'vehicle' is generic, so only one term counts and min_terms=2 is not met
    assert scan(db, PROFILE, 30, min_terms=2, now=NOW).rows == []
    # an explicit ignore set replaces the generic default: now 'vehicle' counts
    result = scan(db, PROFILE, 30, min_terms=2, ignore_terms=frozenset({"lidar"}), now=NOW)
    assert [r.terms for r in result.rows] == [("perception", "vehicle")]


def test_scan_since_and_limit(tmp_path):
    jobs = [make_job(job_id=str(i), title=f"Role {i}", description="perception lidar ADAS") for i in range(4)]
    stamps = {("acme", str(i)): NOW - timedelta(days=10 - 3 * i) for i in range(4)}
    db = seed(tmp_path, jobs, first_seen=stamps)
    since = NOW - timedelta(days=5)
    assert sorted(r.job_id for r in scan(db, PROFILE, 30, since=since, now=NOW).rows) == ["2", "3"]
    assert len(scan(db, PROFILE, 30, limit=2, now=NOW).rows) == 2


def test_scan_without_strong_terms_returns_no_rows_not_an_error(tmp_path):
    db = seed(tmp_path, [make_job(description="perception lidar ADAS")])
    result = scan(db, CandidateProfile(target_title_terms=["robotics"]), 30, now=NOW)
    assert result.rows == [] and result.pool_size == 1


def test_scan_reports_department_coverage_by_source(tmp_path):
    db = seed(tmp_path, [
        make_job(job_id="1", department="Engineering"),
        make_job(job_id="2", department=None),
        make_job(job_id="3", source_key="bigco", department=None),
        make_job(job_id="4", source_key="bigco", department=""),
    ])
    result = scan(db, PROFILE, 30, now=NOW)
    assert (result.eligible_recent, result.empty_department) == (4, 3)
    assert [(c.source_key, c.total, c.empty_department) for c in result.coverage] == [("bigco", 2, 2), ("acme", 2, 1)]


def test_scan_vocabulary_hints_rank_new_title_terms_by_near_miss_frequency(tmp_path):
    desc = "perception lidar ADAS sensor fusion"
    db = seed(tmp_path, [
        make_job(job_id="1", title="Calibration Specialist", description=desc),
        make_job(job_id="2", title="Camera Calibration Lead", description=desc),
        make_job(job_id="3", title="Pastry Chef", description=desc),
        make_job(job_id="4", title="Pastry Chef II", description=desc),
        make_job(job_id="5", title="Pastry Chef Assistant", description="nothing relevant"),
    ])
    hints = scan(db, PROFILE, 30, now=NOW).hints
    by_term = {h.term: h for h in hints}
    assert by_term["calibration"].near_miss_jobs == 2 and by_term["calibration"].gain == 2
    # in two near-misses; it would also admit the non-near-miss 'Pastry Chef Assistant'
    assert by_term["pastry chef"].near_miss_jobs == 2 and by_term["pastry chef"].gain == 3
    assert "camera calibration" not in by_term       # only one near-miss: below the frequency floor
    assert "robotics" not in by_term                 # already a profile term
    assert hints[0].near_miss_jobs >= hints[-1].near_miss_jobs
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_near_miss.py -q`
Expected: collection error (`No module named 'job_hunter.near_miss'`).

- [ ] **Step 3: Implement** — create `src/job_hunter/near_miss.py`:

```python
"""Periodic near-miss discovery: jobs the positive-term gate rejected (`no_positive_match`) whose
descriptions still contain several of the profile's strong-relevance terms, plus vocabulary hints
and a department-coverage table.

This is a *human-only scouting aid* in the spirit of docs/broad-match-plan.md (which concluded
description similarity is unreliable as a filter but good for keyword discovery). Near-misses are
never LLM-scored, never added to `candidates`, never merged into the radar, and nothing here
changes the gate. Pure functions over `raw_active_jobs()`; report writing lives in Task 6."""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .active_pool import raw_active_jobs
from .config import CandidateProfile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency
from .vocabulary import phrase_gain, title_phrases

#: Strong terms too generic to count toward a near-miss (kept in the profile for the soft-exclude
#: rescue, but "vehicle"/"driving" alone say nothing about a role). Override with --ignore-term.
GENERIC_STRONG_TERMS = frozenset(
    {"vehicle", "behavior", "behaviour", "driving", "chassis", "camera", "radar"}
)


def html_to_text(text: str) -> str:
    text = re.sub(r"(?i)<\s*(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in html_to_text(text).split("\n") if p.strip()]


def strip_boilerplate(
    jobs: list[Job], *, threshold: float = 0.3, min_postings: int = 5, min_chars: int = 40
) -> dict[tuple[str, str], str]:
    """Description text per `(source_key, job_id)` with per-source boilerplate paragraphs removed:
    a paragraph of >= `min_chars` characters recurring in at least `threshold` of that source's
    postings (and at least twice) is company copy, not role content. Sources with fewer than
    `min_postings` jobs are left untouched (too little evidence to call anything boilerplate)."""
    by_source: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_source[job.source_key].append(job)
    cleaned: dict[tuple[str, str], str] = {}
    for source, group in by_source.items():
        paragraphs = {job.job_id: _paragraphs(job.description or "") for job in group}
        counts: Counter[str] = Counter()
        for items in paragraphs.values():
            counts.update({p.lower() for p in items if len(p) >= min_chars})
        boiler: set[str] = set()
        if len(group) >= min_postings:
            cutoff = threshold * len(group)
            boiler = {p for p, n in counts.items() if n >= 2 and n >= cutoff}
        for job in group:
            keep = [p for p in paragraphs[job.job_id] if p.lower() not in boiler]
            cleaned[(source, job.job_id)] = "\n".join(keep)
    return cleaned


def _term_regex(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def matched_terms(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if _term_regex(t).search(low)]


@dataclass(frozen=True)
class NearMiss:
    source_key: str
    job_id: str
    company: str
    title: str
    url: str
    department: str | None
    posted_at: datetime | None
    first_seen_at: datetime
    terms: tuple[str, ...]
    occurrences: int

    @property
    def score(self) -> int:
        return len(self.terms)


@dataclass(frozen=True)
class SourceCoverage:
    source_key: str
    total: int
    empty_department: int


@dataclass(frozen=True)
class VocabularyHint:
    term: str
    near_miss_jobs: int
    gain: int
    samples: tuple[str, ...]


@dataclass
class ScanResult:
    rows: list[NearMiss]
    pool_size: int
    eligible_recent: int
    empty_department: int
    coverage: list[SourceCoverage] = field(default_factory=list)
    hints: list[VocabularyHint] = field(default_factory=list)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _vocabulary_hints(
    rows: list[NearMiss], profile: CandidateProfile, pool: list[Job], *, top: int = 15, min_jobs: int = 2
) -> list[VocabularyHint]:
    frequency: Counter[str] = Counter()
    for row in rows:
        frequency.update(set(title_phrases(row.title)))
    existing = [t.lower() for t in (*profile.target_title_terms, *profile.target_domains)]
    hints: list[VocabularyHint] = []
    for phrase, count in frequency.items():
        if count < min_jobs or any(e in phrase or phrase in e for e in existing):
            continue
        gain = phrase_gain(profile, phrase, pool)
        hints.append(VocabularyHint(phrase, count, gain.count, gain.samples))
    hints.sort(key=lambda h: (-h.near_miss_jobs, h.gain, -len(h.term), h.term))
    return hints[:top]


def scan(
    database_path: Path,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    ignore_terms: frozenset[str] = GENERIC_STRONG_TERMS,
    min_terms: int = 3,
    since: datetime | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> ScanResult:
    eligible = [j for j in raw_active_jobs(database_path) if passes_recency(j, max_age_days, now=now)]
    pool = [j for j in eligible if evaluate_prefilter(j, profile).rule is PrefilterRule.NO_POSITIVE_MATCH]

    ignored = {t.lower() for t in ignore_terms}
    terms = [t for t in profile.strong_relevance_terms if t.lower() not in ignored]
    cleaned = strip_boilerplate(pool)
    rows: list[NearMiss] = []
    for job in pool:
        text = cleaned[(job.source_key, job.job_id)]
        hits = matched_terms(text, terms)
        if len(hits) < min_terms:
            continue
        if since is not None and _aware(job.first_seen_at) <= _aware(since):
            continue
        low = text.lower()
        occurrences = sum(len(_term_regex(t).findall(low)) for t in hits)
        rows.append(
            NearMiss(
                job.source_key, job.job_id, job.company, job.title, job.url, job.department,
                job.posted_at, job.first_seen_at, tuple(hits), occurrences,
            )
        )
    rows.sort(key=lambda r: (-r.score, -r.occurrences, r.source_key, r.title))
    if limit is not None:
        rows = rows[:limit]

    totals: Counter[str] = Counter(j.source_key for j in eligible)
    empties: Counter[str] = Counter(j.source_key for j in eligible if not (j.department or "").strip())
    coverage = sorted(
        (SourceCoverage(k, totals[k], empties[k]) for k in totals if empties[k]),
        key=lambda c: (-c.empty_department, c.source_key),
    )
    return ScanResult(
        rows=rows,
        pool_size=len(pool),
        eligible_recent=len(eligible),
        empty_department=sum(empties.values()),
        coverage=coverage,
        hints=_vocabulary_hints(rows, profile, pool),
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_near_miss.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS. If `test_scan_reports_department_coverage_by_source` or the hints ordering test disagrees with the implementation by a detail (tie-break order, which sources list), fix the **test fixture/expectation only if the spec's behavior is still met** (most-empty source first; hints sorted by near-miss frequency then gain), and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/near_miss.py tests/test_near_miss.py
git commit -m "feat: near-miss scan - strong-term scoring, boilerplate stripping, coverage, vocabulary hints" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 6: Near-miss reports, state, and the `near-misses` command

**Files:**
- Modify: `src/job_hunter/near_miss.py` (append renderers, state helpers, `cli_near_misses`)
- Modify: `src/job_hunter/cli.py` (new subparser before the `--project` loop; dispatch after `settings = load_settings()`)
- Test: `tests/test_near_miss.py` (append) and `tests/test_cli_missed_job.py` (append)

**Interfaces:**
- Consumes (Task 5): `scan`, `ScanResult`, `NearMiss`; `atomic_write_text` (`atomic.py`), `nonneg_int` (`rootutil.py`), `load_profile`.
- Produces: `render_html(result, *, generated_at: datetime, since: datetime | None) -> str`; `render_csv(rows) -> str`; `read_last_scan(state_path: Path) -> datetime | None`; `write_last_scan(state_path: Path, moment: datetime) -> None`; `cli_near_misses(args, settings) -> int`. CLI: `job-hunter near-misses [--min-terms N=3] [--limit N] [--all] [--ignore-term T ...] [--output-dir DIR] [--no-state]`. Default output dir `<db parent>/near-miss/`; files `<local-timestamp>.html`/`.csv` and `state.json` (`{"last_scan_at": <ISO UTC>}`).

Behavior: `since` = `last_scan_at` unless `--all` (then None); with no `--limit`, a run that has no `since` (first run or `--all`) is capped at 100 rows; both files are written atomically; `state.json` is advanced to the scan's `now` only after both files were written and only without `--no-state`; any `OSError` while writing prints `job-hunter: could not write the near-miss report: <error>` to stderr and returns 2 without touching state. Stdout summary: `Near-misses: <N> new job(s) (scanned <pool> rejected of <eligible> eligible)` then the two paths.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_near_miss.py`:

```python
import csv
import io
import json

from job_hunter.near_miss import read_last_scan, render_csv, render_html, write_last_scan


def _result(tmp_path, title="Perception <script>alert(1)</script> & Co"):
    db = seed(tmp_path, [make_job(job_id="a", title=title, description="perception lidar ADAS sensor fusion")])
    return scan(db, PROFILE, 30, now=NOW)


def test_render_html_escapes_titles_and_shows_coverage_and_hints(tmp_path):
    page = render_html(_result(tmp_path), generated_at=NOW, since=None)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&amp; Co" in page
    assert "title is the only gate signal" in page


def test_render_csv_has_a_header_and_one_row_per_near_miss(tmp_path):
    rows = list(csv.reader(io.StringIO(render_csv(_result(tmp_path).rows))))
    assert rows[0] == ["rank", "source", "title", "score", "terms", "posted", "first_seen", "url"]
    assert len(rows) == 2 and rows[1][0] == "1" and rows[1][3] == "4"


def test_state_round_trip_and_missing_or_corrupt_state_means_no_since(tmp_path):
    state = tmp_path / "near-miss" / "state.json"
    assert read_last_scan(state) is None
    write_last_scan(state, NOW)
    assert read_last_scan(state) == NOW
    state.write_text("not json")
    assert read_last_scan(state) is None
```

Append to `tests/test_cli_missed_job.py`:

```python
NEAR_DESC = "perception lidar ADAS sensor fusion"


def _near_miss_project(project):
    with Storage(project / "data" / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="9", source_key="acme", title="Pastry Chef", description=NEAR_DESC))
    return project


def test_near_misses_writes_reports_and_advances_state(project, tmp_path, capsys):
    _near_miss_project(project)
    out = tmp_path / "reports"
    assert main(["near-misses", "--output-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Near-misses: 1 new job(s)" in printed
    assert len(list(out.glob("*.html"))) == 1 and len(list(out.glob("*.csv"))) == 1
    assert "last_scan_at" in json.loads((out / "state.json").read_text())


def test_second_run_lists_only_new_jobs_and_all_lists_everything_again(project, tmp_path, capsys):
    _near_miss_project(project)
    out = tmp_path / "reports"
    main(["near-misses", "--output-dir", str(out)])
    capsys.readouterr()
    assert main(["near-misses", "--output-dir", str(out)]) == 0
    assert "Near-misses: 0 new job(s)" in capsys.readouterr().out
    assert main(["near-misses", "--output-dir", str(out), "--all"]) == 0
    assert "Near-misses: 1 new job(s)" in capsys.readouterr().out


def test_no_state_flag_leaves_state_untouched(project, tmp_path):
    _near_miss_project(project)
    out = tmp_path / "reports"
    assert main(["near-misses", "--output-dir", str(out), "--no-state"]) == 0
    assert not (out / "state.json").exists()


def test_an_unwritable_output_location_exits_2_and_never_advances_state(project, tmp_path, capsys):
    _near_miss_project(project)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    assert main(["near-misses", "--output-dir", str(blocker / "sub")]) == 2
    assert "could not write the near-miss report" in capsys.readouterr().err
    assert not (blocker / "sub").exists()


def test_first_run_is_capped_at_100_rows_without_a_limit(project, tmp_path, capsys):
    with Storage(project / "data" / "jobs.sqlite3") as storage:
        for i in range(120):
            storage.upsert_job(make_job(job_id=f"n{i}", title=f"Pastry Chef {i}", description=NEAR_DESC))
    assert main(["near-misses", "--output-dir", str(tmp_path / "r")]) == 0
    assert "Near-misses: 100 new job(s)" in capsys.readouterr().out


def test_near_misses_project_flag_and_options_parse():
    args = parser().parse_args(["near-misses", "--project", "/z", "--min-terms", "2", "--ignore-term", "lidar", "--all"])
    assert args.project == Path("/z") and args.min_terms == 2 and args.ignore_term == ["lidar"] and args.all
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_near_miss.py tests/test_cli_missed_job.py -q -k "render or state or near_misses or capped or unwritable"`
Expected: FAIL (`cannot import name 'render_html'` / invalid choice `near-misses`).

- [ ] **Step 3: Implement** — append to `src/job_hunter/near_miss.py` (add `import csv, io, json, sys` and `from .atomic import atomic_write_text`, `from .config import load_profile` to the imports):

```python
def _fmt_day(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d") if moment else ""


def render_csv(rows: list[NearMiss]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["rank", "source", "title", "score", "terms", "posted", "first_seen", "url"])
    for rank, row in enumerate(rows, 1):
        writer.writerow(
            [rank, row.source_key, row.title, row.score, "; ".join(row.terms),
             _fmt_day(row.posted_at), _fmt_day(row.first_seen_at), row.url]
        )
    return buffer.getvalue()


def render_html(result: ScanResult, *, generated_at: datetime, since: datetime | None) -> str:
    e = html.escape
    scope = f"new since {_fmt_day(since)}" if since else "all (no previous scan)"
    empty_pct = (100 * result.empty_department // result.eligible_recent) if result.eligible_recent else 0
    parts = [
        "<!DOCTYPE html><meta charset='utf-8'><title>Near-miss report</title>",
        "<style>body{font:14px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse;width:100%}"
        "td,th{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}"
        "code{background:#f3f3f3;padding:1px 4px}h2{margin-top:2rem}</style>",
        f"<h1>Near-miss report</h1><p>{e(generated_at.astimezone().strftime('%Y-%m-%d %H:%M'))} - "
        f"{len(result.rows)} job(s), {e(scope)}. Scanned {result.pool_size} rejected jobs of "
        f"{result.eligible_recent} eligible and recent. These jobs failed the title/department gate "
        "but mention several strong-relevance terms in their descriptions. They are <b>not</b> "
        "reviewed, scored, or in the radar.</p>",
        "<h2>Near-misses</h2><table><tr><th>#</th><th>Source</th><th>Title</th><th>Score</th>"
        "<th>Terms</th><th>Posted</th><th>Link</th></tr>",
    ]
    for rank, row in enumerate(result.rows, 1):
        parts.append(
            f"<tr><td>{rank}</td><td>{e(row.source_key)}</td><td>{e(row.title)}</td><td>{row.score}</td>"
            f"<td>{e(', '.join(row.terms))}</td><td>{e(_fmt_day(row.posted_at))}</td>"
            f"<td><a href='{e(row.url, quote=True)}'>open</a></td></tr>"
        )
    parts.append("</table><h2>Vocabulary hints</h2>")
    if result.hints:
        parts.append("<table><tr><th>Title term</th><th>In near-misses</th><th>Would admit</th><th>Examples</th><th>Preview</th></tr>")
        for hint in result.hints:
            preview = f'uv run python scripts/diff_profile.py --add "target_title_terms:{hint.term}"'
            parts.append(
                f"<tr><td>{e(hint.term)}</td><td>{hint.near_miss_jobs}</td><td>{hint.gain} job(s)</td>"
                f"<td>{e('; '.join(hint.samples))}</td><td><code>{e(preview)}</code></td></tr>"
            )
        parts.append("</table><p>Add terms only through the job-feedback skill (preview, then confirm).</p>")
    else:
        parts.append("<p>No new title vocabulary stood out.</p>")
    parts.append(
        f"<h2>Department coverage</h2><p>{result.empty_department} of {result.eligible_recent} eligible jobs "
        f"({empty_pct}%) have no department - <b>title is the only gate signal</b> for them. Top sources:</p>"
        "<table><tr><th>Source</th><th>Jobs without a department</th><th>of total</th></tr>"
    )
    for item in result.coverage[:10]:
        parts.append(f"<tr><td>{e(item.source_key)}</td><td>{item.empty_department}</td><td>{item.total}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def read_last_scan(state_path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(state_path.read_text(encoding="utf-8"))["last_scan_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_last_scan(state_path: Path, moment: datetime) -> None:
    atomic_write_text(state_path, json.dumps({"last_scan_at": moment.isoformat()}) + "\n")


def cli_near_misses(args, settings) -> int:
    out_dir = Path(args.output_dir) if args.output_dir else settings.database_path.parent / "near-miss"
    state_path = out_dir / "state.json"
    since = None if args.all else read_last_scan(state_path)
    limit = args.limit if args.limit is not None else (100 if since is None else None)
    now = datetime.now(UTC)
    result = scan(
        settings.database_path,
        load_profile(),
        settings.search.max_posting_age_days,
        ignore_terms=GENERIC_STRONG_TERMS | frozenset(t.lower() for t in args.ignore_term),
        min_terms=args.min_terms,
        since=since,
        limit=limit,
        now=now,
    )
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-T-%H-%M-%S")
    html_path, csv_path = out_dir / f"{stamp}.html", out_dir / f"{stamp}.csv"
    try:
        atomic_write_text(html_path, render_html(result, generated_at=now, since=since))
        atomic_write_text(csv_path, render_csv(result.rows))
        if not args.no_state:
            write_last_scan(state_path, now)
    except OSError as exc:
        print(f"job-hunter: could not write the near-miss report: {exc}", file=sys.stderr)
        return 2
    print(
        f"Near-misses: {len(result.rows)} new job(s) (scanned {result.pool_size} rejected of "
        f"{result.eligible_recent} eligible)"
    )
    print(f"  report: {html_path}")
    print(f"  csv:    {csv_path}")
    return 0
```

In `cli.py`, before the `--project` loop add:

```python
    near_misses = sub.add_parser(
        "near-misses",
        help="report rejected jobs that look relevant from their description, plus vocabulary hints",
    )
    near_misses.add_argument("--min-terms", type=nonneg_int, default=3, help="distinct strong terms required (default 3)")
    near_misses.add_argument("--limit", type=nonneg_int, default=None, help="cap rows (default 100 when listing everything)")
    near_misses.add_argument("--all", action="store_true", help="list all, not only jobs first seen since the last scan")
    near_misses.add_argument("--ignore-term", action="append", default=[], help="strong term to ignore (repeatable; defaults to a generic set)")
    near_misses.add_argument("--output-dir", type=Path, default=None, help="where to write the report (default data/near-miss/)")
    near_misses.add_argument("--no-state", action="store_true", help="do not advance the last-scan marker")
```

and after the `why-missed` dispatch in `main()`:

```python
        if args.command == "near-misses":
            from .near_miss import cli_near_misses

            return cli_near_misses(args, settings)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_near_miss.py tests/test_cli_missed_job.py -q && uv run pytest -q -m "not live" && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/near_miss.py src/job_hunter/cli.py tests/test_near_miss.py tests/test_cli_missed_job.py
git commit -m "feat: job-hunter near-misses command with HTML/CSV reports and last-scan state" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 7: Apply the vocabulary with the user (controller-run, human-gated)

**Files:** `config/candidate_profile.yaml` (personal data: **never commit it**); no source changes.

This task is run by the controller **with the user**, not delegated: it edits the user's personal profile and must stop for confirmation twice (the same two stop points `skills/job-feedback` mandates).

- [ ] **Step 1: Preview the candidate terms (writes nothing to the profile)**

Run: `uv run python scripts/diff_profile.py --add "target_title_terms:calibration" --add "target_title_terms:test supervisor"`
Expected: a terminal summary and an HTML report path under `data/profile-diff/`; per CLAUDE.md memory, publish that report as an artifact for the user.

- [ ] **Step 2: Show the user the gained jobs per term** (from `why-missed`/`diff_profile`): `calibration` (about 25 jobs, including camera calibration and several powertrain/OBD calibration roles) and `test supervisor` (about 2). Ask which terms to apply. **Stop and wait for the user's answer. Do not infer from silence.**

- [ ] **Step 3: Apply only the confirmed terms** by following `skills/job-feedback/SKILL.md` steps 4-7 (write to `config/candidate_profile.yaml`, re-check with `diff_profile.py`, then **stop and ask again** before `--accept-baseline`).

- [ ] **Step 4: Re-run the original case**

Run: `uv run job-hunter why-missed ford:71202`
Expected after applying `test supervisor`: the `prefilter` stage is `ok  ` and the verdict moves to the next stage (not in the archive until the next refilter or pipeline run).

- [ ] **Step 5: Do not commit the profile.** `config/candidate_profile.yaml` stays uncommitted by design.

---

## Task 8: Docs, skills, full verification, live smoke

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/SPEC.md`, `skills/job-feedback/SKILL.md`, `skills/job-scout/SKILL.md`

- [ ] **Step 1: README.md** — in the commands section (`grep -n "^## Commands" README.md`), add:

```bash
uv run job-hunter why-missed ford:71202     # why wasn't this job in the radar? stage-by-stage, plus title terms that would admit it
uv run job-hunter near-misses               # report rejected jobs that look relevant from their description + vocabulary hints
```

with two prose lines: `why-missed` takes a job id, `source_key:job_id`, a URL (matched by exact URL or ids in its path) or part of the title and changes nothing; `near-misses` writes `data/near-miss/<timestamp>.html`/`.csv` listing only jobs first seen since the last scan (`--all` for everything) and is a scouting aid, never scored or added to the radar.

- [ ] **Step 2: CLAUDE.md** — add the two commands to the `## Commands` block (same two lines as above, matching the existing comment alignment) and, immediately after the `prefilter.py` bullet group (before the `scripts/diff_profile.py` bullet), add two bullets in the surrounding density: `src/job_hunter/why_missed.py` (+ `vocabulary.py`, `active_pool.find_jobs`): deterministic read-only explanation of a stored job's journey, with title-term suggestions whose gain is computed by the real `evaluate_prefilter` on a widened profile copy, never editing the profile; and `src/job_hunter/near_miss.py`: periodic human-only report of `no_positive_match` rejects whose boilerplate-stripped descriptions carry >= 3 distinct strong terms, with vocabulary hints and a department-coverage table, state in `data/near-miss/state.json`, never scored/merged/added to `candidates`. Add one sentence of lesson: positive matching is a substring test on title+department, so `vehicle test` does not match "Vehicle Calibration & Test", and 61% of eligible jobs have no department, so the title is often the only signal.

- [ ] **Step 3: docs/SPEC.md** — insert `### 7.5 Missed-job diagnostics and near-miss discovery` before `## 8. Persistence` (`grep -n "^## 8. Persistence" docs/SPEC.md`) covering: the Ford finding (collected, eligible, recent, rejected `no_positive_match`; exact-phrase matching; empty department; description matching removed on purpose), the measurements (13,948 pool; 733 pass; 12,160 rejected; 61% empty department, 42 sources 100% empty; Ford's feed fills no department-like field — a verified dead end), the two commands and their semantics (resolution order, stages, suggestion ranking and the "broad" flag, near-miss scoring/boilerplate/state/first-run cap), and the explicit non-goals (no gate change, no LLM, no profile writes).

- [ ] **Step 4: skills** — `grep -n "^version:" skills/job-feedback/SKILL.md skills/job-scout/SKILL.md` (both `1.2.0`). In `skills/job-feedback/SKILL.md` add a short section "When the user reports a missed job": run `uv run job-hunter why-missed "<id | source:id | title>" --project "$CLAUDE_PROJECT_DIR"`, relay the failing stage, and if it is a gate rejection present the suggested terms with their gains and continue with this skill's existing steps (preview, confirm, write, confirm baseline); never write the profile from the suggestions without the existing confirmations. Bump to `1.3.0`. In `skills/job-scout/SKILL.md` add a short optional section "Spotting vocabulary gaps": after a scrape, `uv run job-hunter near-misses --project "$CLAUDE_PROJECT_DIR"` lists rejected-but-relevant-looking jobs new since the last scan; it is a human scouting aid (not scored, not in the radar) and its vocabulary hints feed the job-feedback flow. Bump to `1.3.0`. Wording stays generic: no employer names, no personal data.

- [ ] **Step 5: Full verification**

Run: `uv run pytest -q -m "not live" && uv run ruff check . && uv run pytest tests/test_skills_portable.py tests/test_no_owner_pii_in_shareable_files.py -q`
Expected: all pass.

- [ ] **Step 6: Live smoke on the real DB (read-only; never write the real data/)**

```bash
uv run job-hunter why-missed ford:71202
uv run job-hunter why-missed "https://www.careers.ford.com/job/-/-/48560/101370456832"; echo "exit=$?"
uv run job-hunter near-misses --output-dir "$(mktemp -d)" --no-state --limit 20
```

Expected: the first prints a `prefilter` FAIL (`no_positive_match`, department empty) — or `ok` if Task 7 already applied `test supervisor` — and, when rejected, suggestions including `test supervisor` and `calibration` with plausible gains (2 and about 25); the second prints the "no stored job matches" help and `exit=2`; the third prints a near-miss count and two temp-dir paths. Open the HTML once and confirm it renders and the coverage table lists the large empty-department sources (Apple, Ford, Magna, ...). Confirm `git status --short` shows no change under `data/` or `config/`.

- [ ] **Step 7: Commit**

```bash
git add README.md CLAUDE.md docs/SPEC.md skills/job-feedback/SKILL.md skills/job-scout/SKILL.md
git commit -m "docs: missed-job diagnostics and near-miss discovery" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review (completed)

**Spec coverage:** §4 vocabulary → Task 7; §5 `why-missed` (resolution, stages, suggestions, placement) → Tasks 1, 3, 4; §6 `near-misses` (pool, score, boilerplate, state, output, coverage, hints) → Tasks 5, 6; §7 error handling → Tasks 4 and 6 (not-found/ambiguous exit 2, unwritable output exit 2, state only after success); §8 testing → each task is test-first and includes the Ford regression and parser tests; §9 docs/skills → Task 8. Adjustments are listed at the top (URL limit, read-only wording, `field:term` syntax, `StoredJob`, human-gated Task 7).

**Placeholder scan:** no TBD/TODO; every code step contains the code. Test expectations were traced by hand against the implementations during self-review (which corrected the `min_terms`/ignore-terms fixture, the vocabulary-hints frequency floor, and several leftover notes).

**Type consistency:** `StoredJob(job, status, missing_count)`, `find_jobs(database_path, ref, *, limit)`, `title_phrases`, `rejected_pool`, `PhraseGain(count, samples)`, `phrase_gain(profile, phrase, pool, *, sample_size)`, `ArchiveInfo(path, in_candidates, source_status)`, `Stage`, `TermSuggestion`, `WhyMissed.preview_commands`, `explain(..., pool: Callable[[], list[Job]], ...)`, `NearMiss.terms/occurrences/score`, `ScanResult(rows, pool_size, eligible_recent, empty_department, coverage, hints)` are defined once and used consistently in later tasks.
