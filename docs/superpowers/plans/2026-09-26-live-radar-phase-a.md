# Live Radar — Phase A (live feedback + filters) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An opt-in localhost server (`scripts/serve_radar.py`) that serves the radar report with click-to-save feedback in SQLite, live-only filters/sort, stale-write protection, and change polling — without changing the static `file://` report.

**Architecture:** `render_radar.py` gains a pure `render()` (the existing `build()` becomes a thin file-writing wrapper) plus a `live=True` mode that adds identity/filter attributes, a live toolbar and two inlined JS files. Feedback is last-writer-wins by *event time* in `Storage` (guarded write + tombstones); the stdlib threaded HTTP server validates writes against the `jobs` table and never renders on the write path. Pure client logic lives in `radar_live_core.js` (unit-tested with `node --test`); DOM wiring lives in `radar_live_ui.js`.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `http.server`/`sqlite3`, vanilla JS, pytest, node (optional, tests skip without it).

**Spec:** `docs/live-radar-dashboard-plan.md` (revision 2 — read §2 "Audited repository contracts", §3.4 stale-write rule, §4, §5 first). This plan implements its **Phase A** only. Phase B (applications) is a separate plan.

**Before starting:** commit `docs/live-radar-dashboard-plan.md` and this file on `dev`, then work in a git worktree off `dev` (`superpowers:using-git-worktrees`) so they are present in it.

## Global Constraints

- Python `>=3.11`; **no new runtime dependency** (`pyproject.toml` untouched). `ruff` line-length 100, `E501` ignored; `uv run ruff check .` must stay clean (baseline: clean).
- Baseline test status before this work: **467 passed, 2 failed** (`tests/test_collector.py::test_no_default_cap_without_keywords`, `::test_keyword_search_overrides_profile_terms` — pre-existing, unrelated). Report these separately; never "fix" them here.
- **Static report output must stay byte-identical** for the same inputs (`render_radar.py` with `live=False`). All new toolbar controls, CSS and scripts are emitted only when `live=True`. Existing 24 tests in `tests/test_render_radar.py` must pass **unmodified**.
- **Never put company or title into row-root `data-*` attributes** (documented constraint in `_filter_data_attrs`: tests locate rows via `html.index(title)`). Company/title/location filtering reads `.job-company`/`.job-title`/`.job-location` text.
- Client-supplied `company`/`title`/`department`/`score` is never persisted; the server derives them from the `jobs`/`assessments` tables. Request models use `extra="forbid"`.
- **Never write to the real `data/`** (archives, radar HTML, DB, exports) from tests or smoke runs: use `tmp_path` / a temp project (`CLAUDE.md` "Safe testing").
- Live mode is opt-in: `job-hunter`'s other commands, `render_radar.py`'s CLI output, and `apply_radar_feedback.py`'s existing counts/output must not change (the `stale-skipped` count appears only when nonzero).
- `Storage` migrations are append-only: add `_migrate_v3_...` at the end of `_MIGRATIONS`; never renumber.
- Any changed `skills/*/SKILL.md` must bump its frontmatter `version` (minor for new capability).
- End every commit message with the line `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (use a second `-m`).
- Tests run with `uv run pytest`; none may touch the network.

## Review Focus

Failure modes the spec implies that a user is most likely to hit (each is pinned by a test in the named task):

1. **Old `~/Downloads` export re-imported after a live relabel/untag** must not overwrite or resurrect anything — Tasks 1, 2.
2. **Late outbox retry from a stale tab** must not overwrite a newer edit made elsewhere — Task 1 (`apply_feedback`), Task 6 (`stale: true` reply), Task 3 (outbox coalescing).
3. **Hostile job text** (`</script>`, quotes, `&`) in titles, stems and labels must not break out of attributes or the inlined JSON — Task 5.
4. **Server up before any archive exists / archive replaced mid-session / job deleted by `cleanup` while a tab is open** — Task 6 (503 with message; version change; 404 → permanent notice).
5. **Server restarted or unreachable while the page is open** must show "Offline (N unsaved)" and retry, never drop a write silently — Task 3 (`classifyStatus`, `backoffMs`), Task 7 manual smoke.

## File Structure

| File | Responsibility |
|---|---|
| `src/job_hunter/models.py` (modify) | `FeedbackLabel` literal; `JobFeedback.label` typed with it. |
| `src/job_hunter/storage.py` (modify) | v3 migration (`feedback_tombstones`), guarded `apply_feedback`/`delete_feedback`, read helpers (`get_job_feedback`, `feedback_map`, `get_job_snapshot`, `get_assessment_score`). |
| `scripts/apply_radar_feedback.py` (modify) | Importer uses the guarded write with the export file's mtime as event time. |
| `scripts/templates/radar_live_core.js` (create) | Pure, DOM-free logic (filter predicate, sort, hash codec, outbox coalescing, status classification, reconcile). |
| `scripts/templates/radar_live_ui.js` (create) | DOM wiring: feedback clicks, outbox flush, filters, sort, hash, polling, group state. |
| `tests/js/radar_live_core.test.js` (create), `tests/test_radar_live_js.py` (create) | `node --test` suite + pytest wrapper (skips without node). |
| `scripts/render_radar.py` (modify) | `render()`/`build()` split, `LiveState`, live row attributes, live toolbar/CSS/script assembly, shared selection args + kwargs helpers. |
| `scripts/templates/radar_template.html` (modify) | Three inline placeholders only (`__LIVE_STYLE__`, `__LIVE_TOOLBAR__`, `__LIVE_SCRIPT__`) that render to `""` in static mode. |
| `scripts/serve_radar.py` (create) | Threaded server, request checks, routes, `write_feedback`, startup report. |
| `tests/test_storage.py`, `tests/test_apply_radar_feedback.py`, `tests/test_render_radar.py`, `tests/test_serve_radar.py` (modify/create), `tests/fixtures/radar_static_golden.html` (create) | Tests. |
| `docs/SPEC.md`, `README.md`, `CLAUDE.md`, `skills/job-radar/SKILL.md`, `skills/job-feedback/SKILL.md`, `docs/live-radar-dashboard-plan.md` (modify) | Docs + skill version bumps. |

---

### Task 1: Feedback model, tombstone migration, guarded storage writes

**Files:**
- Modify: `src/job_hunter/models.py:139-153` (`JobFeedback`), imports at top
- Modify: `src/job_hunter/storage.py` (add migration after `_migrate_v2_add_salary_evidence_column`; append to `_MIGRATIONS`; add methods after `export_job_feedback`)
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: existing `Storage.connection`, `JobFeedback`, `export_job_feedback()`.
- Produces (later tasks rely on these exact names):
  - `models.FeedbackLabel = Literal["relevant", "okay", "irrelevant"]`
  - `Storage.get_job_feedback(source_key: str, job_id: str) -> dict[str, Any] | None`
  - `Storage.feedback_map() -> dict[str, dict[str, Any]]` — keys `"source_key|job_id"`, values are `job_feedback` rows.
  - `Storage.apply_feedback(feedback: JobFeedback, *, event_at: datetime, force: bool = False) -> Literal["applied", "stale"]`
  - `Storage.delete_feedback(source_key: str, job_id: str, *, event_at: datetime) -> Literal["applied", "stale"]`
  - `Storage.get_job_snapshot(source_key: str, job_id: str) -> dict[str, Any] | None` — keys `source_key, job_id, company, title, department, url, location_raw, posted_at, salary_evidence, status` (never `description`).
  - `Storage.get_assessment_score(source_key: str, job_id: str) -> int | None`
  - Rule: a write applies only if `event_at` is **strictly newer** than both the stored row's `recorded_at` and any tombstone's `deleted_at`; `event_at` must be timezone-aware.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py` (add `import pytest` and `from pydantic import ValidationError` to the imports at the top):

```python
# --- live-radar feedback: tombstones + event-time (last-writer-wins) writes ---


def _t(minutes: int) -> datetime:
    return datetime(2026, 9, 26, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


def test_feedback_label_is_restricted_to_the_three_known_values():
    with pytest.raises(ValidationError):
        make_feedback(label="maybe")


def test_pre_v3_database_gains_the_tombstone_table_on_open(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.connection.execute("DROP TABLE feedback_tombstones")
        storage.connection.execute("PRAGMA user_version = 2")
        storage.connection.commit()
    with Storage(db_path) as storage:
        count = storage.connection.execute("SELECT COUNT(*) FROM feedback_tombstones").fetchone()[0]
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
    assert count == 0
    assert version == len(storage_module._MIGRATIONS)


def test_apply_feedback_requires_a_strictly_newer_event(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.apply_feedback(make_feedback(label="okay"), event_at=_t(0)) == "applied"
        # Equal timestamp: rejected (strictly newer required).
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(0)) == "stale"
        # Older: rejected.
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(-5)) == "stale"
        row = storage.get_job_feedback("apple", "99")
        assert row["label"] == "okay"
        assert row["recorded_at"] == _t(0).isoformat()
        # Newer: applied, recorded_at is the event time (not "now").
        assert storage.apply_feedback(make_feedback(label="irrelevant"), event_at=_t(5)) == "applied"
        row = storage.get_job_feedback("apple", "99")
        assert row["label"] == "irrelevant"
        assert row["recorded_at"] == _t(5).isoformat()


def test_apply_feedback_rejects_a_naive_event_time(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage, pytest.raises(ValueError):
        storage.apply_feedback(make_feedback(), event_at=datetime(2026, 9, 26, 12, 0))


def test_apply_feedback_force_bypasses_the_staleness_check(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(label="okay"), event_at=_t(10))
        assert (
            storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(0), force=True)
            == "applied"
        )
        assert storage.get_job_feedback("apple", "99")["label"] == "relevant"


def test_delete_feedback_leaves_a_tombstone_that_blocks_older_labels(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(label="okay"), event_at=_t(0))
        assert storage.delete_feedback("apple", "99", event_at=_t(10)) == "applied"
        assert storage.get_job_feedback("apple", "99") is None
        # An older label (e.g. a stale export) must not resurrect the job.
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(5)) == "stale"
        assert storage.get_job_feedback("apple", "99") is None
        # A newer label clears the tombstone and lands.
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(20)) == "applied"
        assert storage.get_job_feedback("apple", "99")["label"] == "relevant"
        tombstones = storage.connection.execute("SELECT * FROM feedback_tombstones").fetchall()
        assert tombstones == []


def test_delete_feedback_is_itself_subject_to_the_staleness_check(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(label="okay"), event_at=_t(10))
        assert storage.delete_feedback("apple", "99", event_at=_t(0)) == "stale"
        assert storage.get_job_feedback("apple", "99")["label"] == "okay"


def test_delete_feedback_with_no_prior_row_still_records_a_tombstone(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.delete_feedback("apple", "99", event_at=_t(10)) == "applied"
        assert storage.apply_feedback(make_feedback(label="okay"), event_at=_t(0)) == "stale"


def test_feedback_map_is_keyed_by_source_and_job(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(source_key="a", job_id="1"), event_at=_t(0))
        storage.apply_feedback(make_feedback(source_key="b", job_id="2", label="okay"), event_at=_t(0))
        mapping = storage.feedback_map()
    assert set(mapping) == {"a|1", "b|2"}
    assert mapping["b|2"]["label"] == "okay"


def test_get_job_snapshot_and_assessment_score_never_expose_the_description(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_job_snapshot("acme", "42") is None
        assert storage.get_assessment_score("acme", "42") is None
        storage.upsert_job(make_job(description="secret body", posted_at=None))
        storage.upsert_assessment(make_assessment(score=77))
        snapshot = storage.get_job_snapshot("acme", "42")
        assert snapshot["company"] == "Acme"
        assert snapshot["url"] == "https://example.com/42"
        assert snapshot["status"] == "active"
        assert "description" not in snapshot
        assert storage.get_assessment_score("acme", "42") == 77


def test_delete_closed_jobs_leaves_feedback_tombstones_alone(tmp_path):
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()
        storage.delete_feedback("acme", "stale", event_at=_t(0))
        storage.delete_closed_jobs(now - timedelta(days=7))
        remaining = storage.connection.execute(
            "SELECT job_id FROM feedback_tombstones"
        ).fetchall()
    assert [row["job_id"] for row in remaining] == ["stale"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -q`
Expected: the new tests FAIL (`AttributeError: 'Storage' object has no attribute 'apply_feedback'`, no `feedback_tombstones` table, and `make_feedback(label="maybe")` does not raise). Existing tests still pass.

- [ ] **Step 3: Implement**

In `src/job_hunter/models.py`, change the typing import to `from typing import Any, Literal`, then directly above `class JobFeedback`:

```python
FeedbackLabel = Literal["relevant", "okay", "irrelevant"]
```

and change the field `label: str` to `label: FeedbackLabel`.

In `src/job_hunter/storage.py`, add `Literal` to the typing import (`from typing import Any, Literal`), then after `_migrate_v2_add_salary_evidence_column`:

```python
def _migrate_v3_create_feedback_tombstones(connection: sqlite3.Connection) -> None:
    """`feedback_tombstones`: one row per (source_key, job_id) whose feedback was deleted
    ("untagged"), stamped with the deletion's event time. Lets an older write (a stale outbox
    retry, or an old `radar-feedback-*.json` export) be recognized as older than the untag
    instead of silently resurrecting the label. Retained forever; a row is cleared only by a
    strictly newer label."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS feedback_tombstones (
            source_key TEXT NOT NULL, job_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
            PRIMARY KEY (source_key, job_id)
        )"""
    )
```

Append `_migrate_v3_create_feedback_tombstones,` to the `_MIGRATIONS` list. Add a module-level helper next to the migrations:

```python
def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
```

Add these methods to `Storage`, directly after `export_job_feedback`:

```python
    def get_job_feedback(self, source_key: str, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM job_feedback WHERE source_key=? AND job_id=?", (source_key, job_id)
        ).fetchone()
        return dict(row) if row else None

    def feedback_map(self) -> dict[str, dict[str, Any]]:
        """Every feedback row keyed by "source_key|job_id" — the shape the live radar page and
        `/api/feedback` use."""
        return {f"{row['source_key']}|{row['job_id']}": row for row in self.export_job_feedback()}

    def _latest_feedback_event(self, source_key: str, job_id: str) -> datetime | None:
        """Newest of the stored label's `recorded_at` and any tombstone's `deleted_at`."""
        times: list[datetime] = []
        row = self.connection.execute(
            "SELECT recorded_at FROM job_feedback WHERE source_key=? AND job_id=?",
            (source_key, job_id),
        ).fetchone()
        if row:
            times.append(_parse_utc(row["recorded_at"]))
        tomb = self.connection.execute(
            "SELECT deleted_at FROM feedback_tombstones WHERE source_key=? AND job_id=?",
            (source_key, job_id),
        ).fetchone()
        if tomb:
            times.append(_parse_utc(tomb["deleted_at"]))
        return max(times) if times else None

    def apply_feedback(
        self, feedback: JobFeedback, *, event_at: datetime, force: bool = False
    ) -> Literal["applied", "stale"]:
        """Last-writer-wins by *event time*, for every writer (live click, outbox retry, static
        import): applied only if `event_at` is strictly newer than the stored label and any
        tombstone (docs/live-radar-dashboard-plan.md section 3.4). The row's `recorded_at`
        becomes `event_at`, not the wall clock, and a newer label clears the tombstone. One
        `BEGIN IMMEDIATE` transaction so a concurrent writer can't slip between check and write.
        `force=True` skips the check (legacy callers with no event time)."""
        if event_at.tzinfo is None:
            raise ValueError("event_at must be timezone-aware")
        event_at = event_at.astimezone(UTC)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            latest = self._latest_feedback_event(feedback.source_key, feedback.job_id)
            if not force and latest is not None and event_at <= latest:
                self.connection.rollback()
                return "stale"
            self.connection.execute(
                """INSERT INTO job_feedback(source_key, job_id, company, title, department, score,
                   label, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_key, job_id) DO UPDATE SET company=excluded.company,
                   title=excluded.title, department=excluded.department, score=excluded.score,
                   label=excluded.label, recorded_at=excluded.recorded_at""",
                (
                    feedback.source_key, feedback.job_id, feedback.company, feedback.title,
                    feedback.department, feedback.score, feedback.label, event_at.isoformat(),
                ),
            )
            self.connection.execute(
                "DELETE FROM feedback_tombstones WHERE source_key=? AND job_id=?",
                (feedback.source_key, feedback.job_id),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return "applied"

    def delete_feedback(
        self, source_key: str, job_id: str, *, event_at: datetime
    ) -> Literal["applied", "stale"]:
        """Untag: remove the label and record a tombstone at `event_at`, under the same
        strictly-newer rule as `apply_feedback`. Records the tombstone even when no label row
        exists, so an older import still can't create one afterward."""
        if event_at.tzinfo is None:
            raise ValueError("event_at must be timezone-aware")
        event_at = event_at.astimezone(UTC)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            latest = self._latest_feedback_event(source_key, job_id)
            if latest is not None and event_at <= latest:
                self.connection.rollback()
                return "stale"
            self.connection.execute(
                "DELETE FROM job_feedback WHERE source_key=? AND job_id=?", (source_key, job_id)
            )
            self.connection.execute(
                """INSERT INTO feedback_tombstones(source_key, job_id, deleted_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source_key, job_id) DO UPDATE SET deleted_at=excluded.deleted_at""",
                (source_key, job_id, event_at.isoformat()),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return "applied"

    def get_job_snapshot(self, source_key: str, job_id: str) -> dict[str, Any] | None:
        """The few `jobs` columns a feedback write needs, by primary key — deliberately never
        `description` (the table is ~98% description text)."""
        row = self.connection.execute(
            """SELECT source_key, job_id, company, title, department, canonical_url AS url,
               location_raw, posted_at, salary_evidence, status
               FROM jobs WHERE source_key=? AND job_id=?""",
            (source_key, job_id),
        ).fetchone()
        return dict(row) if row else None

    def get_assessment_score(self, source_key: str, job_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT score FROM assessments WHERE source_key=? AND job_id=?", (source_key, job_id)
        ).fetchone()
        return int(row["score"]) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage.py tests/test_apply_radar_feedback.py tests/test_cleanup.py -q && uv run ruff check src tests`
Expected: all PASS; ruff clean. (`test_fresh_database_ends_up_at_the_current_schema_version` uses `len(_MIGRATIONS)`, so it adapts.)

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/storage.py tests/test_storage.py
git commit -m "feat(storage): event-time guarded feedback writes with tombstones" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Importer uses the guarded write with the export's mtime

**Files:**
- Modify: `scripts/apply_radar_feedback.py`
- Test: `tests/test_apply_radar_feedback.py`

**Interfaces:**
- Consumes: `Storage.apply_feedback`, `Storage.export_job_feedback` (Task 1); `models.FeedbackLabel`.
- Produces:
  - `apply_radar_feedback.export_event_time(path: Path) -> datetime` (UTC-aware mtime).
  - `ingest(storage, payload, *, event_at: datetime | None = None) -> dict[str, int]` — counts keep `new/changed/unchanged/invalid`; a `"stale"` key is added **only when nonzero**. `event_at=None` keeps the legacy always-apply behavior (`force=True`, stamped now).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_apply_radar_feedback.py` (add `from datetime import UTC, datetime, timedelta` and `from job_hunter.models import JobFeedback` to the imports; extend the `from apply_radar_feedback import` line with `export_event_time`):

```python
def _t(minutes: int) -> datetime:
    return datetime(2026, 9, 26, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


def _live(label="irrelevant", **updates):
    values = dict(
        source_key="apple", job_id="99", company="Apple", title="Some Role",
        department=None, score=68, label=label,
    )
    values.update(updates)
    return JobFeedback(**values)


def test_stale_export_cannot_overwrite_a_newer_live_relabel(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(_live("irrelevant"), event_at=_t(30))
        counts = ingest(storage, [_entry(label="relevant")], event_at=_t(0))
        assert counts == {"new": 0, "changed": 0, "unchanged": 0, "invalid": 0, "stale": 1}
        assert storage.get_job_feedback("apple", "99")["label"] == "irrelevant"


def test_stale_export_cannot_resurrect_an_untagged_job(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.delete_feedback("apple", "99", event_at=_t(30))
        counts = ingest(storage, [_entry(label="okay")], event_at=_t(0))
        assert counts["stale"] == 1 and counts["new"] == 0
        assert storage.get_job_feedback("apple", "99") is None


def test_newer_export_wins_and_clears_a_tombstone(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.delete_feedback("apple", "99", event_at=_t(0))
        counts = ingest(storage, [_entry(label="okay")], event_at=_t(30))
        assert counts == {"new": 1, "changed": 0, "unchanged": 0, "invalid": 0}
        assert storage.get_job_feedback("apple", "99")["label"] == "okay"


def test_older_reimport_of_the_same_label_counts_as_unchanged_not_stale(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(_live("okay"), event_at=_t(30))
        counts = ingest(storage, [_entry(label="okay")], event_at=_t(0))
        assert counts == {"new": 0, "changed": 0, "unchanged": 1, "invalid": 0}


def test_imported_rows_are_stamped_with_the_export_event_time(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        ingest(storage, [_entry(job_id="3", label="okay")], event_at=_t(7))
        assert storage.get_job_feedback("apple", "3")["recorded_at"] == _t(7).isoformat()


def test_export_event_time_is_the_files_mtime_in_utc(tmp_path):
    path = tmp_path / "radar-feedback-x.json"
    path.write_text("[]")
    event_time = export_event_time(path)
    assert event_time.tzinfo is not None
    assert abs(event_time.timestamp() - path.stat().st_mtime) < 1e-3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_apply_radar_feedback.py -q`
Expected: FAIL — `ImportError: cannot import name 'export_event_time'` (collection error) / `TypeError: ingest() got an unexpected keyword argument 'event_at'`.

- [ ] **Step 3: Implement**

In `scripts/apply_radar_feedback.py`:

1. Imports: `from datetime import UTC, datetime`, `from typing import Any, get_args`, `from job_hunter.models import FeedbackLabel, JobFeedback`.
2. Replace `_VALID_LABELS = {"relevant", "okay", "irrelevant"}` with `_VALID_LABELS = set(get_args(FeedbackLabel))`.
3. Add above `ingest`:

```python
def export_event_time(path: Path) -> datetime:
    """The export file's mtime as a UTC-aware event time. The static export's entries carry no
    timestamp of their own, so this is the best available "when did this person last click"
    bound: an export can never contain a click newer than the moment it was written. Used so an
    old ~/Downloads file (auto-picked when no --file is given) cannot overwrite a newer live
    relabel or resurrect an untagged job — see docs/live-radar-dashboard-plan.md section 3.4."""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)
```

4. Change `ingest`'s signature and body. Replace the whole function with:

```python
def ingest(
    storage: Storage, payload: list[dict[str, Any]], *, event_at: datetime | None = None
) -> dict[str, int]:
    """Applies every valid entry in payload through `Storage.apply_feedback` at `event_at`
    (the export's mtime when called from `main`). A job omitted from payload is never touched.
    An entry older than the stored label/tombstone is skipped: counted `unchanged` when its
    label equals the stored one, otherwise under a `stale` key that only appears when nonzero.
    `event_at=None` (direct callers/tests) keeps the historical always-apply behavior, stamped
    with the current time. Returns counts: new, changed, unchanged, invalid[, stale]."""
    prior_by_key = {
        (row["source_key"], row["job_id"]): row["label"] for row in storage.export_job_feedback()
    }
    counts = {"new": 0, "changed": 0, "unchanged": 0, "invalid": 0}
    effective_event_at = event_at or datetime.now(UTC)
    for entry in payload:
        label = entry.get("label")
        if label not in _VALID_LABELS:
            print(
                f"job-hunter: skipping {entry.get('source_key')}/{entry.get('job_id')} — "
                f"invalid label {label!r} (expected one of {sorted(_VALID_LABELS)})",
            )
            counts["invalid"] += 1
            continue
        feedback = JobFeedback(
            source_key=entry["source_key"],
            job_id=entry["job_id"],
            company=entry["company"],
            title=entry["title"],
            department=entry.get("department"),
            score=entry.get("score"),
            label=label,
        )
        key = (feedback.source_key, feedback.job_id)
        outcome = storage.apply_feedback(
            feedback, event_at=effective_event_at, force=event_at is None
        )
        if outcome == "stale":
            if prior_by_key.get(key) == label:
                counts["unchanged"] += 1
            else:
                counts["stale"] = counts.get("stale", 0) + 1
            continue
        if key not in prior_by_key:
            counts["new"] += 1
        elif prior_by_key[key] != label:
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    return counts
```

5. In `main()`, change `counts = ingest(storage, payload)` to `counts = ingest(storage, payload, event_at=export_event_time(resolved))`, and extend the final print so a `stale` count is reported only when present:

```python
        + (f", {counts['invalid']} invalid" if counts["invalid"] else "")
        + (f", {counts['stale']} stale-skipped (older than a live change)" if counts.get("stale") else "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_apply_radar_feedback.py tests/test_storage.py -q && uv run ruff check scripts tests`
Expected: PASS (existing importer tests unchanged: they call `ingest` with no `event_at`, so `force=True`).

- [ ] **Step 5: Commit**

```bash
git add scripts/apply_radar_feedback.py tests/test_apply_radar_feedback.py
git commit -m "feat(feedback): importer applies exports at their mtime, skipping stale entries" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Client core logic (pure JS) with node tests

**Files:**
- Create: `scripts/templates/radar_live_core.js`
- Create: `tests/js/radar_live_core.test.js`
- Create: `tests/test_radar_live_js.py`

**Interfaces:**
- Consumes: nothing.
- Produces: in the browser `window.RadarLive`; in node `module.exports`. Functions (all pure):
  - `emptyFilters() -> {query, tags[], arrangement[], sponsorship[], minScore|null, postedDays|null, company, location, hasSalary, feedback, sort}`
  - `filtersActive(f) -> boolean` (sort alone does not count)
  - `rowPasses(facts, f, label, todayIso) -> boolean` where `facts = {score|null, posted, state, country, hasSalary, isNew, longStanding, arrangement, sponsorship, title, company, location}` and `label` is `'relevant'|'okay'|'irrelevant'|null|undefined`
  - `sortOrder(factsList, mode) -> number[]` (`mode` in `default|score|newest|company`)
  - `encodeHash(f) -> string` / `decodeHash(text) -> filters`
  - `enqueue(outbox, item, max?) -> outbox` (coalesce by `kind`+`key`, keep latest, cap)
  - `classifyStatus(httpStatus) -> 'ok'|'permanent'|'retry'`
  - `backoffMs(attempt) -> number`, `isoNow(ms) -> string`
  - `reconcile(localLabels, serverMap, pendingKeys) -> [{key, label}]`
  - `SORT_MODES`

- [ ] **Step 1: Write the failing tests**

Create `tests/js/radar_live_core.test.js`:

```js
const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../../scripts/templates/radar_live_core.js');

function facts(over) {
  return Object.assign({
    score: 80, posted: '2026-09-20', state: 'MI', country: 'US', hasSalary: false,
    isNew: false, longStanding: false, arrangement: 'onsite', sponsorship: 'unmentioned',
    title: 'ADAS Engineer', company: 'Ford', location: 'Dearborn, MI',
  }, over || {});
}
const TODAY = '2026-09-26';

test('empty filters pass everything', () => {
  assert.equal(core.rowPasses(facts(), core.emptyFilters(), null, TODAY), true);
  assert.equal(core.filtersActive(core.emptyFilters()), false);
});

test('query matches company or title, case-insensitively', () => {
  const f = core.emptyFilters();
  f.query = 'FORD';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.query = 'lidar';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('tag chips are a union (some), as in the static toolbar', () => {
  const f = core.emptyFilters();
  f.tags = ['new', 'long-standing'];
  assert.equal(core.rowPasses(facts({ isNew: true }), f, null, TODAY), true);
  assert.equal(core.rowPasses(facts({ longStanding: true }), f, null, TODAY), true);
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('min score excludes unreviewed (null score) rows once set', () => {
  const f = core.emptyFilters();
  f.minScore = 70;
  assert.equal(core.rowPasses(facts({ score: 70 }), f, null, TODAY), true);
  assert.equal(core.rowPasses(facts({ score: 69 }), f, null, TODAY), false);
  assert.equal(core.rowPasses(facts({ score: null }), f, null, TODAY), false);
});

test('posted-within uses whole days and excludes undated rows', () => {
  const f = core.emptyFilters();
  f.postedDays = 7;
  assert.equal(core.rowPasses(facts({ posted: '2026-09-19' }), f, null, TODAY), true); // 7 days
  assert.equal(core.rowPasses(facts({ posted: '2026-09-18' }), f, null, TODAY), false); // 8 days
  assert.equal(core.rowPasses(facts({ posted: '' }), f, null, TODAY), false);
});

test('company is an exact, case-insensitive match', () => {
  const f = core.emptyFilters();
  f.company = 'ford';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.company = 'for';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('location text searches location, state and country', () => {
  const f = core.emptyFilters();
  f.location = 'dearborn';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.location = 'mi';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.location = 'texas';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('has-salary and feedback filters', () => {
  const f = core.emptyFilters();
  f.hasSalary = true;
  assert.equal(core.rowPasses(facts({ hasSalary: false }), f, null, TODAY), false);
  assert.equal(core.rowPasses(facts({ hasSalary: true }), f, null, TODAY), true);

  const g = core.emptyFilters();
  g.feedback = 'untagged';
  assert.equal(core.rowPasses(facts(), g, null, TODAY), true);
  assert.equal(core.rowPasses(facts(), g, 'okay', TODAY), false);
  g.feedback = 'okay';
  assert.equal(core.rowPasses(facts(), g, 'okay', TODAY), true);
  assert.equal(core.rowPasses(facts(), g, 'relevant', TODAY), false);
  assert.equal(core.rowPasses(facts(), g, undefined, TODAY), false);
});

test('sortOrder: default is identity; score/newest/company are stable with nulls last', () => {
  const list = [
    facts({ score: 60, posted: '2026-09-01', company: 'Zeta' }),
    facts({ score: null, posted: '', company: 'alpha' }),
    facts({ score: 90, posted: '2026-09-10', company: 'Beta' }),
    facts({ score: 60, posted: '2026-09-20', company: 'Gamma' }),
  ];
  assert.deepEqual(core.sortOrder(list, 'default'), [0, 1, 2, 3]);
  assert.deepEqual(core.sortOrder(list, 'score'), [2, 0, 3, 1]); // 60s keep original order
  assert.deepEqual(core.sortOrder(list, 'newest'), [3, 2, 0, 1]); // undated last
  assert.deepEqual(core.sortOrder(list, 'company'), [1, 2, 3, 0]);
});

test('hash round-trips every field and omits defaults', () => {
  assert.equal(core.encodeHash(core.emptyFilters()), '');
  const f = core.emptyFilters();
  f.query = 'lidar & radar';
  f.tags = ['new'];
  f.arrangement = ['remote', 'hybrid'];
  f.sponsorship = ['available'];
  f.minScore = 75;
  f.postedDays = 14;
  f.company = 'Ford Motor';
  f.location = 'MI';
  f.hasSalary = true;
  f.feedback = 'untagged';
  f.sort = 'newest';
  assert.deepEqual(core.decodeHash('#' + core.encodeHash(f)), f);
});

test('decodeHash ignores malformed and out-of-range input', () => {
  const f = core.decodeHash('min=abc&days=-3&fb=bogus&sort=nope&q=%E0%A4%A&x&=y&min=250');
  assert.deepEqual(f, core.emptyFilters());
});

test('enqueue keeps only the latest write per kind+key and caps the length', () => {
  let box = [];
  box = core.enqueue(box, { kind: 'feedback', key: 'a|1', payload: { label: 'okay' } });
  box = core.enqueue(box, { kind: 'feedback', key: 'b|2', payload: { label: 'okay' } });
  box = core.enqueue(box, { kind: 'feedback', key: 'a|1', payload: { label: null } });
  assert.deepEqual(box.map((o) => o.key), ['b|2', 'a|1']);
  assert.equal(box[1].payload.label, null);
  let capped = [];
  for (let i = 0; i < 5; i++) capped = core.enqueue(capped, { kind: 'feedback', key: 'k' + i, payload: {} }, 3);
  assert.deepEqual(capped.map((o) => o.key), ['k2', 'k3', 'k4']);
});

test('classifyStatus separates success, permanent rejection and retryable failures', () => {
  assert.equal(core.classifyStatus(200), 'ok');
  assert.equal(core.classifyStatus(400), 'permanent');
  assert.equal(core.classifyStatus(404), 'permanent');
  assert.equal(core.classifyStatus(403), 'permanent');
  assert.equal(core.classifyStatus(408), 'retry');
  assert.equal(core.classifyStatus(429), 'retry');
  assert.equal(core.classifyStatus(500), 'retry');
  assert.equal(core.classifyStatus(503), 'retry');
});

test('backoff doubles from 2s and caps at 30s', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(core.backoffMs), [2000, 4000, 8000, 16000, 30000, 30000]);
});

test('reconcile pulls server state for keys with no pending write', () => {
  const local = { 'a|1': 'okay', 'b|2': 'relevant', 'c|3': 'okay' };
  const server = { 'a|1': { label: 'okay' }, 'b|2': { label: 'irrelevant' }, 'd|4': { label: 'okay' } };
  const changes = core.reconcile(local, server, ['c|3']);
  assert.deepEqual(changes.sort((x, y) => x.key.localeCompare(y.key)), [
    { key: 'b|2', label: 'irrelevant' },
    { key: 'd|4', label: 'okay' },
  ]);
  // A locally-labelled key the server no longer has is cleared unless a write is pending.
  assert.deepEqual(core.reconcile({ 'x|9': 'okay' }, {}, []), [{ key: 'x|9', label: null }]);
  assert.deepEqual(core.reconcile({ 'x|9': 'okay' }, {}, ['x|9']), []);
});
```

Create `tests/test_radar_live_js.py`:

```python
"""Runs the DOM-free client logic's `node --test` suite. Skipped where node isn't installed —
node is a dev convenience here, not a project dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_JS_TEST = Path(__file__).parent / "js" / "radar_live_core.test.js"


def test_radar_live_core_js_unit_tests_pass():
    result = subprocess.run(
        ["node", "--test", str(_JS_TEST)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_radar_live_js.py -q`
Expected: FAIL (`Cannot find module '../../scripts/templates/radar_live_core.js'`). If node is missing the test is skipped — install node or run the remaining steps and rely on the Task 7 smoke.

- [ ] **Step 3: Implement**

Create `scripts/templates/radar_live_core.js`:

```js
// Pure, DOM-free logic for the live radar page (docs/live-radar-dashboard-plan.md section 4).
// Inlined into the page by scripts/render_radar.py (live mode only) and unit-tested with
// `node --test` (tests/js/radar_live_core.test.js). Nothing here touches the DOM, the network,
// or storage — radar_live_ui.js owns all of that.
(function (root) {
  'use strict';

  var MAX_OUTBOX = 200;
  var SORT_MODES = ['default', 'score', 'newest', 'company'];
  var FEEDBACK_FILTERS = ['untagged', 'relevant', 'okay', 'irrelevant'];

  function emptyFilters() {
    return {
      query: '', tags: [], arrangement: [], sponsorship: [], minScore: null, postedDays: null,
      company: '', location: '', hasSalary: false, feedback: '', sort: 'default'
    };
  }

  // Sorting is a view choice, not a filter, so it doesn't make the "Clear filters" button appear.
  function filtersActive(f) {
    return !!(f.query.trim() || f.tags.length || f.arrangement.length || f.sponsorship.length ||
      f.minScore !== null || f.postedDays !== null || f.company || f.location.trim() ||
      f.hasSalary || f.feedback);
  }

  function dayDiff(todayIso, dayIso) {
    return Math.round((Date.parse(todayIso + 'T00:00:00Z') - Date.parse(dayIso + 'T00:00:00Z')) / 86400000);
  }

  function rowPasses(facts, f, label, todayIso) {
    var q = f.query.trim().toLowerCase();
    if (q && (facts.company + ' ' + facts.title).toLowerCase().indexOf(q) === -1) return false;
    if (f.tags.length && !f.tags.some(function (t) {
      return (t === 'new' && facts.isNew) || (t === 'long-standing' && facts.longStanding);
    })) return false;
    if (f.arrangement.length && f.arrangement.indexOf(facts.arrangement) === -1) return false;
    if (f.sponsorship.length && f.sponsorship.indexOf(facts.sponsorship) === -1) return false;
    if (f.minScore !== null && (facts.score === null || facts.score < f.minScore)) return false;
    if (f.postedDays !== null) {
      if (!facts.posted) return false;
      if (dayDiff(todayIso, facts.posted) > f.postedDays) return false;
    }
    if (f.company && facts.company.toLowerCase() !== f.company.toLowerCase()) return false;
    var loc = f.location.trim().toLowerCase();
    if (loc && (facts.location + ' ' + facts.state + ' ' + facts.country).toLowerCase().indexOf(loc) === -1) {
      return false;
    }
    if (f.hasSalary && !facts.hasSalary) return false;
    if (f.feedback === 'untagged' && label) return false;
    if (f.feedback && f.feedback !== 'untagged' && label !== f.feedback) return false;
    return true;
  }

  // Indices of `factsList` in display order. Stable: ties keep original (server) order.
  function sortOrder(factsList, mode) {
    var idx = factsList.map(function (_, i) { return i; });
    if (mode === 'default') return idx;
    function cmp(a, b) {
      var x = factsList[a], y = factsList[b], d = 0;
      if (mode === 'score') {
        d = (y.score === null ? -1 : y.score) - (x.score === null ? -1 : x.score);
      } else if (mode === 'newest') {
        d = (y.posted || '').localeCompare(x.posted || '');
      } else if (mode === 'company') {
        d = x.company.toLowerCase().localeCompare(y.company.toLowerCase());
      }
      return d || a - b;
    }
    return idx.sort(cmp);
  }

  function encodeHash(f) {
    var parts = [];
    function add(k, v) { parts.push(k + '=' + encodeURIComponent(v)); }
    if (f.query) add('q', f.query);
    if (f.tags.length) add('tag', f.tags.join(','));
    if (f.arrangement.length) add('arr', f.arrangement.join(','));
    if (f.sponsorship.length) add('spon', f.sponsorship.join(','));
    if (f.minScore !== null) add('min', String(f.minScore));
    if (f.postedDays !== null) add('days', String(f.postedDays));
    if (f.company) add('co', f.company);
    if (f.location) add('loc', f.location);
    if (f.hasSalary) add('sal', '1');
    if (f.feedback) add('fb', f.feedback);
    if (f.sort !== 'default') add('sort', f.sort);
    return parts.join('&');
  }

  function decodeHash(text) {
    var f = emptyFilters();
    String(text || '').replace(/^#/, '').split('&').forEach(function (pair) {
      var i = pair.indexOf('=');
      if (i < 1) return;
      var key = pair.slice(0, i), value;
      try { value = decodeURIComponent(pair.slice(i + 1)); } catch (e) { return; }
      var list = value ? value.split(',') : [];
      var n = parseInt(value, 10);
      if (key === 'q') f.query = value;
      else if (key === 'tag') f.tags = list;
      else if (key === 'arr') f.arrangement = list;
      else if (key === 'spon') f.sponsorship = list;
      else if (key === 'min') { if (!isNaN(n) && n >= 0 && n <= 100) f.minScore = n; }
      else if (key === 'days') { if (!isNaN(n) && n > 0) f.postedDays = n; }
      else if (key === 'co') f.company = value;
      else if (key === 'loc') f.location = value;
      else if (key === 'sal') f.hasSalary = value === '1';
      else if (key === 'fb') { if (FEEDBACK_FILTERS.indexOf(value) !== -1) f.feedback = value; }
      else if (key === 'sort') { if (SORT_MODES.indexOf(value) !== -1) f.sort = value; }
    });
    return f;
  }

  // Latest write per (kind, key) wins; oldest entries fall off past the cap.
  function enqueue(outbox, item, max) {
    var limit = max || MAX_OUTBOX;
    var next = outbox.filter(function (o) { return !(o.kind === item.kind && o.key === item.key); });
    next.push(item);
    return next.length > limit ? next.slice(next.length - limit) : next;
  }

  // 'permanent' = the server understood and refused (unknown job, bad payload): retrying can
  // never succeed, so the UI drops the write and says so. Everything else is worth retrying.
  function classifyStatus(status) {
    if (status >= 200 && status < 300) return 'ok';
    if (status >= 400 && status < 500 && status !== 408 && status !== 429) return 'permanent';
    return 'retry';
  }

  function backoffMs(attempt) {
    return Math.min(30000, 2000 * Math.pow(2, Math.max(0, attempt)));
  }

  function isoNow(ms) { return new Date(ms).toISOString(); }

  // localLabels: {key: label}; serverMap: {key: {label}}; pendingKeys: keys with an unsent write.
  function reconcile(localLabels, serverMap, pendingKeys) {
    var changes = [], seen = {};
    Object.keys(serverMap).forEach(function (k) {
      seen[k] = true;
      if (pendingKeys.indexOf(k) !== -1) return;
      if (localLabels[k] !== serverMap[k].label) changes.push({ key: k, label: serverMap[k].label });
    });
    Object.keys(localLabels).forEach(function (k) {
      if (seen[k] || pendingKeys.indexOf(k) !== -1) return;
      changes.push({ key: k, label: null });
    });
    return changes;
  }

  var api = {
    SORT_MODES: SORT_MODES, emptyFilters: emptyFilters, filtersActive: filtersActive,
    rowPasses: rowPasses, sortOrder: sortOrder, encodeHash: encodeHash, decodeHash: decodeHash,
    enqueue: enqueue, classifyStatus: classifyStatus, backoffMs: backoffMs, isoNow: isoNow,
    reconcile: reconcile
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RadarLive = api;
})(typeof window !== 'undefined' ? window : globalThis);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/js/radar_live_core.test.js && uv run pytest tests/test_radar_live_js.py -q`
Expected: all PASS (skipped only if node is absent).

- [ ] **Step 5: Commit**

```bash
git add scripts/templates/radar_live_core.js tests/js/radar_live_core.test.js tests/test_radar_live_js.py
git commit -m "feat(radar): DOM-free live client logic with node tests" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Live UI script (DOM wiring against a fixed contract)

**Files:**
- Create: `scripts/templates/radar_live_ui.js`

**Interfaces:**
- Consumes: `window.RadarLive` (Task 3) and `window.__RADAR_LIVE__ = {stem: string, feedback: {"source_key|job_id": {label}}, versions: {archive, assessments, feedback}}` (emitted by Task 5).
- **DOM contract that Task 5 must render** (the UI script depends on exactly these):
  - Rows: `details.row` and `div.plain-row`, each with `data-source-key`, `data-job-id`, `data-score` (empty for unreviewed), `data-posted` (`YYYY-MM-DD` or empty), `data-state`, `data-country`, `data-has-salary` (`1`/`0`), plus the existing `data-new`, `data-long-standing`, `data-arrangement`, `data-sponsorship`; text in `.job-title`, `.job-company`, optional `.job-location`.
  - Feedback: `.feedback-buttons` containing `.fb-btn[data-label]`; row class `fb-tagged` and button class `fb-active` mean "labelled".
  - Toolbar: `#radar-toolbar`, `#filter-search`, `.filter-chip[data-filter-group][data-filter-value]`, `#filter-clear`, `#filter-count`; live controls `#live-min-score` (number), `#live-posted-days` (select, `''`=any), `#live-company` (select; options are built by this script), `#live-location` (text), `#live-has-salary` (checkbox), `#live-feedback` (select), `#live-sort` (select).
  - Status: `#live-status`, `#live-notice`, `#live-reload` (hidden banner) containing `#live-reload-link`.
  - Groups: `details.group` with optional `.group-count`, `.group-note`, and a `.rows` container followed by `.filter-empty-state`.
- Produces: no exports; runs on load. Endpoints it calls (Task 6): `POST /api/feedback`, `GET /api/feedback` → `{feedback: {key: {label,...}}}`, `GET /api/state` → `{versions: {...}}`.

- [ ] **Step 1: Write the script**

Create `scripts/templates/radar_live_ui.js`:

```js
// DOM wiring for the live radar page. Requires window.RadarLive (radar_live_core.js) and
// window.__RADAR_LIVE__ (emitted by scripts/render_radar.py). SQLite (via the server) is the
// source of truth; localStorage holds only a bounded outbox of unsent writes.
(function () {
  'use strict';
  var L = window.RadarLive, boot = window.__RADAR_LIVE__;
  if (!L || !boot) return;

  var OUTBOX_KEY = 'job-hunter-outbox:' + boot.stem;
  var GROUPS_KEY = 'job-hunter-groups:' + boot.stem;
  var ROW_SELECTOR = 'details.row, div.plain-row';
  var SORT_NOTE = {
    score: 'score, highest first', newest: 'posting date, newest first', company: 'company, A\u2013Z'
  };
  var DEFAULT_NOTE_PREFIX = 'Sorted by score, highest first.';

  function $(id) { return document.getElementById(id); }
  function keyOf(row) { return row.dataset.sourceKey + '|' + row.dataset.jobId; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function todayIso() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  // ---- row facts -------------------------------------------------------------------------
  var rows = Array.prototype.slice.call(document.querySelectorAll(ROW_SELECTOR));
  var factsOf = new Map();
  var rowsByKey = {};
  function text(row, sel) { var el = row.querySelector(sel); return el ? el.textContent.trim() : ''; }
  rows.forEach(function (row) {
    var d = row.dataset;
    factsOf.set(row, {
      score: d.score === '' || d.score === undefined ? null : parseInt(d.score, 10),
      posted: d.posted || '', state: d.state || '', country: d.country || '',
      hasSalary: d.hasSalary === '1', isNew: d.new === '1', longStanding: d.longStanding === '1',
      arrangement: d.arrangement || '', sponsorship: d.sponsorship || '',
      title: text(row, '.job-title'), company: text(row, '.job-company'),
      location: text(row, '.job-location')
    });
    (rowsByKey[keyOf(row)] = rowsByKey[keyOf(row)] || []).push(row);
  });

  // ---- feedback state + outbox -----------------------------------------------------------
  var labels = {};
  Object.keys(boot.feedback).forEach(function (k) { labels[k] = boot.feedback[k].label; });
  var outbox = loadOutbox();
  var flushing = false, attempt = 0, retryTimer = null;

  function loadOutbox() {
    try {
      var parsed = JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) { return []; }
  }
  function saveOutbox() {
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(outbox)); } catch (e) { /* private mode / quota */ }
  }
  function pendingKeys() { return outbox.filter(function (o) { return o.kind === 'feedback'; }).map(function (o) { return o.key; }); }

  function paintRow(row) {
    var label = labels[keyOf(row)] || null;
    row.classList.toggle('fb-tagged', !!label);
    Array.prototype.forEach.call(row.querySelectorAll('.fb-btn'), function (b) {
      b.classList.toggle('fb-active', b.dataset.label === label);
    });
  }
  function setLabel(key, label) {
    if (label) labels[key] = label; else delete labels[key];
    (rowsByKey[key] || []).forEach(paintRow);
  }

  function setStatus() {
    var el = $('live-status');
    if (!el) return;
    var state = flushing ? 'saving' : (outbox.length ? 'offline' : 'live');
    el.dataset.state = state;
    el.textContent = state === 'saving' ? 'Saving\u2026'
      : state === 'offline' ? 'Offline (' + outbox.length + ' unsaved)' : 'Live';
  }
  var noticeTimer = null;
  function notice(message) {
    var el = $('live-notice');
    if (!el) return;
    el.textContent = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { el.textContent = ''; }, 8000);
  }

  function send(item) {
    return fetch('/api/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item.payload)
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (json) {
        return { status: res.status, json: json };
      });
    });
  }

  function flush() {
    if (flushing || !outbox.length) { setStatus(); return; }
    flushing = true;
    setStatus();
    var item = outbox[0];
    send(item).then(function (res) {
      var kind = L.classifyStatus(res.status);
      if (kind === 'retry') throw new Error('retry');
      // Remove only this exact entry: a newer write for the same key may have replaced it
      // while the request was in flight, and must stay queued.
      outbox = outbox.filter(function (o) { return o !== item; });
      var superseded = outbox.some(function (o) { return o.kind === item.kind && o.key === item.key; });
      if (kind === 'ok') {
        if (!superseded) setLabel(item.key, res.json && res.json.item ? res.json.item.label : null);
      } else {
        notice('Could not save that change: ' + ((res.json && res.json.error) || 'HTTP ' + res.status));
        if (!superseded) pullFeedback();
      }
      saveOutbox();
      flushing = false;
      attempt = 0;
      flush();
    }).catch(function () {
      flushing = false;
      attempt += 1;
      setStatus();
      clearTimeout(retryTimer);
      retryTimer = setTimeout(flush, L.backoffMs(attempt - 1));
    });
  }

  function queue(item) {
    outbox = L.enqueue(outbox, item);
    saveOutbox();
    setStatus();
    flush();
  }

  document.addEventListener('click', function (evt) {
    var btn = evt.target.closest ? evt.target.closest('.fb-btn') : null;
    if (!btn) return;
    // Nested inside <summary>: without this the click would also toggle the row open/closed.
    evt.preventDefault();
    evt.stopPropagation();
    var row = btn.closest(ROW_SELECTOR);
    if (!row) return;
    var key = keyOf(row);
    var next = labels[key] === btn.dataset.label ? null : btn.dataset.label;
    setLabel(key, next);
    queue({
      kind: 'feedback', key: key,
      payload: {
        source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, label: next,
        client_ts: L.isoNow(Date.now())
      }
    });
    applyFilters();
  });
  Array.prototype.forEach.call(document.querySelectorAll('details.row > summary .apply-link'), function (link) {
    link.addEventListener('click', function (evt) { evt.stopPropagation(); });
  });

  // ---- server sync: tab reconcile + polling ----------------------------------------------
  var known = boot.versions || {};
  function pullFeedback() {
    return fetch('/api/feedback').then(function (r) { return r.json(); }).then(function (body) {
      L.reconcile(labels, body.feedback || {}, pendingKeys()).forEach(function (c) { setLabel(c.key, c.label); });
      applyFilters();
    }).catch(function () { /* transient; the outbox status already reflects real save failures */ });
  }
  function showReload() {
    var banner = $('live-reload');
    if (banner) banner.hidden = false;
  }
  function poll() {
    if (document.visibilityState !== 'visible') return;
    fetch('/api/state').then(function (r) { return r.json(); }).then(function (s) {
      var v = s.versions || {};
      if (v.archive !== known.archive || v.assessments !== known.assessments) showReload();
      if (v.feedback !== known.feedback) { known.feedback = v.feedback; pullFeedback(); }
    }).catch(function () { /* server briefly unreachable; the next tick retries */ });
  }
  setInterval(poll, 10000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { poll(); pullFeedback(); flush(); }
  });
  window.addEventListener('focus', flush);
  var reloadLink = $('live-reload-link');
  if (reloadLink) reloadLink.addEventListener('click', function (evt) { evt.preventDefault(); location.reload(); });

  // ---- filters, sort, counts, hash -------------------------------------------------------
  var toolbar = $('radar-toolbar');
  var search = $('filter-search'), chips = Array.prototype.slice.call(toolbar.querySelectorAll('.filter-chip'));
  var minScore = $('live-min-score'), postedDays = $('live-posted-days'), company = $('live-company');
  var locationInput = $('live-location'), hasSalary = $('live-has-salary');
  var feedbackSel = $('live-feedback'), sortSel = $('live-sort');
  var clearBtn = $('filter-clear'), countEl = $('filter-count');

  var companies = {};
  factsOf.forEach(function (f) { if (f.company) companies[f.company] = true; });
  Object.keys(companies).sort(function (a, b) { return a.toLowerCase().localeCompare(b.toLowerCase()); })
    .forEach(function (name) {
      var opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      company.appendChild(opt);
    });

  function readFilters() {
    var f = L.emptyFilters();
    f.query = search.value;
    chips.forEach(function (c) {
      if (!c.classList.contains('active')) return;
      var g = c.dataset.filterGroup, v = c.dataset.filterValue;
      if (g === 'tag') f.tags.push(v);
      else if (g === 'arrangement') f.arrangement.push(v);
      else if (g === 'sponsorship') f.sponsorship.push(v);
    });
    var min = parseInt(minScore.value, 10);
    f.minScore = isNaN(min) ? null : Math.max(0, Math.min(100, min));
    var days = parseInt(postedDays.value, 10);
    f.postedDays = isNaN(days) ? null : days;
    f.company = company.value;
    f.location = locationInput.value;
    f.hasSalary = hasSalary.checked;
    f.feedback = feedbackSel.value;
    f.sort = sortSel.value;
    return f;
  }
  function writeFilters(f) {
    search.value = f.query;
    chips.forEach(function (c) {
      var list = c.dataset.filterGroup === 'tag' ? f.tags
        : c.dataset.filterGroup === 'arrangement' ? f.arrangement : f.sponsorship;
      c.classList.toggle('active', list.indexOf(c.dataset.filterValue) !== -1);
    });
    minScore.value = f.minScore === null ? '' : String(f.minScore);
    postedDays.value = f.postedDays === null ? '' : String(f.postedDays);
    company.value = f.company;
    locationInput.value = f.location;
    hasSalary.checked = f.hasSalary;
    feedbackSel.value = f.feedback;
    sortSel.value = f.sort;
  }

  var containers = [];
  Array.prototype.forEach.call(document.querySelectorAll('.rows'), function (div) {
    var own = Array.prototype.filter.call(div.children, function (el) { return el.matches(ROW_SELECTOR); });
    if (own.length) containers.push({ div: div, original: own });
  });
  Array.prototype.forEach.call(document.querySelectorAll('.group-count, .group-note'), function (el) {
    el.dataset.orig = el.textContent;
  });

  function applyFilters() {
    var f = readFilters();
    var today = todayIso();
    var active = L.filtersActive(f);
    var visible = 0;
    rows.forEach(function (row) {
      var ok = L.rowPasses(factsOf.get(row), f, labels[keyOf(row)], today);
      row.hidden = !ok;
      if (ok) visible += 1;
    });
    containers.forEach(function (c) {
      var order = L.sortOrder(c.original.map(function (r) { return factsOf.get(r); }), f.sort);
      order.forEach(function (i) { c.div.appendChild(c.original[i]); });
      var shown = c.original.filter(function (r) { return !r.hidden; }).length;
      var group = c.div.closest('details.group');
      if (group) {
        var count = group.querySelector('.group-count');
        if (count) count.textContent = active ? count.dataset.orig + ' \u00b7 ' + shown + ' shown' : count.dataset.orig;
        var note = group.querySelector('.group-note');
        if (note && note.dataset.orig.indexOf(DEFAULT_NOTE_PREFIX) === 0) {
          note.textContent = f.sort === 'default' || f.sort === 'score' ? note.dataset.orig
            : 'Sorted by ' + SORT_NOTE[f.sort] + '.' + note.dataset.orig.slice(DEFAULT_NOTE_PREFIX.length);
        }
      }
      var msg = c.div.nextElementSibling;
      if (msg && msg.classList.contains('filter-empty-state')) msg.hidden = shown > 0;
    });
    countEl.textContent = rows.length ? 'Showing ' + visible + ' of ' + rows.length : '';
    clearBtn.hidden = !active;
    var hash = L.encodeHash(f);
    try {
      history.replaceState(null, '', hash ? '#' + hash : location.pathname + location.search);
    } catch (e) { /* sandboxed frame */ }
  }

  chips.forEach(function (c) {
    c.addEventListener('click', function () { c.classList.toggle('active'); applyFilters(); });
  });
  [search, minScore, locationInput].forEach(function (el) { el.addEventListener('input', applyFilters); });
  [postedDays, company, hasSalary, feedbackSel, sortSel].forEach(function (el) {
    el.addEventListener('change', applyFilters);
  });
  clearBtn.addEventListener('click', function () {
    var keepSort = sortSel.value;
    writeFilters(L.emptyFilters());
    sortSel.value = keepSort;
    applyFilters();
  });

  // ---- group open/closed state (per tab session only) -------------------------------------
  var groups = Array.prototype.slice.call(document.querySelectorAll('details.group'));
  try {
    var saved = JSON.parse(sessionStorage.getItem(GROUPS_KEY) || 'null');
    if (Array.isArray(saved) && saved.length === groups.length) {
      groups.forEach(function (g, i) { g.open = !!saved[i]; });
    }
  } catch (e) { /* storage unavailable */ }
  groups.forEach(function (g) {
    g.addEventListener('toggle', function () {
      try {
        sessionStorage.setItem(GROUPS_KEY, JSON.stringify(groups.map(function (x) { return x.open; })));
      } catch (e) { /* storage unavailable */ }
    });
  });

  // ---- boot ------------------------------------------------------------------------------
  writeFilters(L.decodeHash(location.hash));
  rows.forEach(paintRow);
  applyFilters();
  setStatus();
  flush();
})();
```

- [ ] **Step 2: Syntax-check**

Run: `node --check scripts/templates/radar_live_ui.js && node --check scripts/templates/radar_live_core.js`
Expected: no output, exit 0. (Full behavior is verified by Task 5's render tests plus Task 7's browser smoke, since the DOM contract is rendered there.)

- [ ] **Step 3: Commit**

```bash
git add scripts/templates/radar_live_ui.js
git commit -m "feat(radar): live UI script (feedback outbox, filters, sort, polling)" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Renderer — `render()`/`build()` split and live mode

**Files:**
- Create: `tests/fixtures/radar_static_golden.html` (generated in Step 2, **before** any edit to `render_radar.py`)
- Modify: `scripts/render_radar.py` (`build` → `render`; row helpers; new live helpers)
- Modify: `scripts/templates/radar_template.html` (three inline placeholders)
- Test: `tests/test_render_radar.py`

**Interfaces:**
- Consumes: `scripts/templates/radar_live_core.js`, `radar_live_ui.js` (Tasks 3–4), the DOM contract in Task 4, `Storage.feedback_map()` rows (Task 1) supplied by callers.
- Produces:
  - `render_radar.LiveState(feedback: dict[str, dict[str, Any]], versions: dict[str, str | None], archive_name: str)` — frozen dataclass.
  - `render_radar.render(*, search_path, assessments_path, title, keyword_label, new_days, undated_new_days=15, undated_stale_days=45, now=None, database_path=None, profile=None, max_age_days=None, keywords=None, collection_fallback=True, live=False, live_state=None) -> tuple[str, dict[str, Any]]` — `(html, stats)`; `ValueError` if `live` and `live_state is None`.
  - `render_radar.build(*, output_path, **render_kwargs) -> dict[str, Any]` — unchanged signature/behavior for existing callers.

- [ ] **Step 1: Write the golden test (helper + env-guarded generation)**

Append to `tests/test_render_radar.py` (add `import os` to its imports):

```python
_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "radar_static_golden.html"


def _golden_inputs(tmp_path):
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", posted_at="2026-08-25T00:00:00Z", title="Scored Role",
                               company="Acme", salary_evidence="$100,000 - $120,000",
                               visa_sponsorship="available", work_arrangement="remote"),
                    _candidate("x", "2", posted_at="2026-08-20T00:00:00Z", title="Second Role"),
                    _candidate("x", "3", posted_at="2026-08-22T00:00:00Z", title="Unreviewed Role"),
                ],
                source_health=[
                    {"source_key": "beta", "company": "Beta Corp", "status": "failed",
                     "message": "Connection timed out."},
                ],
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps([_assessment("x", "1", 88, title="Scored Role"),
                    _assessment("x", "2", 41, title="Second Role")])
    )
    return dict(
        search_path=search_path, assessments_path=assessments_path, title="Golden Radar",
        keyword_label=None, new_days=10, now=now,
    )


def test_static_render_is_byte_identical_to_the_pre_live_golden(tmp_path):
    """Guards the "static report never changes" contract: the golden file was generated from
    build() *before* the render()/live split, so any static-mode drift fails here."""
    output_path = tmp_path / "out.html"
    build(output_path=output_path, **_golden_inputs(tmp_path))
    actual = output_path.read_text(encoding="utf-8")
    if os.environ.get("UPDATE_RADAR_GOLDEN") == "1":
        _GOLDEN_PATH.write_text(actual, encoding="utf-8")
        pytest.skip("golden regenerated")
    assert actual == _GOLDEN_PATH.read_text(encoding="utf-8")
```

- [ ] **Step 2: Generate the golden from the UNMODIFIED renderer, then confirm it passes**

Run (must happen before editing `render_radar.py` or the template):

```bash
git status --short scripts/render_radar.py scripts/templates/radar_template.html   # expect no output
UPDATE_RADAR_GOLDEN=1 uv run pytest tests/test_render_radar.py::test_static_render_is_byte_identical_to_the_pre_live_golden -q
uv run pytest tests/test_render_radar.py::test_static_render_is_byte_identical_to_the_pre_live_golden -q
```

Expected: first run "1 skipped", second run "1 passed". Commit the golden and test now:

```bash
git add tests/fixtures/radar_static_golden.html tests/test_render_radar.py
git commit -m "test(radar): pin static report output with a pre-refactor golden" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Write the failing live-mode tests**

Append to `tests/test_render_radar.py` (add `import re` and extend the render_radar import to `from render_radar import LiveState, _default_title, build, main, render`):

```python
def _live_state(feedback=None):
    return LiveState(
        feedback=feedback or {},
        versions={"archive": "a1", "assessments": "s1", "feedback": "f1"},
        archive_name="search.json",
    )


def _live_html(tmp_path, feedback=None, mutate=None):
    inputs = _golden_inputs(tmp_path)
    if mutate:
        mutate(inputs)
    html, stats = render(live=True, live_state=_live_state(feedback), **inputs)
    return html, stats


def test_render_returns_html_and_the_same_stats_build_reports(tmp_path):
    inputs = _golden_inputs(tmp_path)
    html, stats = render(**inputs)
    assert "Scored Role" in html
    output_path = tmp_path / "out.html"
    assert build(output_path=output_path, **inputs) == stats
    assert output_path.read_text(encoding="utf-8") == html


def test_live_render_requires_live_state(tmp_path):
    with pytest.raises(ValueError):
        render(live=True, **_golden_inputs(tmp_path))


def test_static_render_has_no_live_machinery(tmp_path):
    html, _ = render(**_golden_inputs(tmp_path))
    for needle in ("fetch(", "__RADAR_LIVE__", "live-status", "live-toolbar-extra", "__LIVE_"):
        assert needle not in html


def test_live_render_swaps_static_feedback_for_the_live_layer(tmp_path):
    html, _ = _live_html(tmp_path)
    assert "feedback-export-btn" not in html  # static Export button removed
    assert "job-hunter-feedback:" not in html  # static localStorage feedback script removed
    assert "Quick-glance client-side filtering" not in html  # static filter script replaced
    assert 'id="live-status"' in html
    assert 'id="live-reload"' in html
    assert "window.RadarLive" in html or "root.RadarLive" in html
    assert "__LIVE_" not in html
    # The rest of the report is untouched.
    assert "Scored Role" in html and "Unreviewed Role" in html and "Beta Corp" in html


def test_live_render_embeds_versions_stem_and_initial_feedback(tmp_path):
    fb = {"x|1": {"label": "okay"}}
    html, _ = _live_html(tmp_path, feedback=fb)
    assert '"stem": "search"' in html
    assert '"archive": "a1"' in html
    assert '"x|1": {"label": "okay"}' in html
    assert "search.json" in html  # footer archive name


def test_live_rows_have_identity_and_filter_attrs_but_no_title_or_company_attrs(tmp_path):
    html, _ = _live_html(tmp_path)
    scored = re.search(r'<details class="row[^>]*data-job-id="1"[^>]*>', html)
    assert scored, "scored row root missing live attributes"
    tag = scored.group(0)
    for attr in ('data-source-key="x"', 'data-score="88"', 'data-posted="2026-08-25"',
                 'data-has-salary="1"', 'data-state=', 'data-country='):
        assert attr in tag
    assert "data-title" not in tag and "data-company" not in tag
    unreviewed = re.search(r'<div class="plain-row[^>]*data-job-id="3"[^>]*>', html)
    assert unreviewed and 'data-score=""' in unreviewed.group(0)
    assert "data-title" not in unreviewed.group(0)


def test_live_render_paints_initial_feedback_without_a_flash(tmp_path):
    fb = {"x|1": {"label": "irrelevant"}, "x|3": {"label": "relevant"}}
    html, _ = _live_html(tmp_path, feedback=fb)
    scored = re.search(r'<details class="row[^>]*data-job-id="1"[^>]*>', html).group(0)
    assert "fb-tagged" in scored
    assert 'class="fb-btn fb-irrelevant fb-active"' in html
    assert 'class="fb-btn fb-relevant fb-active"' in html  # unreviewed row too
    untouched = re.search(r'<details class="row[^>]*data-job-id="2"[^>]*>', html).group(0)
    assert "fb-tagged" not in untouched


def test_live_render_escapes_hostile_text_in_attributes_and_embedded_json(tmp_path):
    def mutate(inputs):
        candidate = _candidate("x", "9", title='</script><img src=x onerror=alert(1)>',
                               company='A"B&C', posted_at="2026-08-25T00:00:00Z")
        data = json.loads(inputs["search_path"].read_text())
        data["candidates"].append(candidate)
        hostile = inputs["search_path"].with_name("a<b>&c.json")
        hostile.write_text(json.dumps(data))
        inputs["search_path"] = hostile
        assessments = json.loads(inputs["assessments_path"].read_text())
        assessments.append(_assessment("x", "9", 60, title=candidate["title"], company=candidate["company"]))
        inputs["assessments_path"].write_text(json.dumps(assessments))

    html, _ = _live_html(tmp_path, mutate=mutate)
    assert "<img src=x" not in html
    assert "</script><img" not in html
    assert '"stem": "a\\u003cb\\u003e\\u0026c"' in html
    assert 'data-company="A&quot;B&amp;C"' in html  # existing feedback-button attr, still escaped


def test_live_scripts_contain_no_template_tokens_or_script_terminators():
    scripts_dir = Path(__file__).parents[1] / "scripts" / "templates"
    template = (scripts_dir / "radar_template.html").read_text(encoding="utf-8")
    tokens = set(re.findall(r"__[A-Z][A-Z0-9_]*__", template))
    for name in ("radar_live_core.js", "radar_live_ui.js"):
        source = (scripts_dir / name).read_text(encoding="utf-8")
        assert "</script" not in source.lower()
        assert not {t for t in tokens if t in source}, name
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_render_radar.py -q`
Expected: collection error (`ImportError: cannot import name 'LiveState'`).

- [ ] **Step 5: Implement the template placeholders**

In `scripts/templates/radar_template.html` make exactly these three inline edits (each keeps static output byte-identical because the placeholder renders to `""` with no added whitespace):

1. Line ~870: change `</style>` to `__LIVE_STYLE__</style>`.
2. Line ~925 (`toolbar`): change `    <button type="button" id="filter-clear" class="toolbar-clear" hidden>Clear filters</button>` to `    __LIVE_TOOLBAR__<button type="button" id="filter-clear" class="toolbar-clear" hidden>Clear filters</button>`.
3. The file's final line `</script>` (end of the filter script, after `applyFilters();\n})();`): change to `</script>__LIVE_SCRIPT__` (the file ends with a single trailing newline — keep it).

- [ ] **Step 6: Implement `render_radar.py`**

Add imports at the top: `import re`, `from dataclasses import dataclass`. Add below `_TEMPLATE_PATH`:

```python
_TEMPLATE_DIR = _TEMPLATE_PATH.parent
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class LiveState:
    """Everything a live render needs that isn't in the archive/assessments: the current
    feedback rows (keyed "source_key|job_id"), the state versions the client polls against, and
    the archive filename shown in the footer. Passed in explicitly — the renderer never opens
    its own DB connection."""

    feedback: dict[str, dict[str, Any]]
    versions: dict[str, str | None]
    archive_name: str
```

Add these helpers above `_row_html`:

```python
def _live_row_attrs(
    *, source_key: str, job_id: str, score: int | None, posted_at: str | None,
    state: str | None, country: str | None, salary_evidence: str | None,
) -> str:
    """Identity + filter attributes for a live row root. Deliberately no company/title (see
    `_filter_data_attrs`'s docstring); the live script reads those from the visible text."""
    posted = (posted_at or "")[:10]
    if not _ISO_DAY.fullmatch(posted):
        posted = ""
    return (
        f' data-source-key="{_attr(source_key)}" data-job-id="{_attr(job_id)}"'
        f' data-score="{"" if score is None else score}" data-posted="{posted}"'
        f' data-state="{_attr(state)}" data-country="{_attr(country)}"'
        f' data-has-salary="{1 if salary_evidence else 0}"'
    )


def _feedback_buttons_html(
    *, source_key: str, job_id: str, company: str | None, title: str | None,
    department: str | None, score_attr: str, label: str | None = None,
) -> str:
    """The three 👍/🆗/👎 buttons. With `label=None` (static mode, and any unlabelled live row)
    this is byte-for-byte the markup the two row builders used to inline."""

    def active(name: str) -> str:
        return " fb-active" if label == name else ""

    return f'''<span class="feedback-buttons"
          data-source-key="{_attr(source_key)}" data-job-id="{_attr(job_id)}"
          data-company="{_attr(company)}" data-title="{_attr(title)}"
          data-department="{_attr(department)}" data-score="{score_attr}">
          <button type="button" class="fb-btn fb-relevant{active("relevant")}" data-label="relevant" title="Relevant">&#128077;</button>
          <button type="button" class="fb-btn fb-okay{active("okay")}" data-label="okay" title="Okay">&#128994;</button>
          <button type="button" class="fb-btn fb-irrelevant{active("irrelevant")}" data-label="irrelevant" title="Irrelevant">&#128078;</button>
        </span>'''


def _live_label(feedback: dict[str, dict[str, Any]] | None, source_key: str, job_id: str) -> str | None:
    if not feedback:
        return None
    entry = feedback.get(f"{source_key}|{job_id}")
    return entry["label"] if entry else None
```

Change `_row_html` to `def _row_html(row, *, live: bool = False, feedback: dict[str, dict[str, Any]] | None = None)`; replace its inline `feedback_buttons = f'''...'''` block with:

```python
    label = _live_label(feedback, row["source_key"], row["job_id"]) if live else None
    feedback_buttons = _feedback_buttons_html(
        source_key=row["source_key"], job_id=row["job_id"], company=row["company"],
        title=row["title"], department=row.get("department"), score_attr=str(row["score"]),
        label=label,
    )
    live_attrs = (
        _live_row_attrs(
            source_key=row["source_key"], job_id=row["job_id"], score=row["score"],
            posted_at=row["posted_at"], state=row.get("state"), country=row.get("country"),
            salary_evidence=row.get("salary_evidence"),
        )
        if live
        else ""
    )
    tagged = " fb-tagged" if label else ""
```

and change the opening tag line `<details class="row tier-{tier}" {filter_attrs}>` to `<details class="row tier-{tier}{tagged}" {filter_attrs}{live_attrs}>`.

Do the same in `_never_reviewed_row_html` (add `live: bool = False, feedback: ... = None` parameters; compute `label`, `live_attrs` with `score=None`, `posted_at=posted_at`, `state=candidate.get("state")`, `country=candidate.get("country")`, `salary_evidence=candidate.get("salary_evidence")`; build the buttons via `_feedback_buttons_html(..., score_attr="", label=label)`), and change `<div class="plain-row" {filter_attrs}>` to `<div class="plain-row{tagged}" {filter_attrs}{live_attrs}>`.

Thread the parameters through `_rows_html(rows, *, empty_message, live=False, feedback=None)` (`_row_html(row, live=live, feedback=feedback)`) and `_never_reviewed_rows_html(candidates, *, now, ..., live=False, feedback=None)`.

Add `"state": candidate.get("state"), "country": candidate.get("country"),` to the per-row dict built in the scoring loop.

Add the live block builders above `render`:

```python
_LIVE_STATIC_BLOCKS = (
    ('<div class="feedback-export">', "</div>"),
    ('<script>\n(function () {\n  // Per-job relevance feedback', "</script>"),
    ('<script>\n(function () {\n  // Quick-glance client-side filtering', "</script>"),
)

_LIVE_STYLE = """
  .live-toolbar-extra { display: contents; }
  .toolbar-field { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-soft); }
  .toolbar-num, .toolbar-select { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: inherit; }
  .toolbar-num { width: 64px; }
  .live-bar { position: fixed; bottom: 24px; right: 24px; z-index: 10; display: flex; flex-direction: column; align-items: flex-end; gap: 6px; font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  .live-pill { padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line); background: var(--surface); box-shadow: var(--shadow); font-weight: 600; }
  .live-pill[data-state="live"] { color: var(--accent); }
  .live-pill[data-state="saving"] { color: var(--ink-soft); }
  .live-pill[data-state="offline"] { background: var(--accent); color: var(--surface); }
  .live-notice, .live-reload, .live-archive { padding: 4px 10px; border-radius: 8px; background: var(--surface); border: 1px solid var(--line); }
  .live-notice:empty { display: none; }
  .live-archive { color: var(--muted); }
"""

_LIVE_TOOLBAR = """<span class="live-toolbar-extra">
      <label class="toolbar-field">Min score <input type="number" id="live-min-score" class="toolbar-num" min="0" max="100" step="5"></label>
      <label class="toolbar-field">Posted <select id="live-posted-days" class="toolbar-select"><option value="">any time</option><option value="7">last 7 days</option><option value="14">last 14 days</option><option value="30">last 30 days</option></select></label>
      <label class="toolbar-field">Company <select id="live-company" class="toolbar-select"><option value="">all</option></select></label>
      <label class="toolbar-field">Location <input type="search" id="live-location" class="toolbar-select" placeholder="state or city" autocomplete="off"></label>
      <label class="toolbar-field"><input type="checkbox" id="live-has-salary"> Has salary</label>
      <label class="toolbar-field">Feedback <select id="live-feedback" class="toolbar-select"><option value="">any</option><option value="untagged">untagged</option><option value="relevant">relevant</option><option value="okay">okay</option><option value="irrelevant">irrelevant</option></select></label>
      <label class="toolbar-field">Sort <select id="live-sort" class="toolbar-select"><option value="default">default</option><option value="score">score</option><option value="newest">newest</option><option value="company">company</option></select></label>
    </span>"""


def _json_for_script(value: Any) -> str:
    """JSON safe to embed inside a <script> element: `<`, `>`, `&` and the two JS line
    separators are \\u-escaped so hostile text can never close the tag or break the literal."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def _replace_block(text: str, start: str, end: str, replacement: str) -> str:
    """Replace the first `start` ... `end` span. Raises (rather than silently no-ops) if the
    template no longer contains it, so a template edit that breaks live mode fails loudly."""
    try:
        first = text.index(start)
        last = text.index(end, first) + len(end)
    except ValueError as exc:
        raise ValueError(f"radar template no longer contains the block starting {start!r}") from exc
    return text[:first] + replacement + text[last:]


def _live_bar_html(live_state: LiveState, sources: str) -> str:
    rendered = datetime.now().astimezone().strftime("%H:%M:%S")
    return (
        '<div class="live-bar">'
        '<span id="live-status" class="live-pill" role="status" data-state="live">Live</span>'
        '<span id="live-notice" class="live-notice" role="alert"></span>'
        '<span id="live-reload" class="live-reload" hidden>New results &mdash; '
        '<a href="#" id="live-reload-link">Reload</a></span>'
        f'<span class="live-archive">{_e(live_state.archive_name)} &middot; {_e(sources)} sources '
        f"&middot; rendered {rendered}</span></div>"
    )


def _live_script_html(live_state: LiveState, stem: str) -> str:
    boot = {
        "stem": stem,
        "feedback": {k: {"label": v["label"]} for k, v in live_state.feedback.items()},
        "versions": live_state.versions,
    }
    parts = [f"<script>window.__RADAR_LIVE__ = {_json_for_script(boot)};</script>"]
    for name in ("radar_live_core.js", "radar_live_ui.js"):
        source = (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
        if "</script" in source.lower():
            raise ValueError(f"{name} must not contain a script terminator")
        parts.append(f"<script>\n{source}\n</script>")
    return "\n".join(parts)
```

Rename `def build(` to `def render(` and (a) add parameters `live: bool = False, live_state: LiveState | None = None` at the end of its signature, **remove** the `output_path: Path` parameter, (b) add at the top of the body `if live and live_state is None: raise ValueError("live=True requires a LiveState")`, (c) replace the `_rows_html(...)`/`_never_reviewed_rows_html(...)` calls in the substitution chain so they pass `live=live, feedback=live_state.feedback if live_state else None`, (d) change `template = _TEMPLATE_PATH.read_text(...)` and the start of the chain to apply the live edits **first, on the raw template**:

```python
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    if live:
        for start, end in _LIVE_STATIC_BLOCKS[1:]:
            template = _replace_block(template, start, end, "")
        template = _replace_block(
            template, *_LIVE_STATIC_BLOCKS[0], _live_bar_html(live_state, sources)
        )
        template = (
            template.replace("__LIVE_STYLE__", _LIVE_STYLE)
            .replace("__LIVE_TOOLBAR__", _LIVE_TOOLBAR)
            .replace("__LIVE_SCRIPT__", _live_script_html(live_state, search_path.stem))
        )
    else:
        template = (
            template.replace("__LIVE_STYLE__", "")
            .replace("__LIVE_TOOLBAR__", "")
            .replace("__LIVE_SCRIPT__", "")
        )
    out = (
        template.replace("__TITLE__", _e(title))
        ...   # rest of the existing chain unchanged
    )
```

(e) replace the trailing `atomic_write_text(output_path, out)` + `return {...}` with `return out, {...same stats dict...}`. Add the wrapper after `render`:

```python
def build(*, output_path: Path, **render_kwargs: Any) -> dict[str, Any]:
    """Render the static (file://) report and write it atomically. Signature and return value
    are unchanged for every existing caller/test; live rendering goes through `render()`."""
    out, stats = render(live=False, **render_kwargs)
    atomic_write_text(output_path, out)
    return stats
```

Note `sources` (the `"N/M"` string) is already computed before the template step in the existing body — confirm it is defined above where `_live_bar_html` is called; if `sources` is computed after, move the `summary`/`sources` lines up. `_live_bar_html` receives it so the footer reads e.g. `search.json · 20/21 sources · rendered 14:03:11`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_render_radar.py -q && uv run ruff check scripts tests`
Expected: PASS — the golden test proves static output is unchanged; all 24 original tests pass unmodified.

- [ ] **Step 8: Commit**

```bash
git add scripts/render_radar.py scripts/templates/radar_template.html tests/test_render_radar.py
git commit -m "feat(radar): pure render() with an opt-in live mode; build() unchanged" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Live server (`scripts/serve_radar.py`)

**Files:**
- Modify: `scripts/render_radar.py` (extract shared selection args + kwargs; `main()` uses them)
- Create: `scripts/serve_radar.py`
- Test: `tests/test_serve_radar.py`, `tests/test_render_radar.py` (existing `main` tests must still pass)

**Interfaces:**
- Consumes: `render_radar.render`/`LiveState` (Task 5), `Storage` feedback/snapshot methods (Task 1), `run_lock`/`RunLockHeld`, `add_project_argument`/`chdir_to_project_root`, `resolve_search_path`.
- Produces:
  - `render_radar.add_selection_arguments(parser)` — registers `--search --assessments --title --keyword --companies --new-days --undated-new-days --undated-stale-days --no-collection-fallback` (verbatim definitions moved out of `main()`).
  - `render_radar.selection_render_kwargs(args, settings, profile) -> dict[str, Any]` — kwargs for `render()`/`build()` (everything except `search_path`, `assessments_path`, `output_path`, `now`, `live`, `live_state`).
  - `serve_radar.build_parser() -> ArgumentParser` (adds `--host --port --open --allowed-host` + `--project`).
  - `serve_radar.ServerConfig(args, settings, profile_loader, extra_hosts=frozenset())`, `serve_radar.RadarServer(cfg)` (`ThreadingHTTPServer`; `.allowed`, `.server_address`), `serve_radar.FeedbackWrite`, `serve_radar.write_feedback(storage, req, *, now) -> tuple[int, dict]`, `serve_radar.allowed_hosts(host, port, extra)`, `serve_radar.non_loopback_warning(host) -> str | None`, `serve_radar.main(argv=None) -> int`.
  - HTTP: `GET /`, `GET /api/state` → `{ok, versions{archive,assessments,feedback}, counts{feedback}, archive_name}`, `GET /api/feedback` → `{ok, feedback{key: {source_key, job_id, label, recorded_at}}}`, `POST /api/feedback` body `{source_key, job_id, label|null, client_ts}` → `{ok, item}` | `{ok, deleted}` | `{ok, stale, item|null}`; errors `{ok:false, error}` with 400/403/404/411/413/415/503/500.

- [ ] **Step 1: Refactor `render_radar.main()` to share the selection arguments (behavior-preserving)**

In `scripts/render_radar.py`, move the nine `parser.add_argument(...)` blocks for `--search`, `--assessments`, `--title`, `--keyword`, `--companies`, `--new-days`, `--undated-new-days`, `--undated-stale-days`, `--no-collection-fallback` **verbatim** into a new function, leaving `--output` and `--result-json` in `main()`:

```python
def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags that choose *which* report to build (archive, scope, windows) — shared by this
    script's CLI and `serve_radar.py` so the two can never drift apart."""
    parser.add_argument("--search", type=Path, default=None, help=(...verbatim...))
    ...   # the other eight, verbatim
```

Add:

```python
def selection_render_kwargs(args: argparse.Namespace, settings: Any, profile: CandidateProfile | None) -> dict[str, Any]:
    keywords = [t.strip() for t in args.keyword.split(",") if t.strip()] if args.keyword else None
    return dict(
        title=args.title or _default_title(args.keyword),
        keyword_label=args.keyword,
        new_days=args.new_days,
        undated_new_days=(
            args.undated_new_days if args.undated_new_days is not None else settings.search.undated_new_days
        ),
        undated_stale_days=(
            args.undated_stale_days if args.undated_stale_days is not None else settings.search.undated_stale_days
        ),
        database_path=settings.database_path,
        profile=profile,
        max_age_days=settings.search.max_posting_age_days,
        keywords=keywords,
        collection_fallback=not args.no_collection_fallback,
    )
```

In `main()`: call `add_selection_arguments(parser)`, keep `--output`/`--result-json`/`add_project_argument`, and replace the inline `title`/`undated_*`/`keywords`/`build(...)` wiring with:

```python
    settings = load_settings()
    stats = build(
        search_path=args.search, assessments_path=args.assessments, output_path=output_path,
        **selection_render_kwargs(args, settings, load_profile()),
    )
```

(`output_path` computation stays above.) Run `uv run pytest tests/test_render_radar.py -q` → all PASS (incl. the golden and `test_negative_day_window_options_are_rejected`).

- [ ] **Step 2: Write the failing server tests**

Create `tests/test_serve_radar.py`:

```python
import http.client
import json
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import serve_radar  # noqa: E402

from job_hunter.config import Settings
from job_hunter.models import Assessment, Job, LocationConfidence
from job_hunter.runlock import run_lock
from job_hunter.storage import Storage


def _job(job_id, title="Engineer", company="Acme"):
    return Job(
        source_key="acme", source_platform="test", company=company, job_id=job_id, title=title,
        url=f"https://example.com/{job_id}", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, description="secret body", content_hash="h",
    )


def _candidate(job_id, title, company="Acme"):
    return {
        "source_key": "acme", "job_id": job_id, "posted_at": "2026-09-20T00:00:00Z",
        "first_seen_at": None, "location_raw": "Detroit, MI", "visa_sponsorship": "unmentioned",
        "sponsorship_evidence": None, "salary_evidence": None, "work_arrangement": "unknown",
        "company": company, "title": title, "url": f"https://example.com/{job_id}",
        "state": "MI", "country": "US",
    }


def _ts(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


class Env:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.db = tmp_path / "data" / "jobs.sqlite3"
        self.archive = tmp_path / "data" / "searches" / "default_2026-09-26.json"
        self.assessments = tmp_path / "data" / "assessments.json"
        self.archive.parent.mkdir(parents=True)
        self.archive.write_text(json.dumps({
            "summary": {"sources_attempted": 3, "sources_succeeded": 3},
            "candidates": [_candidate("1", "ADAS Engineer"), _candidate("2", "Other Role")],
            "source_health": [],
        }))
        self.assessments.write_text(json.dumps([{
            "source_key": "acme", "job_id": "1", "score": 82, "company": "Acme",
            "title": "ADAS Engineer", "url": "https://example.com/1", "matches": ["m"], "gaps": ["g"],
        }]))
        with Storage(self.db) as storage:
            storage.upsert_job(_job("1", "ADAS Engineer"))
            storage.upsert_job(_job("2", "Other Role"))
            storage.upsert_assessment(Assessment(
                source_key="acme", job_id="1", company="Acme", title="ADAS Engineer",
                url="https://example.com/1", content_hash="h", score=82, recommended=True,
                matches=["m"], gaps=["g"],
            ))
        args = serve_radar.build_parser().parse_args([
            "--search", str(self.archive), "--assessments", str(self.assessments), "--port", "0",
        ])
        cfg = serve_radar.ServerConfig(
            args=args, settings=Settings(database_path=self.db), profile_loader=lambda: None
        )
        self.server = serve_radar.RadarServer(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = raw
        if body is not None:
            payload = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        parsed = None
        if "json" in (response.getheader("Content-Type") or ""):
            parsed = json.loads(data)
        return response.status, response, parsed if parsed is not None else data.decode("utf-8")

    def post_feedback(self, label, seconds_ago, job_id="1", **extra):
        body = {"source_key": "acme", "job_id": job_id, "label": label, "client_ts": _ts(seconds_ago)}
        body.update(extra)
        return self.request("POST", "/api/feedback", body)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_root_serves_a_live_page_without_writing_radar_files(env):
    status, response, html = env.request("GET", "/")
    assert status == 200
    assert response.getheader("Cache-Control") == "no-store"
    assert "window.__RADAR_LIVE__" in html and "ADAS Engineer" in html
    assert not list((env.root / "data").glob("radar*"))


def test_unknown_routes_and_data_files_are_never_served(env):
    for path in ("/nope", "/data/jobs.sqlite3", "/../etc/passwd", "/api", "/applications"):
        status, _, _ = env.request("GET", path)
        assert status == 404, path


def test_unsupported_methods_are_rejected(env):
    for method in ("PUT", "DELETE", "PATCH"):
        status, _, _ = env.request(method, "/api/feedback", body={})
        assert status == 405


def test_feedback_create_relabel_and_untag_round_trip(env):
    status, _, body = env.post_feedback("okay", 30)
    assert status == 200 and body["item"]["label"] == "okay"
    assert env.request("GET", "/api/feedback")[2]["feedback"]["acme|1"]["label"] == "okay"
    status, _, body = env.post_feedback("irrelevant", 20)
    assert body["item"]["label"] == "irrelevant"
    status, _, body = env.post_feedback(None, 10)
    assert status == 200 and body == {"ok": True, "deleted": True}
    assert env.request("GET", "/api/feedback")[2]["feedback"] == {}


def test_server_derives_snapshot_fields_and_rejects_client_supplied_ones(env):
    status, _, body = env.post_feedback("okay", 5, company="Evil Corp")
    assert status == 400  # extra="forbid": client metadata is never accepted
    env.post_feedback("okay", 5)
    with Storage(env.db) as storage:
        row = storage.get_job_feedback("acme", "1")
    assert (row["company"], row["title"], row["score"]) == ("Acme", "ADAS Engineer", 82)


def test_late_retry_cannot_overwrite_a_newer_edit(env):
    env.post_feedback("okay", 30)
    status, _, body = env.post_feedback("irrelevant", 60)  # older than what's stored
    assert status == 200 and body["stale"] is True and body["item"]["label"] == "okay"
    assert env.post_feedback("irrelevant", 1)[2]["item"]["label"] == "irrelevant"


def test_a_stale_label_cannot_resurrect_an_untagged_job(env):
    env.post_feedback("okay", 40)
    env.post_feedback(None, 20)
    status, _, body = env.post_feedback("relevant", 30)
    assert body["stale"] is True and body["item"] is None
    assert env.request("GET", "/api/feedback")[2]["feedback"] == {}


def test_unknown_job_is_404_but_untagging_an_existing_label_of_a_removed_job_works(env):
    assert env.post_feedback("okay", 5, job_id="nope")[0] == 404
    assert env.post_feedback(None, 5, job_id="nope")[0] == 404
    env.post_feedback("okay", 30)
    with Storage(env.db) as storage:  # job later removed by `cleanup`
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    assert env.post_feedback("okay", 5)[0] == 404  # tagging a job that is gone is refused...
    assert env.post_feedback(None, 5)[0] == 200  # ...but a stale tab can still untag it


def test_request_validation_errors(env):
    assert env.request("POST", "/api/feedback", raw="{not json", headers={"Content-Type": "application/json"})[0] == 400
    assert env.request("POST", "/api/feedback", body=[1, 2])[0] == 400
    assert env.post_feedback("maybe", 5)[0] == 400
    assert env.post_feedback("okay", -3600)[0] == 400  # client_ts in the future
    naive = {"source_key": "acme", "job_id": "1", "label": "okay", "client_ts": "2026-09-26T12:00:00"}
    assert env.request("POST", "/api/feedback", naive)[0] == 400
    blank = {"source_key": " ", "job_id": "1", "label": "okay", "client_ts": _ts(1)}
    assert env.request("POST", "/api/feedback", blank)[0] == 400
    missing_label = {"source_key": "acme", "job_id": "1", "client_ts": _ts(1)}
    assert env.request("POST", "/api/feedback", missing_label)[0] == 400


def test_request_guards_content_type_origin_host_and_body_size(env):
    payload = json.dumps({"source_key": "acme", "job_id": "1", "label": "okay", "client_ts": _ts(1)})
    assert env.request("POST", "/api/feedback", raw=payload, headers={"Content-Type": "text/plain"})[0] == 415
    host = f"127.0.0.1:{env.port}"
    ok_headers = {"Content-Type": "application/json", "Origin": f"http://{host}"}
    assert env.request("POST", "/api/feedback", raw=payload, headers=ok_headers)[0] == 200
    bad_origin = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert env.request("POST", "/api/feedback", raw=payload, headers=bad_origin)[0] == 403
    assert env.request("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    big = "x" * (serve_radar.MAX_BODY_BYTES + 1)
    assert env.request("POST", "/api/feedback", raw=big, headers={"Content-Type": "application/json"})[0] == 413
    response_headers = env.request("GET", "/api/state")[1]
    assert response_headers.getheader("Access-Control-Allow-Origin") is None


def test_state_versions_change_with_archive_assessments_and_feedback(env):
    v0 = env.request("GET", "/api/state")[2]["versions"]
    env.post_feedback("okay", 30)
    v1 = env.request("GET", "/api/state")[2]
    assert v1["versions"]["feedback"] != v0["feedback"]
    assert v1["counts"]["feedback"] == 1 and v1["archive_name"] == env.archive.name
    env.post_feedback("irrelevant", 10)  # same row count, different label
    v2 = env.request("GET", "/api/state")[2]["versions"]
    assert v2["feedback"] != v1["versions"]["feedback"]
    env.assessments.write_text(env.assessments.read_text() + " ")
    assert env.request("GET", "/api/state")[2]["versions"]["assessments"] != v0["assessments"]
    data = json.loads(env.archive.read_text())
    data["candidates"].append(_candidate("3", "New Role"))
    env.archive.write_text(json.dumps(data))
    assert env.request("GET", "/api/state")[2]["versions"]["archive"] != v0["archive"]


def test_missing_archive_is_a_clear_503_not_a_crash(env):
    os.remove(env.archive)
    status, _, body = env.request("GET", "/")
    assert status == 503 and "does not exist" in body["error"]
    assert env.request("GET", "/api/state")[2]["archive_name"] is None


def test_allowed_hosts_rules():
    assert serve_radar.allowed_hosts("127.0.0.1", 8765) == {
        "127.0.0.1:8765", "localhost:8765", "[::1]:8765"
    }
    wildcard = serve_radar.allowed_hosts("0.0.0.0", 9, ["Radar.lan:9"])
    assert "radar.lan:9" in wildcard and "0.0.0.0:9" not in wildcard
    assert serve_radar.allowed_hosts("192.168.1.5", 9) == {"192.168.1.5:9"}


def test_non_loopback_warning():
    assert serve_radar.non_loopback_warning("127.0.0.1") is None
    assert serve_radar.non_loopback_warning("localhost") is None
    assert "unauthenticated" in serve_radar.non_loopback_warning("0.0.0.0")


def test_port_collision_and_held_lock_are_clean_failures(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(f"database_path: {tmp_path}/data/jobs.sqlite3\n")
    monkeypatch.chdir(tmp_path)
    env_dir = tmp_path / "e"
    env_dir.mkdir()
    e = Env(env_dir)
    try:
        assert serve_radar.main(["--port", str(e.port), "--host", "127.0.0.1"]) == 2
        assert "cannot listen" in capsys.readouterr().err
    finally:
        e.close()
    with run_lock("radar-server"):
        assert serve_radar.main(["--port", "0"]) == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_serve_radar.py -q`
Expected: collection error (`ModuleNotFoundError: No module named 'serve_radar'`).

- [ ] **Step 4: Implement `scripts/serve_radar.py`**

```python
#!/usr/bin/env python3
"""Serve the radar report live on localhost: fresh HTML on every load, feedback clicks saved to
SQLite immediately (with stale-write protection), change polling. Opt-in; the static
`render_radar.py` file:// report is unchanged. See docs/live-radar-dashboard-plan.md.

Unauthenticated by design: it binds to loopback by default and rejects foreign Host/Origin
headers, which guards against accidental cross-origin/DNS-rebinding use — not against anyone
who can reach a non-loopback bind. Never writes data/radar/ and serves nothing from disk.

Usage:
    uv run python scripts/serve_radar.py                          # newest archive, http://127.0.0.1:8765/
    uv run python scripts/serve_radar.py --keyword "adas" --open
    uv run python scripts/serve_radar.py --search data/searches/default_2026-09-26.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sqlite3
import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import render_radar
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from job_hunter.config import CandidateProfile, Settings, load_profile, load_settings
from job_hunter.models import FeedbackLabel, JobFeedback
from job_hunter.rootutil import add_project_argument, chdir_to_project_root
from job_hunter.runlock import RunLockHeld, run_lock
from job_hunter.search_archive import resolve_search_path
from job_hunter.storage import Storage

MAX_BODY_BYTES = 64 * 1024
MAX_DRAIN_BYTES = 1024 * 1024  # read-and-discard cap so early rejections never leave unread bytes
FUTURE_TOLERANCE = timedelta(seconds=5)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WILDCARD_HOSTS = {"0.0.0.0", "::"}


@dataclass
class ServerConfig:
    args: argparse.Namespace
    settings: Settings
    profile_loader: Callable[[], CandidateProfile | None]
    extra_hosts: frozenset[str] = field(default_factory=frozenset)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    render_radar.add_selection_arguments(parser)
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to bind (default 8765; 0 = any free port)")
    parser.add_argument("--open", action="store_true", help="open the page in your browser once listening")
    parser.add_argument(
        "--allowed-host", action="append", default=[],
        help="extra Host header value to accept (repeatable; only needed for a non-loopback bind)",
    )
    add_project_argument(parser)
    return parser


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def allowed_hosts(host: str, port: int, extra: Iterable[str] = ()) -> frozenset[str]:
    """Host header values this server accepts. Loopback and wildcard binds accept the loopback
    names; a specific non-loopback bind accepts only itself. Wildcard binds need `--allowed-host`
    for any non-loopback name — never arbitrary aliases (DNS-rebinding guard)."""
    allowed = {h.lower() for h in extra}
    if host in _LOOPBACK_HOSTS or host in _WILDCARD_HOSTS:
        allowed |= {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    else:
        allowed.add(f"{_bracket(host)}:{port}".lower())
    return frozenset(allowed)


def non_loopback_warning(host: str) -> str | None:
    if host in _LOOPBACK_HOSTS:
        return None
    return (
        f"WARNING: binding to {host} exposes an unauthenticated API that can change your feedback "
        "database to anyone who can reach this address. Use 127.0.0.1 unless you understand that."
    )


class FeedbackWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=500)
    label: FeedbackLabel | None
    client_ts: datetime

    @field_validator("source_key", "job_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("client_ts")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("client_ts must include a UTC offset")
        return value.astimezone(UTC)


def _public_feedback(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in ("source_key", "job_id", "label", "recorded_at")}


def write_feedback(storage: Storage, req: FeedbackWrite, *, now: datetime) -> tuple[int, dict[str, Any]]:
    """One validated feedback write. Job facts come from the `jobs`/`assessments` tables, never
    from the client. Last-writer-wins by `client_ts` (clamped to now): a late retry that lost the
    race returns 200 `stale` with the current state so the client converges instead of retrying."""
    if req.client_ts > now + FUTURE_TOLERANCE:
        return 400, {"ok": False, "error": "client_ts is in the future"}
    event_at = min(req.client_ts, now)
    key = (req.source_key, req.job_id)
    existing = storage.get_job_feedback(*key)
    snapshot = storage.get_job_snapshot(*key)
    if req.label is None:
        if existing is None and snapshot is None:
            return 404, {"ok": False, "error": "unknown job"}
        outcome = storage.delete_feedback(*key, event_at=event_at)
    else:
        if snapshot is None:
            return 404, {"ok": False, "error": "unknown job"}
        feedback = JobFeedback(
            source_key=req.source_key, job_id=req.job_id, company=snapshot["company"],
            title=snapshot["title"], department=snapshot["department"],
            score=storage.get_assessment_score(*key), label=req.label, recorded_at=event_at,
        )
        outcome = storage.apply_feedback(feedback, event_at=event_at)
    current = _public_feedback(storage.get_job_feedback(*key))
    if outcome == "stale":
        return 200, {"ok": True, "stale": True, "item": current}
    if req.label is None:
        return 200, {"ok": True, "deleted": True}
    return 200, {"ok": True, "item": current}


def _file_version(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}-{stat.st_size}"


def _feedback_version(rows: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(rows, key=lambda r: (r["source_key"], r["job_id"])),
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _resolve_archive(args: argparse.Namespace) -> Path:
    return resolve_search_path(search=args.search, keyword=args.keyword, companies=args.companies)


def _versions(cfg: ServerConfig, rows: list[dict[str, Any]], archive: Path | None) -> dict[str, str | None]:
    return {
        "archive": _file_version(archive),
        "assessments": _file_version(cfg.args.assessments),
        "feedback": _feedback_version(rows),
    }


def render_page(cfg: ServerConfig) -> tuple[str, Path]:
    archive = _resolve_archive(cfg.args)
    with Storage(cfg.settings.database_path) as storage:
        rows = storage.export_job_feedback()
        feedback = storage.feedback_map()
    state = render_radar.LiveState(
        feedback=feedback, versions=_versions(cfg, rows, archive), archive_name=archive.name
    )
    html, _ = render_radar.render(
        search_path=archive, assessments_path=cfg.args.assessments, live=True, live_state=state,
        **render_radar.selection_render_kwargs(cfg.args, cfg.settings, cfg.profile_loader()),
    )
    return html, archive


class RadarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        if ":" in cfg.args.host:
            self.address_family = socket.AF_INET6
        super().__init__((cfg.args.host, cfg.args.port), RadarHandler)
        self.allowed = allowed_hosts(cfg.args.host, self.server_address[1], cfg.extra_hosts)
        self._announced: Path | None = None
        self._announce_lock = threading.Lock()

    def note_archive(self, archive: Path) -> None:
        """Print the resolved archive and its attempted-source count whenever it changes
        (CLAUDE.md: mtime "newest" can silently pick a narrow archive — make it visible)."""
        with self._announce_lock:
            if archive == self._announced:
                return
            self._announced = archive
        try:
            attempted = json.loads(archive.read_text(encoding="utf-8")).get("summary", {}).get("sources_attempted")
        except (OSError, ValueError):
            attempted = None
        print(f"Serving archive {archive} ({attempted if attempted is not None else '?'} sources attempted)", flush=True)


class RadarHandler(BaseHTTPRequestHandler):
    server: RadarServer  # type: ignore[assignment]
    server_version = "job-hunter-radar"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write("radar-server: " + (format % args) + "\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _guard_host(self) -> bool:
        if (self.headers.get("Host") or "").lower() not in self.server.allowed:
            self._json(403, {"ok": False, "error": "invalid host"})
            return False
        return True

    def _fail(self, exc: Exception) -> None:
        if isinstance(exc, FileNotFoundError):
            self._json(503, {"ok": False, "error": str(exc)})
        elif isinstance(exc, sqlite3.OperationalError):
            self._json(503, {"ok": False, "error": f"database busy or unavailable: {exc}"})
        else:
            traceback.print_exc()
            self._json(500, {"ok": False, "error": "internal error"})

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard_host():
            return
        path = self.path.split("?", 1)[0]
        cfg = self.server.cfg
        try:
            if path == "/":
                html, archive = render_page(cfg)
                self.server.note_archive(archive)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    archive = _resolve_archive(cfg.args)
                except FileNotFoundError:
                    archive = None
                with Storage(cfg.settings.database_path) as storage:
                    rows = storage.export_job_feedback()
                self._json(200, {
                    "ok": True, "versions": _versions(cfg, rows, archive),
                    "counts": {"feedback": len(rows)},
                    "archive_name": archive.name if archive else None,
                })
            elif path == "/api/feedback":
                with Storage(cfg.settings.database_path) as storage:
                    mapping = storage.feedback_map()
                self._json(200, {"ok": True, "feedback": {k: _public_feedback(v) for k, v in mapping.items()}})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001 - one bad request must never stop the server
            self._fail(exc)

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True
        header = self.headers.get("Content-Length")
        try:
            declared = int(header) if header is not None else None
        except ValueError:
            declared = -1
        # Drain a bounded amount of the body before ANY response: replying to a request whose
        # body is still unread can make the client see a connection reset instead of our 4xx.
        body = self.rfile.read(min(declared, MAX_DRAIN_BYTES)) if declared and declared > 0 else b""
        if not self._guard_host():
            return
        if self.path.split("?", 1)[0] != "/api/feedback":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self._json(415, {"ok": False, "error": "Content-Type must be application/json"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() != f"http://{(self.headers.get('Host') or '').lower()}":
            self._json(403, {"ok": False, "error": "foreign origin"})
            return
        if declared is None:
            self._json(411, {"ok": False, "error": "Content-Length required"})
            return
        if declared < 0:
            self._json(400, {"ok": False, "error": "invalid Content-Length"})
            return
        if declared > MAX_BODY_BYTES:  # checked before anything is parsed
            self._json(413, {"ok": False, "error": "request body too large"})
            return
        try:
            req = FeedbackWrite.model_validate(json.loads(body))
        except (ValueError, UnicodeDecodeError) as exc:
            detail = (
                json.loads(exc.json(include_url=False, include_context=False, include_input=False))
                if isinstance(exc, ValidationError)
                else "invalid JSON"
            )
            self._json(400, {"ok": False, "error": "invalid request", "details": detail})
            return
        try:
            with Storage(self.server.cfg.settings.database_path) as storage:
                status, response = write_feedback(storage, req, now=datetime.now(UTC))
            self._json(status, response)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        if self._guard_host():
            self._json(405, {"ok": False, "error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chdir_to_project_root(args.project)
    settings = load_settings()
    cfg = ServerConfig(
        args=args, settings=settings, profile_loader=load_profile,
        extra_hosts=frozenset(h.lower() for h in args.allowed_host),
    )
    warning = non_loopback_warning(args.host)
    if warning:
        print(warning, file=sys.stderr)
    try:
        with run_lock("radar-server"):
            Storage(settings.database_path).close()  # migrate once, at startup
            try:
                server = RadarServer(cfg)
            except OSError as exc:
                print(f"job-hunter: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
                return 2
            try:
                server.note_archive(_resolve_archive(args))
            except FileNotFoundError as exc:
                print(f"job-hunter: no archive yet ({exc}); the page will answer 503 until one exists.", file=sys.stderr)
            url = f"http://{args.host}:{server.server_address[1]}/"
            print(f"Serving live radar at {url} (Ctrl+C to stop)", flush=True)
            if args.open:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nStopping.")
            finally:
                server.server_close()
    except RunLockHeld as exc:
        print(f"job-hunter: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_serve_radar.py tests/test_render_radar.py tests/test_storage.py tests/test_apply_radar_feedback.py -q && uv run ruff check scripts tests src`
Expected: PASS, ruff clean. If a test hangs, a handler is not closing the connection: confirm `close_connection = True` on POST and that every path returns a body with `Content-Length`. (`ValidationError` subclasses `ValueError` in Pydantic 2, so the single `except (ValueError, UnicodeDecodeError)` covers bad JSON and bad schema; the `isinstance` branch picks the right detail.)

- [ ] **Step 6: Commit**

```bash
git add scripts/render_radar.py scripts/serve_radar.py tests/test_serve_radar.py
git commit -m "feat(radar): opt-in live server with SQLite feedback writes" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs, skill versions, full verification, browser smoke

**Files:**
- Modify: `docs/SPEC.md` (§8.5, §8.6, §10), `README.md`, `CLAUDE.md`, `skills/job-radar/SKILL.md`, `skills/job-feedback/SKILL.md`, `docs/live-radar-dashboard-plan.md` (two consistency fixes)

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Update `docs/SPEC.md`**

- §8.5 (`job_feedback`): append a paragraph: *"Since the live radar, `job_feedback` is also written by `scripts/serve_radar.py` (`POST /api/feedback`). Writes are last-writer-wins by **event time** through `Storage.apply_feedback`/`delete_feedback`: a write applies only if strictly newer than the stored label's `recorded_at` and any row in `feedback_tombstones` (migration v3; one row per untagged job, retained, cleared only by a newer label). `apply_radar_feedback.py` uses the export file's mtime as the event time, so an old export can neither overwrite a newer live label nor resurrect an untagged job; skipped entries are reported as `stale-skipped` (only when nonzero). Live saves do not refresh `data/job_feedback.json`/`.csv` — run `job-hunter export-feedback` or `apply_radar_feedback.py` for those; `suggest_exclusions.py` reads the table directly."*
- §8.6 (cleanup): add *"`feedback_tombstones` rows are not deleted by cleanup."*
- §10 (Scripts): add `serve_radar.py` — one line: opt-in localhost live radar (GET `/`, `/api/state`, `/api/feedback`; POST `/api/feedback`); loopback by default; unauthenticated.

- [ ] **Step 2: Update `README.md` and `CLAUDE.md`**

- `README.md` (the feedback paragraph near the `apply_radar_feedback.py` example, ~line 139): add a short "Live mode" note: `uv run python scripts/serve_radar.py --open` serves the radar with click-to-save feedback (SQLite), extra filters (min score, posted-within, company, location, has-salary, feedback state, sort), and "New results — Reload" polling; static `render_radar.py` output and Export Feedback are unchanged; loopback-only by default.
- `CLAUDE.md`: in the `storage.py` bullet, after the `job_feedback` description, add one sentence on tombstones + event-time writes; add a `scripts/serve_radar.py` bullet under Architecture (what it serves, per-request `Storage`, `run_lock("radar-server")`, writes validated against `jobs`, never renders on the write path, `--project` supported); add to the "Safe testing" list: *"`serve_radar.py` tests/smoke runs use a temp project (`--project`); never point one at the real `data/` for experiments."* Add `apply_radar_feedback.py` note: *"uses the export mtime as event time (stale-skipped)."*

- [ ] **Step 3: Update the two skills and bump versions**

- `skills/job-radar/SKILL.md`: frontmatter `version: 1.2.0` → `1.3.0`. After the step that renders the report with `render_radar.py`, add: *"**Live option:** for interactive triage, `uv run python scripts/serve_radar.py --project "$CLAUDE_PROJECT_DIR" --open` serves the same report with click-to-save feedback, extra filters, and change polling. It is opt-in, loopback-only by default, and never writes `data/radar/`; the static report remains the deliverable."* (Find the spot with `grep -n "render_radar" skills/job-radar/SKILL.md`.)
- `skills/job-feedback/SKILL.md`: `version: 1.1.0` → `1.2.0`. At the step that runs `apply_radar_feedback.py` (line ~67), add: *"If feedback was tagged in the live radar it is already in SQLite; importing an older exported file is safe — entries older than a live change are skipped and reported as `stale-skipped`."*
- Confirm with `grep -n "^version:" skills/*/SKILL.md` that only these two changed.

- [ ] **Step 4: Fix two spec details in `docs/live-radar-dashboard-plan.md`**

- §4.3 item 5: replace "each tier count shows "visible/total" while any filter is active" with "each tier count appends ` · N shown` while any filter is active".
- §5.2 route table: `GET /api/feedback` returns `{"ok": true, "feedback": {key: {source_key, job_id, label, recorded_at}}}`; the POST stale reply is `{ok: true, stale: true, item: <row|null>}` (`null` = currently untagged).

- [ ] **Step 5: Full verification**

Run:

```bash
uv run ruff check .
uv run pytest -q
node --test tests/js/radar_live_core.test.js
```

Expected: ruff clean; pytest = baseline 2 failures (`test_collector.py` x2) plus **zero new failures** (previous 467 passed + the new tests); node tests pass. Report the 2 baseline failures separately as pre-existing.

- [ ] **Step 6: Browser smoke on a temp project (never the real `data/`)**

```bash
TMP=$(mktemp -d) && cp -R pyproject.toml config "$TMP"/ && mkdir -p "$TMP/data/searches" && ls -la "$TMP"/data
uv run python - "$TMP" <<'PY'
import json, sys
from pathlib import Path
from job_hunter.models import Assessment, Job, LocationConfidence
from job_hunter.storage import Storage
root = Path(sys.argv[1])
cands, assess = [], []
with Storage(root / "data" / "jobs.sqlite3") as s:
    for i, (title, score) in enumerate([("ADAS Engineer", 88), ("Perception Lead", 72), ("Sim Engineer", 55), ("HR Analyst", 30)], 1):
        j = Job(source_key="acme", source_platform="t", company=f"Co{i % 2}", job_id=str(i), title=title,
                url=f"https://example.com/{i}", us_eligible=True, location_confidence=LocationConfidence.HIGH,
                description="d", content_hash="h")
        s.upsert_job(j)
        s.upsert_assessment(Assessment(source_key="acme", job_id=str(i), company=j.company, title=title,
            url=j.url, content_hash="h", score=score, recommended=score >= 75, matches=["m"], gaps=["g"]))
        cands.append({"source_key": "acme", "job_id": str(i), "posted_at": f"2026-09-{10 + i}T00:00:00Z",
            "location_raw": "Detroit, MI", "state": "MI", "country": "US", "visa_sponsorship": "unmentioned",
            "work_arrangement": "unknown", "company": j.company, "title": title, "url": j.url,
            "salary_evidence": "$100,000 - $120,000" if i == 1 else None})
        assess.append({"source_key": "acme", "job_id": str(i), "score": score, "company": j.company,
            "title": title, "url": j.url, "matches": ["m"], "gaps": ["g"]})
(root / "data" / "searches" / "default_2026-09-26.json").write_text(json.dumps(
    {"summary": {"sources_attempted": 1, "sources_succeeded": 1}, "candidates": cands, "source_health": []}))
(root / "data" / "assessments.json").write_text(json.dumps(assess))
PY
uv run python scripts/serve_radar.py --project "$TMP" --port 8765 --open
```

Expected in the terminal: `Serving archive .../default_2026-09-26.json (1 sources attempted)` then `Serving live radar at http://127.0.0.1:8765/`. In the browser check each, ticking it off:

- [ ] Page loads with 👍/🆗/👎 buttons, a **Live** pill (bottom right), the extended toolbar, no "Export Feedback" button.
- [ ] Click 👍 on a row → pill flashes "Saving…" then "Live"; **reload** keeps the tag; `sqlite3 "$TMP/data/jobs.sqlite3" "select source_key,job_id,label from job_feedback"` shows the row (company/title/score server-derived).
- [ ] **Two tabs:** tag in tab A; switch to tab B → it shows the tag within seconds (focus reconcile). Untag in B; A converges.
- [ ] Filters: min score 70 hides the two low rows and the count reads "Showing 2 of 4"; posted-within, company, location `mi`, has-salary, feedback=untagged all narrow correctly; sort=newest reorders and updates the tier note; reload restores the filters from the URL hash; "Clear filters" resets them.
- [ ] **Offline:** stop the server (Ctrl+C), click 🆗 → pill reads "Offline (1 unsaved)"; restart the server; within ~30 s (or on tab focus) it flushes and reads "Live"; the row is in SQLite.
- [ ] **Stale protection:** `uv run python scripts/apply_radar_feedback.py --project "$TMP" --file <an export with an older mtime>` (or `touch -t 202001010000` on a hand-made export JSON) does not overwrite the live label; output shows `stale-skipped`.
- [ ] **New results:** `touch "$TMP"/data/searches/default_2026-09-26.json` → within ~10 s the "New results — Reload" banner appears; it never auto-reloads.
- [ ] **Static report unchanged:** `uv run python scripts/render_radar.py --project "$TMP" --output "$TMP/static.html"` produces a page with Export Feedback and no live pill; open it via `file://` and tag a row (localStorage path still works).

Then remove the temp project: `rm -rf "$TMP"`. Record the checklist outcome in the PR description.

- [ ] **Step 7: Commit**

```bash
git add docs/SPEC.md README.md CLAUDE.md skills/job-radar/SKILL.md skills/job-feedback/SKILL.md docs/live-radar-dashboard-plan.md
git commit -m "docs: live radar (Phase A) — SPEC, README, CLAUDE.md, skill versions" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review (spec coverage, done inline)

- **Spec §3.1 models (Phase A):** `FeedbackLabel` → Task 1. **§3.2 v3 migration:** Task 1. **§3.3 storage API (Phase A):** Task 1 (`get_job_feedback`, `feedback_map`, guarded writes, `delete_feedback`); the spec's `delete_job_feedback` name is realized as `delete_feedback` (spec updated implicitly — the interface is defined here; Task 7 Step 4 edits the two spec details; also rename the spec's `delete_job_feedback`/`upsert_job_feedback` mentions if you want them literally aligned).
- **§3.4 stale-write rule:** strictly-newer, future clamp/reject, mtime import, `stale-skipped`, `recorded_at` = event time → Tasks 1, 2, 6.
- **§4.1 render split, live attrs, no title/company attrs, initial feedback, escaping, static unchanged:** Task 5 (golden test).
- **§4.2 template (Phase A parts):** status pill, footer archive line, live toolbar, static feedback script/Export removed → Task 5; Track chip/Applications page are Phase B.
- **§4.3 client rules:** outbox coalescing/permanent drop/stale reply → Tasks 3–4; reconcile/poll/no auto-reload/hash/sessionStorage groups/sort/counts → Task 4; verified in Task 7 smoke.
- **§5 server:** threaded server, lock, per-request storage, routes, request models, Host/Origin/Content-Type/size checks, versions, `--project`, archive announcement, non-loopback warning, port collision → Task 6. (`GET /applications`, `/api/applications`, `POST /api/application` are Phase B; tests assert `/applications` is 404 for now.)
- **§5.4 exports:** Phase B. **§6 docs:** Task 7. **§8 tests:** each task; node tests Task 3; browser smoke Task 7.
- **Placeholder scan:** none ("verbatim" in Task 6 Step 1 refers to moving existing code that the implementer has in front of them, with the exact destination function shown).
- **Type consistency:** `apply_feedback(feedback, *, event_at, force=False)`, `delete_feedback(source_key, job_id, *, event_at)`, `get_job_feedback`, `feedback_map`, `get_job_snapshot`, `get_assessment_score`, `LiveState(feedback, versions, archive_name)`, `render(...)->(html, stats)`, `build(*, output_path, **kwargs)`, `FeedbackWrite`, `write_feedback(storage, req, *, now)` are used identically in Tasks 1, 2, 5, 6.
