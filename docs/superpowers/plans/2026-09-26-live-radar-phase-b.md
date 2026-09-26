# Live Radar — Phase B (application tracking) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the live radar track applications per collected job (saved → applied → interviewing → offer / rejected / withdrawn), show them on a `/applications` page, filter the radar by application state, and export them to JSON/CSV — all persisted in SQLite and surviving `cleanup` and archive changes.

**Architecture:** Phase A's event-time (last-writer-wins) write rule is reused for a new `applications` table (+ `application_tombstones`). The client outbox/retry engine is extracted from `radar_live_ui.js` into a small, node-testable `radar_live_sync.js` so the radar page and the new Applications page share one implementation. The server gains `POST /api/application`, `GET /api/applications`, `GET /applications`; every UI edit sends the **full current record** (status/date/notes) so outbox coalescing (latest write per job wins) can never lose a field.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `http.server`/`sqlite3`/`csv`, vanilla JS, pytest, node (optional; JS tests skip without it).

**Spec:** `docs/live-radar-dashboard-plan.md` (revision 2 — read §3.1–3.4 (application parts), §4.2, §5, §5.4 first). This plan implements its **Phase B**. Phase A is already merged (commit `1716c61`); read `docs/superpowers/plans/2026-09-26-live-radar-phase-a.md` for the conventions Phase B extends.

**Before starting:** this branch (`live-radar-phase-b`) is cut from `dev` at `1716c61`. Commit this plan on it first.

## Global Constraints

- Python `>=3.11`; **no new runtime dependency**. `uv run ruff check .` must stay clean (baseline: clean).
- Baseline test status before Phase B: **512 passed, 2 failed** (`tests/test_collector.py::test_no_default_cap_without_keywords`, `::test_keyword_search_overrides_profile_terms` — pre-existing, unrelated; report separately, never "fix" here).
- **Static report output must stay byte-identical** (`render_radar.py` with `live=False`): the golden test `tests/test_render_radar.py::test_static_render_is_byte_identical_to_the_pre_live_golden` must pass **unmodified**. Every new row/toolbar/style/script fragment is emitted only when `live=True`; new f-string slots in the row builders must be **inline with no added whitespace** (`{feedback_buttons}{app_chip}` on one line) so static output is unchanged.
- **Never put company or title into row-root `data-*` attributes** (documented in `_filter_data_attrs`). Application state on a row root is only `data-app-status`.
- `Storage` migrations are append-only: add `_migrate_v4_...` after v3; never renumber.
- Client-supplied `company`/`title`/`url`/`location`/`posted_at`/`score`/`salary_evidence` is never persisted: the server derives the snapshot from `jobs`/`assessments` at first creation. Request models use `extra="forbid"`.
- Application invariants (enforced by `models.Application` and `Storage.apply_application`): `saved` ⇒ `applied_at` is NULL; first move to a non-`saved` status defaults `applied_at` to the caller-supplied local `today`; an explicit `applied_at` must be a real date; `applied_at` cannot be cleared (move back to `saved` instead); `notes` ≤ 4000 characters; snapshot fields never change after creation.
- Last-writer-wins by **event time** for applications too: a write applies only if `event_at` is strictly newer than the row's `updated_at` and any `application_tombstones.deleted_at`; a stale write returns `200 {ok:true, stale:true, item:<row|null>}`.
- **Never write to the real `data/`** from tests or smoke runs: use `tmp_path` / a temp project. Server tests bind port 0; none may depend on `$JOB_HUNTER_ROOT` (pass `--project` or `delenv`).
- CSV export: any string cell starting with `=`, `+`, `-`, `@`, tab or CR is prefixed with `'` (spreadsheet formula guard).
- An export-file refresh failure after a committed write must **not** fail or revert the write: return 200 with `export_warning`.
- Any changed `skills/*/SKILL.md` must bump its frontmatter `version` (minor for new capability).
- End every commit message with the line `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (use a second `-m`). Stage files by name; never `git add -A` (an untracked `.superpowers/` dir exists and must not be committed).
- Tests run with `uv run pytest`; none may touch the network. JS tests: `node --test tests/js/<file>.test.js` (pytest wrappers skip when node is absent).

## Review Focus

Failure modes the spec implies that a user is most likely to hit (each is pinned by a test in the named task):

1. **A tracked application must survive** `job-hunter cleanup` (job row deleted), an archive that no longer lists the job, and show as "removed" on `/applications` — Tasks 1, 7, 8.
2. **Two quick edits to one job** (status change, then notes change) or a **late offline retry** must not lose a field or overwrite a newer edit — Task 1 (event-time rule), Task 3 (outbox coalescing), Task 8 (`stale`).
3. **Delete then a late write** must not resurrect the application — Task 1 (`application_tombstones`), Task 8.
4. **Hostile text** in notes/titles (`</script>`, quotes, `=HYPERLINK(...)`) must be inert on the page, in embedded JSON, and in the CSV export — Tasks 2, 7, 8.
5. **Export files unwritable/stale** must never fail or revert a save, and repeated exports must be identical — Tasks 2, 8.

## File Structure

| File | Responsibility |
|---|---|
| `src/job_hunter/models.py` (modify) | `ApplicationStatus`, `Application` (invariants). |
| `src/job_hunter/storage.py` (modify) | v4 migration; `apply_application`, reads, `job_statuses`, tombstone readers. |
| `src/job_hunter/applications_export.py` (create) | Fixed CSV columns, formula guard, JSON/CSV writers (shared by CLI + server). |
| `src/job_hunter/cli.py` (modify) | `export-applications`. |
| `scripts/templates/radar_live_sync.js` (create) | Outbox + retry engine (`createSync`), DOM-free, node-testable. |
| `scripts/templates/radar_live_core.js` (modify) | Application filters (`app`, `hideApplied`), `reconcileApps`. |
| `scripts/templates/radar_live_ui.js` (modify) | Use the sync engine; Track chip/panel wiring; application filters/polling. |
| `scripts/templates/applications_ui.js` (create) | Applications page behavior. |
| `scripts/render_radar.py` (modify) | `LiveState.applications`; chip/panel/toolbar/link/style; boot JSON. |
| `scripts/render_applications.py` + `scripts/templates/applications_template.html` (create) | Applications page renderer. |
| `scripts/serve_radar.py` (modify) | Application routes, exports refresh, versions, idempotent untag, POST timeout. |
| `tests/test_storage.py`, `tests/test_applications_export.py`, `tests/test_cli.py`, `tests/js/*.test.js`, `tests/test_render_radar.py`, `tests/test_render_applications.py`, `tests/test_serve_radar.py` | Tests. |
| `docs/SPEC.md`, `README.md`, `CLAUDE.md`, `skills/job-radar/SKILL.md`, `docs/live-radar-dashboard-plan.md` | Docs + skill version bump. |

---

### Task 1: Models, v4 migration, application storage

**Files:**
- Modify: `src/job_hunter/models.py` (add after `JobFeedback`; extend imports)
- Modify: `src/job_hunter/storage.py` (migration after `_migrate_v3_...`; append to `_MIGRATIONS`; methods after `get_assessment_score`)
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: Phase A `Storage` (`_parse_utc`, `get_job_snapshot`, `get_assessment_score`, `delete_closed_jobs`), `models.utcnow`.
- Produces (later tasks rely on these exact names):
  - `models.ApplicationStatus` (StrEnum: `saved, applied, interviewing, offer, rejected, withdrawn`), `models.Application` (fields: `source_key, job_id, status, applied_at: date|None, notes: str|None (≤4000), company, title, url, location, posted_at, score, salary_evidence, created_at, updated_at`).
  - `Storage.get_application(source_key, job_id) -> dict | None` (raw row; dates/timestamps are ISO strings)
  - `Storage.application_map() -> dict[str, dict]` keyed `"source_key|job_id"`
  - `Storage.export_applications() -> list[dict]` ordered `updated_at DESC`
  - `Storage.get_application_tombstone(source_key, job_id) -> str | None`, `Storage.get_feedback_tombstone(source_key, job_id) -> str | None`
  - `Storage.job_statuses(pairs: Iterable[tuple[str, str]]) -> dict[str, str]` (`"source|job"` → `jobs.status`; absent when the job row is gone)
  - `Storage.apply_application(source_key, job_id, *, event_at, changes, snapshot, today) -> tuple[Literal["applied","deleted","stale"], dict | None]` where `changes` maps any of `"status"` (`ApplicationStatus | None`; `None` = delete), `"applied_at"` (`date | None`), `"notes"` (`str | None`) — **an absent key means "unchanged"**; `snapshot` is `None` or a dict with keys `company, title, url, location_raw, posted_at, score, salary_evidence` (used only on creation); `today` is the caller's local `date`. Raises `ValueError` for invalid input (including pydantic `ValidationError`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py` (add `from datetime import date` to the datetime import at the top, and `from job_hunter.models import Application, ApplicationStatus` to the models import):

```python
# --- live-radar application tracking (Phase B) ---

TODAY = date(2026, 9, 26)


def _snap(**over):
    values = {
        "company": "Acme", "title": "Engineer", "url": "https://example.com/42",
        "location_raw": "Detroit, MI", "posted_at": "2026-09-20T00:00:00Z", "score": 82,
        "salary_evidence": "$100,000 - $120,000",
    }
    values.update(over)
    return values


def _apply(storage, minutes, changes, snapshot="default", key=("acme", "42")):
    return storage.apply_application(
        *key, event_at=_t(minutes), changes=changes,
        snapshot=_snap() if snapshot == "default" else snapshot, today=TODAY,
    )


def test_application_model_enforces_invariants():
    base = dict(
        source_key="a", job_id="1", status="saved", company="Acme", title="T", url="https://x",
        created_at=_t(0), updated_at=_t(0),
    )
    assert Application(**base).applied_at is None
    with pytest.raises(ValidationError):
        Application(**{**base, "applied_at": date(2026, 9, 1)})  # saved cannot have a date
    with pytest.raises(ValidationError):
        Application(**{**base, "status": "hired"})
    with pytest.raises(ValidationError):
        Application(**{**base, "notes": "x" * 4001})
    with pytest.raises(ValidationError):
        Application(**{**base, "title": "  "})


def test_pre_v4_database_gains_the_application_tables_on_open(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.connection.execute("DROP TABLE applications")
        storage.connection.execute("DROP TABLE application_tombstones")
        storage.connection.execute("PRAGMA user_version = 3")
        storage.connection.commit()
    with Storage(db_path) as storage:
        for table in ("applications", "application_tombstones"):
            assert storage.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == len(
            storage_module._MIGRATIONS
        )


def test_first_write_creates_a_row_with_a_server_snapshot(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        outcome, row = _apply(storage, 0, {"status": ApplicationStatus.SAVED})
        assert outcome == "applied"
        assert (row["status"], row["applied_at"], row["notes"]) == ("saved", None, None)
        assert (row["company"], row["title"], row["url"], row["score"]) == (
            "Acme", "Engineer", "https://example.com/42", 82,
        )
        assert row["location"] == "Detroit, MI" and row["salary_evidence"] == "$100,000 - $120,000"
        assert row["created_at"] == row["updated_at"] == _t(0).isoformat()
        assert storage.get_application("acme", "42") == row


def test_creating_requires_a_status_and_a_known_job(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        with pytest.raises(ValueError, match="status"):
            _apply(storage, 0, {"notes": "hello"})
        with pytest.raises(ValueError, match="unknown job"):
            _apply(storage, 0, {"status": ApplicationStatus.SAVED}, snapshot=None)


def test_applied_date_rules(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 0, {"status": ApplicationStatus.SAVED})
        # first move to a non-saved status defaults to the caller's local today
        _, row = _apply(storage, 1, {"status": ApplicationStatus.APPLIED})
        assert row["applied_at"] == "2026-09-26"
        # a later status keeps the existing date
        _, row = _apply(storage, 2, {"status": ApplicationStatus.INTERVIEWING})
        assert row["applied_at"] == "2026-09-26"
        # an explicit date wins
        _, row = _apply(storage, 3, {"applied_at": date(2026, 9, 1)})
        assert (row["status"], row["applied_at"]) == ("interviewing", "2026-09-01")
        # back to saved clears the date
        _, row = _apply(storage, 4, {"status": ApplicationStatus.SAVED})
        assert row["applied_at"] is None
        # saved cannot take an explicit date, and a date can never be cleared explicitly
        with pytest.raises(ValueError, match="saved"):
            _apply(storage, 5, {"applied_at": date(2026, 9, 2)})
        _apply(storage, 6, {"status": ApplicationStatus.APPLIED})
        with pytest.raises(ValueError, match="cleared"):
            _apply(storage, 7, {"applied_at": None})


def test_partial_updates_change_only_the_named_fields_and_never_the_snapshot(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 0, {"status": ApplicationStatus.APPLIED, "notes": "first"})
        _, row = _apply(storage, 1, {"notes": "second"}, snapshot=_snap(company="Changed", score=1))
        assert (row["status"], row["notes"]) == ("applied", "second")
        assert (row["company"], row["score"]) == ("Acme", 82)  # snapshot is immutable
        assert row["created_at"] == _t(0).isoformat() and row["updated_at"] == _t(1).isoformat()
        _, row = _apply(storage, 2, {"notes": None})
        assert row["notes"] is None and row["status"] == "applied"


def test_notes_are_capped(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        with pytest.raises(ValueError):
            _apply(storage, 0, {"status": ApplicationStatus.SAVED, "notes": "x" * 4001})
        assert storage.get_application("acme", "42") is None  # nothing half-written


def test_stale_writes_are_rejected_and_return_the_current_row(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 10, {"status": ApplicationStatus.APPLIED})
        outcome, row = _apply(storage, 10, {"status": ApplicationStatus.OFFER})  # equal → stale
        assert outcome == "stale" and row["status"] == "applied"
        outcome, row = _apply(storage, 5, {"notes": "late"})  # older → stale
        assert outcome == "stale" and row["notes"] is None
        outcome, row = _apply(storage, 11, {"status": ApplicationStatus.OFFER})
        assert outcome == "applied" and row["status"] == "offer"


def test_delete_leaves_a_tombstone_that_blocks_older_writes(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 0, {"status": ApplicationStatus.APPLIED, "notes": "keep?"})
        outcome, row = _apply(storage, 10, {"status": None})
        assert (outcome, row) == ("deleted", None)
        assert storage.get_application("acme", "42") is None
        assert storage.get_application_tombstone("acme", "42") == _t(10).isoformat()
        outcome, row = _apply(storage, 5, {"status": ApplicationStatus.APPLIED})
        assert (outcome, row) == ("stale", None)
        # a newer write recreates it (fresh snapshot, fresh created_at) and clears the tombstone
        outcome, row = _apply(storage, 20, {"status": ApplicationStatus.SAVED})
        assert outcome == "applied" and row["created_at"] == _t(20).isoformat()
        assert storage.get_application_tombstone("acme", "42") is None


def test_deleting_something_that_was_never_tracked_still_records_a_tombstone(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert _apply(storage, 10, {"status": None}, snapshot=None) == ("deleted", None)
        assert _apply(storage, 5, {"status": ApplicationStatus.SAVED})[0] == "stale"


def test_naive_event_time_is_rejected(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage, pytest.raises(ValueError):
        storage.apply_application(
            "acme", "42", event_at=datetime(2026, 9, 26, 12, 0),
            changes={"status": ApplicationStatus.SAVED}, snapshot=_snap(), today=TODAY,
        )


def test_application_map_export_order_and_job_statuses(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="a"))
        storage.upsert_job(make_job(job_id="b"))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='b'")
        storage.connection.commit()
        _apply(storage, 0, {"status": ApplicationStatus.SAVED}, key=("acme", "a"))
        _apply(storage, 5, {"status": ApplicationStatus.APPLIED}, key=("acme", "b"))
        _apply(storage, 9, {"status": ApplicationStatus.APPLIED}, key=("acme", "gone"))
        assert set(storage.application_map()) == {"acme|a", "acme|b", "acme|gone"}
        assert [r["job_id"] for r in storage.export_applications()] == ["gone", "b", "a"]
        statuses = storage.job_statuses([("acme", "a"), ("acme", "b"), ("acme", "gone")])
        assert statuses == {"acme|a": "active", "acme|b": "closed"}  # "gone" is absent


def test_feedback_tombstone_reader(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_feedback_tombstone("apple", "99") is None
        storage.delete_feedback("apple", "99", event_at=_t(3))
        assert storage.get_feedback_tombstone("apple", "99") == _t(3).isoformat()


def test_cleanup_never_touches_applications_or_their_tombstones(tmp_path):
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()
        _apply(storage, 0, {"status": ApplicationStatus.APPLIED}, key=("acme", "stale"))
        _apply(storage, 1, {"status": None}, key=("acme", "other"), snapshot=None)
        deleted = storage.delete_closed_jobs(now - timedelta(days=7))
        assert [j["job_id"] for j in deleted["jobs"]] == ["stale"]
        assert set(deleted) == {"jobs", "assessments", "job_feedback"}  # return shape unchanged
        assert storage.get_application("acme", "stale")["status"] == "applied"
        assert storage.get_application_tombstone("acme", "other") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -q`
Expected: the new tests FAIL (`ImportError: cannot import name 'Application'` at collection).

- [ ] **Step 3: Implement the models**

In `src/job_hunter/models.py`: change the datetime import to `from datetime import UTC, date, datetime` and the pydantic import to `from pydantic import BaseModel, Field, field_validator, model_validator`. Directly after `class JobFeedback`, add:

```python
class ApplicationStatus(StrEnum):
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Application(BaseModel):
    """A human-tracked application to one collected job. `company`/`title`/`url`/... are a
    snapshot taken once, when tracking starts, so the row stays readable after the posting closes
    or `cleanup` deletes the job. `applied_at` is a local calendar date; `saved` never has one."""

    source_key: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    status: ApplicationStatus
    applied_at: date | None = None
    notes: str | None = Field(default=None, max_length=4000)
    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    location: str | None = None
    posted_at: str | None = None
    score: int | None = None
    salary_evidence: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("source_key", "job_id", "company", "title", "url")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _saved_has_no_applied_date(self) -> "Application":
        if self.status is ApplicationStatus.SAVED and self.applied_at is not None:
            raise ValueError("a saved application cannot have an applied date")
        return self
```

- [ ] **Step 4: Implement the migration and storage methods**

In `src/job_hunter/storage.py`: extend the datetime import to `from datetime import UTC, date, datetime`, the models import with `Application, ApplicationStatus`, and add `_MISSING = object()` near `_parse_utc`. After `_migrate_v3_create_feedback_tombstones` add:

```python
def _migrate_v4_create_application_tables(connection: sqlite3.Connection) -> None:
    """`applications` (a human-tracked application per job; the job's facts are a snapshot, so
    there is deliberately no foreign key to `jobs` and `cleanup` can never delete a row) and
    `application_tombstones` (a deleted application's event time, so a late write cannot
    resurrect it — same rule as `feedback_tombstones`)."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS applications (
            source_key TEXT NOT NULL, job_id TEXT NOT NULL, status TEXT NOT NULL,
            applied_at TEXT, notes TEXT,
            company TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
            location TEXT, posted_at TEXT, score INTEGER, salary_evidence TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (source_key, job_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS application_tombstones (
            source_key TEXT NOT NULL, job_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
            PRIMARY KEY (source_key, job_id)
        )"""
    )
```

Append `_migrate_v4_create_application_tables,` to `_MIGRATIONS`. Add a module-level helper next to `_parse_utc`:

```python
def _resolve_applied_at(
    status: ApplicationStatus, existing: str | None, requested: Any, today: date
) -> date | None:
    """`requested` is `_MISSING` (caller said nothing), None (explicit clear) or a date."""
    if status is ApplicationStatus.SAVED:
        if requested is not _MISSING and requested is not None:
            raise ValueError("a saved application cannot have an applied date")
        return None
    if requested is None:
        raise ValueError("applied_at cannot be cleared; move the application back to 'saved' instead")
    if requested is not _MISSING:
        return requested
    return date.fromisoformat(existing) if existing else today
```

Add these methods to `Storage` after `get_assessment_score`:

```python
    def get_feedback_tombstone(self, source_key: str, job_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT deleted_at FROM feedback_tombstones WHERE source_key=? AND job_id=?",
            (source_key, job_id),
        ).fetchone()
        return row["deleted_at"] if row else None

    def get_application_tombstone(self, source_key: str, job_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT deleted_at FROM application_tombstones WHERE source_key=? AND job_id=?",
            (source_key, job_id),
        ).fetchone()
        return row["deleted_at"] if row else None

    def get_application(self, source_key: str, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM applications WHERE source_key=? AND job_id=?", (source_key, job_id)
        ).fetchone()
        return dict(row) if row else None

    def export_applications(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM applications ORDER BY updated_at DESC, source_key, job_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def application_map(self) -> dict[str, dict[str, Any]]:
        return {f"{r['source_key']}|{r['job_id']}": r for r in self.export_applications()}

    def job_statuses(self, pairs: Any) -> dict[str, str]:
        """`jobs.status` for each (source_key, job_id) that still has a job row; a pair whose
        job was deleted (cleanup) is simply absent — callers treat that as "removed"."""
        found: dict[str, str] = {}
        for source_key, job_id in pairs:
            row = self.connection.execute(
                "SELECT status FROM jobs WHERE source_key=? AND job_id=?", (source_key, job_id)
            ).fetchone()
            if row:
                found[f"{source_key}|{job_id}"] = row["status"]
        return found

    def apply_application(
        self, source_key: str, job_id: str, *, event_at: datetime,
        changes: dict[str, Any], snapshot: dict[str, Any] | None, today: date,
    ) -> tuple[Literal["applied", "deleted", "stale"], dict[str, Any] | None]:
        """One application write, last-writer-wins by event time (same rule as feedback): a
        write applies only if `event_at` is strictly newer than the row's `updated_at` and any
        tombstone. `changes` keys that are absent mean "unchanged"; `status: None` deletes.
        `snapshot` (company/title/url/location_raw/posted_at/score/salary_evidence) is used only
        when creating. Everything happens in one BEGIN IMMEDIATE transaction; any failure
        (including validation) rolls back and raises."""
        if event_at.tzinfo is None:
            raise ValueError("event_at must be timezone-aware")
        event_at = event_at.astimezone(UTC)
        key = (source_key, job_id)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_application(*key)
            times: list[datetime] = []
            if existing:
                times.append(_parse_utc(existing["updated_at"]))
            tomb = self.get_application_tombstone(*key)
            if tomb:
                times.append(_parse_utc(tomb))
            if times and event_at <= max(times):
                self.connection.rollback()
                return "stale", existing
            if "status" in changes and changes["status"] is None:
                self.connection.execute(
                    "DELETE FROM applications WHERE source_key=? AND job_id=?", key
                )
                self.connection.execute(
                    """INSERT INTO application_tombstones(source_key, job_id, deleted_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(source_key, job_id) DO UPDATE SET deleted_at=excluded.deleted_at""",
                    (*key, event_at.isoformat()),
                )
                self.connection.commit()
                return "deleted", None
            if existing is None:
                if changes.get("status") is None:
                    raise ValueError("status is required to start tracking a job")
                if snapshot is None:
                    raise ValueError("unknown job")
                base: dict[str, Any] = {
                    "source_key": source_key, "job_id": job_id, "company": snapshot["company"],
                    "title": snapshot["title"], "url": snapshot["url"],
                    "location": snapshot.get("location_raw"), "posted_at": snapshot.get("posted_at"),
                    "score": snapshot.get("score"), "salary_evidence": snapshot.get("salary_evidence"),
                    "status": None, "applied_at": None, "notes": None,
                    "created_at": event_at.isoformat(),
                }
            else:
                base = dict(existing)
            status = ApplicationStatus(changes.get("status", base["status"]))
            applied_at = _resolve_applied_at(
                status, base["applied_at"], changes.get("applied_at", _MISSING), today
            )
            notes = changes["notes"] if "notes" in changes else base["notes"]
            row = {
                **base, "status": status.value, "applied_at": applied_at.isoformat() if applied_at else None,
                "notes": notes, "updated_at": event_at.isoformat(),
            }
            Application.model_validate(row)  # invariants: blank fields, notes cap, saved => no date
            self.connection.execute(
                """INSERT INTO applications(source_key, job_id, status, applied_at, notes, company,
                   title, url, location, posted_at, score, salary_evidence, created_at, updated_at)
                   VALUES (:source_key, :job_id, :status, :applied_at, :notes, :company, :title,
                   :url, :location, :posted_at, :score, :salary_evidence, :created_at, :updated_at)
                   ON CONFLICT(source_key, job_id) DO UPDATE SET status=excluded.status,
                   applied_at=excluded.applied_at, notes=excluded.notes,
                   updated_at=excluded.updated_at""",
                row,
            )
            self.connection.execute(
                "DELETE FROM application_tombstones WHERE source_key=? AND job_id=?", key
            )
            self.connection.commit()
            return "applied", row
        except BaseException:
            self.connection.rollback()
            raise
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage.py tests/test_cleanup.py -q && uv run ruff check src tests`
Expected: PASS; ruff clean. (`test_fresh_database_ends_up_at_the_current_schema_version` uses `len(_MIGRATIONS)` and adapts. `Application.model_validate(row)` accepts ISO strings for the date/datetime fields; `status` is validated as a real enum member.)

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/storage.py tests/test_storage.py
git commit -m "feat(storage): application tracking with event-time writes and tombstones" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Export helper and `export-applications` CLI

**Files:**
- Create: `src/job_hunter/applications_export.py`
- Modify: `src/job_hunter/cli.py`
- Test: `tests/test_applications_export.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Storage.export_applications()` (Task 1), `job_hunter.atomic.atomic_write_text`.
- Produces:
  - `applications_export.CSV_COLUMNS: list[str]` (fixed, documented order)
  - `applications_export.csv_safe(value: Any) -> str` (formula guard)
  - `applications_export.applications_csv(rows) -> str`, `applications_export.applications_json(rows) -> str`
  - `applications_export.write_applications_exports(directory: Path, rows) -> tuple[Path, Path]` — writes `applications.json` then `applications.csv` atomically.
  - CLI: `job-hunter export-applications` (writes both files next to the database, prints the JSON rows like `export-feedback`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_applications_export.py`:

```python
import csv
import io
import json

from job_hunter.applications_export import (
    CSV_COLUMNS,
    applications_csv,
    applications_json,
    csv_safe,
    write_applications_exports,
)


def _row(**over):
    row = {
        "source_key": "acme", "job_id": "42", "company": "Acme", "title": "Engineer",
        "url": "https://example.com/42", "status": "applied", "applied_at": "2026-09-20",
        "location": "Detroit, MI", "posted_at": "2026-09-01T00:00:00Z", "score": 82,
        "salary_evidence": "$100,000 - $120,000", "notes": "referral", 
        "created_at": "2026-09-20T10:00:00+00:00", "updated_at": "2026-09-21T10:00:00+00:00",
    }
    row.update(over)
    return row


def test_csv_columns_are_fixed_and_documented_order():
    assert CSV_COLUMNS == [
        "source_key", "job_id", "company", "title", "url", "status", "applied_at", "location",
        "posted_at", "score", "salary_evidence", "notes", "created_at", "updated_at",
    ]


def test_csv_safe_prefixes_formula_starts_only_for_strings():
    assert csv_safe("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    for start in ("+1", "-5% pay", "@cmd", "\tx", "\rx"):
        assert csv_safe(start) == "'" + start
    assert csv_safe("plain") == "plain"
    assert csv_safe(None) == ""
    assert csv_safe(-1) == "-1"  # numbers are not text and are left alone
    assert csv_safe(82) == "82"


def test_csv_round_trips_through_a_real_csv_reader_and_guards_hostile_cells():
    rows = [_row(), _row(job_id="43", notes="=1+1", title='He said "hi", ok\nnext line', score=None)]
    parsed = list(csv.DictReader(io.StringIO(applications_csv(rows))))
    assert list(parsed[0]) == CSV_COLUMNS
    assert parsed[0]["company"] == "Acme" and parsed[0]["score"] == "82"
    assert parsed[1]["notes"] == "'=1+1"
    assert parsed[1]["title"] == 'He said "hi", ok\nnext line'
    assert parsed[1]["score"] == ""


def test_empty_export_is_a_header_only_csv_and_an_empty_json_list():
    assert applications_csv([]) == ",".join(CSV_COLUMNS) + "\n"
    assert json.loads(applications_json([])) == []


def test_json_keeps_row_order_and_unicode():
    rows = [_row(job_id="2", notes="café"), _row(job_id="1")]
    text = applications_json(rows)
    assert "café" in text and text.endswith("\n")
    assert [r["job_id"] for r in json.loads(text)] == ["2", "1"]


def test_write_exports_creates_both_files_and_is_idempotent(tmp_path):
    rows = [_row(), _row(job_id="43")]
    json_path, csv_path = write_applications_exports(tmp_path / "data", rows)
    assert json_path.name == "applications.json" and csv_path.name == "applications.csv"
    first = (json_path.read_text(), csv_path.read_text())
    write_applications_exports(tmp_path / "data", rows)
    assert (json_path.read_text(), csv_path.read_text()) == first
    assert not list((tmp_path / "data").glob(".*.tmp"))  # atomic writes leave no temp files
```

Append to `tests/test_cli.py` (add `from job_hunter.storage import Storage` and `from job_hunter.models import ApplicationStatus` to the imports):

```python
def test_export_applications_writes_json_and_csv_next_to_the_database(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        f"database_path: {tmp_path}/data/jobs.sqlite3\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOB_HUNTER_ROOT", raising=False)
    with Storage(tmp_path / "data" / "jobs.sqlite3") as storage:
        storage.apply_application(
            "acme", "42", event_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
            changes={"status": ApplicationStatus.SAVED, "notes": "=BAD()"},
            snapshot={"company": "Acme", "title": "Engineer", "url": "https://example.com/42",
                      "location_raw": None, "posted_at": None, "score": None, "salary_evidence": None},
            today=datetime(2026, 9, 26).date(),
        )
    assert main(["export-applications"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [r["job_id"] for r in printed] == ["42"]
    data = tmp_path / "data"
    assert json.loads((data / "applications.json").read_text())[0]["status"] == "saved"
    assert "'=BAD()" in (data / "applications.csv").read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_applications_export.py tests/test_cli.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'job_hunter.applications_export'`).

- [ ] **Step 3: Implement**

Create `src/job_hunter/applications_export.py`:

```python
"""JSON/CSV exports of tracked applications, shared by `job-hunter export-applications` and the
live radar server (which refreshes both files after every committed application write).

`CSV_COLUMNS` is the fixed, documented column order. Any *string* cell that starts with a
spreadsheet formula trigger (`=`, `+`, `-`, `@`, tab, CR) is prefixed with `'` so opening the CSV
in Excel/Sheets can never execute scraped or hand-typed text as a formula.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text

CSV_COLUMNS = [
    "source_key", "job_id", "company", "title", "url", "status", "applied_at", "location",
    "posted_at", "score", "salary_evidence", "notes", "created_at", "updated_at",
]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_PREFIXES) else value
    return str(value)


def applications_csv(rows: Iterable[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([csv_safe(row.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()


def applications_json(rows: Iterable[dict[str, Any]]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False, default=str) + "\n"


def write_applications_exports(directory: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Atomically (re)write `applications.json` then `applications.csv` under `directory`."""
    directory = Path(directory)
    json_path = directory / "applications.json"
    csv_path = directory / "applications.csv"
    atomic_write_text(json_path, applications_json(rows))
    atomic_write_text(csv_path, applications_csv(rows))
    return json_path, csv_path
```

In `src/job_hunter/cli.py`: add `from .applications_export import write_applications_exports` to the imports; after `sub.add_parser("export-feedback")` add `sub.add_parser("export-applications")`; and in `main()`, immediately **before** the `if args.command in {"source-status", ...}:` block, add:

```python
        if args.command == "export-applications":
            with Storage(settings.database_path) as storage:
                rows = storage.export_applications()
            write_applications_exports(settings.database_path.parent, rows)
            print(_json(rows))
            return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_applications_export.py tests/test_cli.py -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/applications_export.py src/job_hunter/cli.py tests/test_applications_export.py tests/test_cli.py
git commit -m "feat(export): applications JSON/CSV exports with formula guard" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Extract the outbox/retry engine into `radar_live_sync.js`

The Phase A `radar_live_ui.js` contains the outbox, retry and flush logic inline (it needed two review-fix rounds to get right, and has no automated test). Phase B needs the same logic on a second page, so extract it verbatim into a DOM-free, dependency-injected module and test it with `node --test`. **Behavior must not change.**

**Files:**
- Create: `scripts/templates/radar_live_sync.js`
- Create: `tests/js/radar_live_sync.test.js`
- Modify: `tests/test_radar_live_js.py` (also run the new test file)
- Modify: `scripts/templates/radar_live_ui.js` (replace the inline outbox/flush code)
- Modify: `scripts/render_radar.py` (`_live_script_html` inlines the new file)
- Test: `tests/test_render_radar.py`

**Interfaces:**
- Consumes: `radar_live_core.js` (`enqueue`, `classifyStatus`, `backoffMs`).
- Produces: `RadarLiveSync.createSync(opts)` (browser global; `module.exports = { createSync }` in node) where
  `opts = { storage: {getItem,setItem}, storageKey: string, send(item) -> Promise<{status, json}>, onState(state, unsavedCount), onOk(item, res, superseded), onRejected(item, res, superseded), onError(err), setTimeout?, clearTimeout? }` and the returned object has `queue(item)`, `flush()`, `restored() -> item[]` (the outbox as loaded at creation), `pending(kind) -> key[]`, `items() -> item[]`.
  Semantics (identical to Phase A): items are `{kind, key, payload}`; strictly sequential sends; `classifyStatus` `'retry'` or a rejected `send()` keeps the item queued and schedules `backoffMs(attempt-1)`; `'ok'`/`'permanent'` remove exactly that item (by identity), persist, then call `onOk`/`onRejected` with `superseded = a newer item for the same kind+key is queued`, reset `attempt`, and continue; an exception thrown inside `onOk`/`onRejected` resets `flushing`, calls `onError`, and never wedges the queue. `onState` receives `'live' | 'saving' | 'offline'`.

- [ ] **Step 1: Write the failing tests**

Create `tests/js/radar_live_sync.test.js`:

```js
const test = require('node:test');
const assert = require('node:assert/strict');
const { createSync } = require('../../scripts/templates/radar_live_sync.js');

const tick = () => new Promise((r) => setImmediate(r));

function harness(over) {
  const store = {}, timers = [], sent = [], pending = [];
  const events = { ok: [], rejected: [], errors: [], states: [] };
  const opts = Object.assign({
    storage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    storageKey: 'box',
    send: (item) => new Promise((resolve, reject) => { sent.push(item); pending.push({ resolve, reject }); }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms, cancelled: false }); return timers.length - 1; },
    clearTimeout: (id) => { if (timers[id]) timers[id].cancelled = true; },
    onState: (s, n) => events.states.push([s, n]),
    onOk: (item, res, sup) => events.ok.push([item, res, sup]),
    onRejected: (item, res, sup) => events.rejected.push([item, res, sup]),
    onError: (e) => events.errors.push(e),
  }, over || {});
  const sync = createSync(opts);
  const active = () => timers.filter((t) => !t.cancelled);
  const saved = () => JSON.parse(store.box || '[]');
  return { sync, store, timers, sent, pending, events, active, saved };
}
const fb = (key, label) => ({ kind: 'feedback', key, payload: { label } });

test('a successful send removes the item, persists, and reports live', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  assert.equal(h.sent.length, 1);
  h.pending[0].resolve({ status: 200, json: { item: {} } });
  await tick();
  assert.equal(h.events.ok.length, 1);
  assert.equal(h.events.ok[0][2], false);
  assert.deepEqual(h.saved(), []);
  assert.deepEqual(h.events.states[h.events.states.length - 1], ['live', 0]);
});

test('a newer write for the same key survives an in-flight older one and is flagged superseded', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.sync.queue(fb('a|1', 'irrelevant')); // replaces the queued entry while the first is in flight
  assert.equal(h.sent.length, 1);
  h.pending[0].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.ok[0][2], true); // superseded: the UI must not clobber the newer choice
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].payload.label, 'irrelevant');
  h.pending[1].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.ok[1][2], false);
  assert.deepEqual(h.saved(), []);
});

test('a 503 keeps the item queued and schedules exactly one backoff timer', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.pending[0].resolve({ status: 503, json: null });
  await tick();
  assert.deepEqual(h.events.states[h.events.states.length - 1], ['offline', 1]);
  assert.equal(h.active().length, 1);
  assert.equal(h.active()[0].ms, 2000);
  assert.equal(h.saved().length, 1);
  assert.equal(h.events.errors.length, 0);
  // the timer retries; a second failure doubles the delay
  h.active()[0].fn();
  assert.equal(h.sent.length, 2);
  h.pending[1].resolve({ status: 429, json: null });
  await tick();
  assert.equal(h.active().length, 1);
  assert.equal(h.active()[0].ms, 4000);
  // success resets the attempt counter
  h.active()[0].fn();
  h.pending[2].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.ok.length, 1);
  assert.deepEqual(h.saved(), []);
});

test('a network failure (rejected send) behaves like a retryable status, without double scheduling', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.pending[0].reject(new Error('offline'));
  await tick();
  assert.deepEqual(h.events.states[h.events.states.length - 1], ['offline', 1]);
  assert.equal(h.active().length, 1);
  assert.equal(h.active()[0].ms, 2000);
  assert.equal(h.events.errors.length, 0);
});

test('a permanent 4xx drops exactly that item, reports it, and the queue continues', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.sync.queue(fb('b|2', 'okay'));
  h.pending[0].resolve({ status: 404, json: { error: 'unknown job' } });
  await tick();
  assert.equal(h.events.rejected.length, 1);
  assert.equal(h.events.rejected[0][0].key, 'a|1');
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].key, 'b|2');
  assert.deepEqual(h.saved().map((o) => o.key), ['b|2']);
});

test('an exception in onOk resets the engine, is surfaced via onError, and never wedges the queue', async () => {
  const h = harness({ onOk: () => { throw new Error('boom'); } });
  h.sync.queue(fb('a|1', 'okay'));
  h.pending[0].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.errors.length, 1);
  assert.equal(h.events.errors[0].message, 'boom');
  assert.deepEqual(h.saved(), []); // the item was removed and persisted before the callback ran
  assert.equal(h.active().length, 0); // not misrouted to the retry path
  h.sync.queue(fb('b|2', 'okay'));
  assert.equal(h.sent.length, 2); // flushing was reset
});

test('items persisted by an earlier page load are restored and flushed', async () => {
  const seeded = [fb('a|1', 'okay'), fb('b|2', null)];
  const h = harness({
    storage: { getItem: () => JSON.stringify(seeded), setItem: () => {} },
  });
  assert.deepEqual(h.sync.restored().map((o) => o.key), ['a|1', 'b|2']);
  h.sync.flush();
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].key, 'a|1');
});

test('corrupt stored JSON is treated as an empty outbox', () => {
  const h = harness({ storage: { getItem: () => '{not json', setItem: () => {} } });
  assert.deepEqual(h.sync.restored(), []);
});

test('pending(kind) lists queued keys of that kind only', () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.sync.queue({ kind: 'application', key: 'b|2', payload: { status: 'saved' } });
  assert.deepEqual(h.sync.pending('application'), ['b|2']);
  assert.deepEqual(h.sync.pending('feedback'), ['a|1']);
});
```

In `tests/test_radar_live_js.py` replace the single test with a parametrized one covering every `*.test.js` file:

```python
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_JS_DIR = Path(__file__).parent / "js"


@pytest.mark.parametrize("test_file", sorted(_JS_DIR.glob("*.test.js")), ids=lambda p: p.name)
def test_js_unit_tests_pass(test_file):
    result = subprocess.run(
        ["node", "--test", str(test_file)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Append to `tests/test_render_radar.py` (extend the existing `test_live_scripts_contain_no_template_tokens_or_script_terminators` loop's tuple to `("radar_live_core.js", "radar_live_sync.js", "radar_live_ui.js")`, and add):

```python
def test_live_page_inlines_the_sync_engine_between_core_and_ui(tmp_path):
    html, _ = _live_html(tmp_path)
    core = html.index("root.RadarLive = api")
    sync = html.index("root.RadarLiveSync = api")
    ui = html.index("var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__RADAR_LIVE__")
    assert core < sync < ui
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_radar_live_js.py tests/test_render_radar.py -q`
Expected: FAIL (`Cannot find module '../../scripts/templates/radar_live_sync.js'`; the render test cannot find the sync script).

- [ ] **Step 3: Implement the engine**

Create `scripts/templates/radar_live_sync.js`:

```js
// Outbox + retry engine shared by the live radar and Applications pages (extracted from
// radar_live_ui.js, behavior unchanged). DOM-free and dependency-injected so it can be unit
// tested with `node --test` (tests/js/radar_live_sync.test.js). Requires radar_live_core.js.
(function (root) {
  'use strict';
  var core = (typeof module !== 'undefined' && module.exports) ? require('./radar_live_core.js') : root.RadarLive;

  function createSync(opts) {
    var storage = opts.storage, storageKey = opts.storageKey;
    var setT = opts.setTimeout || function (fn, ms) { return setTimeout(fn, ms); };
    var clearT = opts.clearTimeout || function (id) { return clearTimeout(id); };
    var outbox = load();
    var loaded = outbox.slice();
    var flushing = false, attempt = 0, retryTimer = null;

    function load() {
      try {
        var parsed = JSON.parse(storage.getItem(storageKey) || '[]');
        return Array.isArray(parsed) ? parsed : [];
      } catch (e) { return []; }
    }
    function save() {
      try { storage.setItem(storageKey, JSON.stringify(outbox)); } catch (e) { /* private mode / quota */ }
    }
    function state() { return flushing ? 'saving' : (outbox.length ? 'offline' : 'live'); }
    function notify() { if (opts.onState) opts.onState(state(), outbox.length); }

    function scheduleRetry() {
      flushing = false;
      attempt += 1;
      notify();
      clearT(retryTimer);
      retryTimer = setT(flush, core.backoffMs(attempt - 1));
    }

    function flush() {
      if (flushing || !outbox.length) { notify(); return; }
      flushing = true;
      notify();
      var item = outbox[0];
      opts.send(item).then(function (res) {
        var kind = core.classifyStatus(res.status);
        if (kind === 'retry') { scheduleRetry(); return; }
        // Remove only this exact entry: a newer write for the same key may have replaced it
        // while the request was in flight, and must stay queued.
        outbox = outbox.filter(function (o) { return o !== item; });
        save();
        var superseded = outbox.some(function (o) { return o.kind === item.kind && o.key === item.key; });
        if (kind === 'ok') opts.onOk(item, res, superseded);
        else opts.onRejected(item, res, superseded);
        flushing = false;
        attempt = 0;
        flush();
      }, function () {
        // Network failure only (not post-processing errors).
        scheduleRetry();
      }).catch(function (e) {
        // Post-processing errors: reset so the queue cannot wedge, and surface the error.
        flushing = false;
        notify();
        (opts.onError || function (err) { setTimeout(function () { throw err; }); })(e);
      });
    }

    return {
      queue: function (item) { outbox = core.enqueue(outbox, item); save(); notify(); flush(); },
      flush: flush,
      restored: function () { return loaded.slice(); },
      items: function () { return outbox.slice(); },
      pending: function (kind) {
        return outbox.filter(function (o) { return o.kind === kind; }).map(function (o) { return o.key; });
      }
    };
  }

  var api = { createSync: createSync };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RadarLiveSync = api;
})(typeof window !== 'undefined' ? window : globalThis);
```

- [ ] **Step 4: Refactor `radar_live_ui.js` onto the engine (behavior unchanged)**

In `scripts/templates/radar_live_ui.js`:

1. Change the opening lines `var L = window.RadarLive, boot = window.__RADAR_LIVE__;` / `if (!L || !boot) return;` to `var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__RADAR_LIVE__;` / `if (!L || !S || !boot) return;` (the Step 1 render test already looks for this new first line).
2. Delete `loadOutbox`, `saveOutbox`, `pendingKeys` (old), `setStatus`, `send`, `scheduleRetry`, `flush`, `queue`, and the `var outbox = loadOutbox(); var flushing...` line, and the "Apply pending outbox entries" block. Keep `paintRow`, `setLabel`, `notice` exactly as they are. In their place (after `notice`) add:

```js
  function safeStorage() {
    try { var s = window.localStorage; s.getItem('job-hunter-probe'); return s; } catch (e) {
      var mem = {};
      return {
        getItem: function (k) { return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
        setItem: function (k, v) { mem[k] = String(v); }
      };
    }
  }
  var SEND_URL = { feedback: '/api/feedback', application: '/api/application' };

  function renderStatus(state, unsaved) {
    var el = $('live-status');
    if (!el) return;
    el.dataset.state = state;
    el.textContent = state === 'saving' ? 'Saving\u2026'
      : state === 'offline' ? 'Offline (' + unsaved + ' unsaved)' : 'Live';
  }

  var sync = S.createSync({
    storage: safeStorage(),
    storageKey: OUTBOX_KEY,
    send: function (item) {
      return fetch(SEND_URL[item.kind], {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item.payload)
      }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (json) {
          return { status: res.status, json: json };
        });
      });
    },
    onState: renderStatus,
    onOk: function (item, res, superseded) {
      if (item.kind === 'feedback' && !superseded) {
        setLabel(item.key, res.json && res.json.item ? res.json.item.label : null);
        applyFilters();
      }
    },
    onRejected: function (item, res, superseded) {
      notice('Could not save that change: ' + ((res.json && res.json.error) || 'HTTP ' + res.status));
      if (item.kind === 'feedback' && !superseded) pullFeedback();
    },
    onError: function (e) { setTimeout(function () { throw e; }); }
  });

  // Apply pending outbox entries to labels so unsent writes are visible after reload.
  sync.restored().filter(function (o) { return o.kind === 'feedback'; }).forEach(function (o) {
    if (o.payload.label) labels[o.key] = o.payload.label; else delete labels[o.key];
  });
  function pendingKeys() { return sync.pending('feedback'); }
```

3. In the feedback click handler replace `queue({` with `sync.queue({`. Replace `window.addEventListener('focus', flush);` with `window.addEventListener('focus', sync.flush);`, and inside the `visibilitychange` handler replace `flush();` with `sync.flush();`. In the boot block at the bottom replace the two lines `setStatus(); flush();` with `sync.flush();`.
4. Run `grep -n "flush\|setStatus\|loadOutbox\|saveOutbox\|scheduleRetry\|[^.]queue(" scripts/templates/radar_live_ui.js` — every remaining hit must be `sync.`-prefixed or the `renderStatus` definition.

- [ ] **Step 5: Inline the engine**

In `scripts/render_radar.py`, `_live_script_html`: change the tuple to `for name in ("radar_live_core.js", "radar_live_sync.js", "radar_live_ui.js"):`. (The `test_live_page_inlines...` test above asserts the order.)

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
node --check scripts/templates/radar_live_sync.js && node --check scripts/templates/radar_live_ui.js
node --test tests/js/radar_live_sync.test.js tests/js/radar_live_core.test.js
uv run pytest tests/test_radar_live_js.py tests/test_render_radar.py -q && uv run ruff check .
```

Expected: all PASS (golden static test still green — the change is live-only).

- [ ] **Step 7: Commit**

```bash
git add scripts/templates/radar_live_sync.js scripts/templates/radar_live_ui.js scripts/render_radar.py tests/js/radar_live_sync.test.js tests/test_radar_live_js.py tests/test_render_radar.py
git commit -m "refactor(radar): extract the outbox/retry engine into a tested module" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Core JS — application filters and reconcile

**Files:**
- Modify: `scripts/templates/radar_live_core.js`
- Modify: `tests/js/radar_live_core.test.js`

**Interfaces:**
- Consumes: none.
- Produces (on `RadarLive`):
  - `APP_STATUSES` (`['saved','applied','interviewing','offer','rejected','withdrawn']`)
  - `emptyFilters()` gains `app: ''` (`''` any | `'untracked'` | a status) and `hideApplied: false`
  - `filtersActive(f)` counts `f.app` / `f.hideApplied`
  - `rowPasses(facts, f, label, todayIso, appStatus)` — new optional 5th arg (`undefined`/`null` = not tracked). `hideApplied` hides any tracked row whose status is not `saved`.
  - `encodeHash`/`decodeHash` carry `app` (validated against `'untracked'` + statuses) and `hideapp=1`.
  - `reconcileApps(localApps, serverApps, pendingKeys) -> [{key, app|null}]` (apps compared by `status`, `applied_at`, `notes`; keys with a pending write are skipped; a local key absent on the server yields `app: null`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/js/radar_live_core.test.js`:

```js
test('application filter: any / untracked / a specific status', () => {
  const f = core.emptyFilters();
  assert.equal(core.rowPasses(facts(), f, null, TODAY, 'applied'), true);
  f.app = 'untracked';
  assert.equal(core.rowPasses(facts(), f, null, TODAY, undefined), true);
  assert.equal(core.rowPasses(facts(), f, null, TODAY, 'saved'), false);
  f.app = 'applied';
  assert.equal(core.rowPasses(facts(), f, null, TODAY, 'applied'), true);
  assert.equal(core.rowPasses(facts(), f, null, TODAY, 'offer'), false);
  assert.equal(core.rowPasses(facts(), f, null, TODAY, null), false);
});

test('hide-applied hides tracked rows past "saved" but keeps saved and untracked ones', () => {
  const f = core.emptyFilters();
  f.hideApplied = true;
  assert.equal(core.rowPasses(facts(), f, null, TODAY, undefined), true);
  assert.equal(core.rowPasses(facts(), f, null, TODAY, 'saved'), true);
  for (const s of ['applied', 'interviewing', 'offer', 'rejected', 'withdrawn']) {
    assert.equal(core.rowPasses(facts(), f, null, TODAY, s), false, s);
  }
});

test('application filters make filtersActive true and survive the hash round trip', () => {
  const f = core.emptyFilters();
  f.app = 'interviewing';
  f.hideApplied = true;
  assert.equal(core.filtersActive(f), true);
  assert.equal(core.encodeHash(f), 'app=interviewing&hideapp=1');
  assert.deepEqual(core.decodeHash('#' + core.encodeHash(f)), f);
  assert.equal(core.decodeHash('app=untracked').app, 'untracked');
  assert.equal(core.decodeHash('app=hired').app, '');
  assert.equal(core.decodeHash('hideapp=0').hideApplied, false);
});

test('APP_STATUSES lists the six statuses in lifecycle order', () => {
  assert.deepEqual(core.APP_STATUSES, ['saved', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn']);
});

test('reconcileApps pulls server state for keys with no pending write', () => {
  const a = { status: 'applied', applied_at: '2026-09-20', notes: null };
  const local = { 'a|1': a, 'b|2': { status: 'saved', applied_at: null, notes: null }, 'c|3': a };
  const server = {
    'a|1': { status: 'applied', applied_at: '2026-09-20', notes: null }, // equal -> no change
    'b|2': { status: 'offer', applied_at: '2026-09-21', notes: 'x' },
    'd|4': a,
  };
  const changes = core.reconcileApps(local, server, ['c|3']).sort((x, y) => x.key.localeCompare(y.key));
  assert.deepEqual(changes.map((c) => c.key), ['b|2', 'd|4']);
  assert.equal(changes[0].app.status, 'offer');
  // a local key the server no longer has is cleared unless a write is pending
  assert.deepEqual(core.reconcileApps({ 'x|9': a }, {}, []), [{ key: 'x|9', app: null }]);
  assert.deepEqual(core.reconcileApps({ 'x|9': a }, {}, ['x|9']), []);
  // null/undefined/"" notes and null/undefined dates compare equal
  assert.deepEqual(
    core.reconcileApps({ 'k|1': { status: 'saved', notes: '', applied_at: undefined } },
                       { 'k|1': { status: 'saved', notes: null, applied_at: null } }, []),
    []
  );
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test tests/js/radar_live_core.test.js`
Expected: the new tests FAIL (`core.APP_STATUSES` undefined, `reconcileApps` not a function, filters have no `app`).

- [ ] **Step 3: Implement**

In `scripts/templates/radar_live_core.js`:

1. Add `var APP_STATUSES = ['saved', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn'];` next to `FEEDBACK_FILTERS`.
2. `emptyFilters()`: add `app: '', hideApplied: false` to the returned object.
3. `filtersActive(f)`: append `|| f.app || f.hideApplied` inside the boolean expression (before the closing `)`), i.e. `... f.hasSalary || f.feedback || f.app || f.hideApplied)`.
4. `rowPasses(facts, f, label, todayIso, appStatus)`: add the parameter and, immediately before `return true;`:

```js
    var app = appStatus || null;
    if (f.hideApplied && app && app !== 'saved') return false;
    if (f.app === 'untracked' && app) return false;
    if (f.app && f.app !== 'untracked' && app !== f.app) return false;
```

5. `encodeHash`: before `if (f.sort !== 'default')` add `if (f.app) add('app', f.app); if (f.hideApplied) add('hideapp', '1');`. `decodeHash`: add branches

```js
      else if (key === 'app') { if (value === 'untracked' || APP_STATUSES.indexOf(value) !== -1) f.app = value; }
      else if (key === 'hideapp') f.hideApplied = value === '1';
```

6. Add above `var api = {`:

```js
  function sameApp(a, b) {
    return a.status === b.status && (a.applied_at || null) === (b.applied_at || null) &&
      (a.notes || null) === (b.notes || null);
  }

  // localApps / serverApps: {key: {status, applied_at, notes}}; pendingKeys: keys with an unsent write.
  function reconcileApps(localApps, serverApps, pendingKeys) {
    var changes = [], seen = {};
    Object.keys(serverApps).forEach(function (k) {
      seen[k] = true;
      if (pendingKeys.indexOf(k) !== -1) return;
      if (!localApps[k] || !sameApp(localApps[k], serverApps[k])) changes.push({ key: k, app: serverApps[k] });
    });
    Object.keys(localApps).forEach(function (k) {
      if (seen[k] || pendingKeys.indexOf(k) !== -1) return;
      changes.push({ key: k, app: null });
    });
    return changes;
  }
```

and add `APP_STATUSES: APP_STATUSES, reconcileApps: reconcileApps` to the `api` object.

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --check scripts/templates/radar_live_core.js && node --test tests/js/radar_live_core.test.js tests/js/radar_live_sync.test.js && uv run pytest tests/test_radar_live_js.py -q`
Expected: PASS (all earlier core tests still pass — the new arguments/fields are additive).

- [ ] **Step 5: Commit**

```bash
git add scripts/templates/radar_live_core.js tests/js/radar_live_core.test.js
git commit -m "feat(radar): application filters and reconcile in the client core" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Radar UI — Track chip/panel, application filters, polling (against a fixed DOM contract)

**Files:**
- Modify: `scripts/templates/radar_live_ui.js`

**Interfaces:**
- Consumes: `RadarLive` (`rowPasses` 5th arg, `emptyFilters().app/.hideApplied`, `reconcileApps`, `isoNow`), `RadarLiveSync` (Task 3); `window.__RADAR_LIVE__` now also carries `applications: {"source_key|job_id": {status, applied_at, notes}}` and `versions.applications`.
- **DOM contract that Task 7 must render** (the UI depends on exactly these):
  - Every live row (`details.row` and `div.plain-row`) has, inside `.row-end` after the feedback buttons, a `button.app-chip` (text `Track` or the capitalised status; `data-app-status` = status or `""`; class `app-chip-on` when tracked).
  - Every live row has one `div.app-panel` containing `select.app-status` (options: value `""` = "Not tracking", then the six statuses), `input.app-date` (`type=date`), `textarea.app-notes`. For `details.row` the panel sits inside `.row-detail`; for `div.plain-row` it is a child of the row after `.plain-row-top` and carries the `hidden` attribute initially.
  - Toolbar (live only): `select#live-app` (values `""`, `untracked`, the six statuses) and `input#live-hide-applied` (checkbox).
- Produces: nothing exported. Endpoints it calls: `POST /api/application` `{source_key, job_id, status, applied_at?, notes, client_ts}` (**always the full current record**; `status: null` deletes), `GET /api/applications` → `{applications: {key: item}}`, `GET /api/state` → `versions.applications`. A write reply is `{item}` | `{deleted:true}` | `{stale:true, item|null}`, optionally with `export_warning`.

- [ ] **Step 1: Implement**

In `scripts/templates/radar_live_ui.js`, make these edits:

1. Element lookups (next to the other toolbar lookups): `var appSel = $('live-app'), hideApplied = $('live-hide-applied');`
2. Add the application section directly after `function pendingKeys()` (it must come after `sync` is created and before the polling section):

```js
  // ---- application tracking --------------------------------------------------------------
  var APP_LABEL = {
    saved: 'Saved', applied: 'Applied', interviewing: 'Interviewing', offer: 'Offer',
    rejected: 'Rejected', withdrawn: 'Withdrawn'
  };
  var apps = {};
  Object.keys(boot.applications || {}).forEach(function (k) { apps[k] = boot.applications[k]; });

  function appFromPayload(key, p) {
    if (p.status === null) return null;
    var prev = apps[key];
    return {
      status: p.status,
      applied_at: p.status === 'saved' ? null : (p.applied_at || (prev && prev.applied_at) || todayIso()),
      notes: p.notes === undefined ? (prev ? prev.notes : null) : p.notes
    };
  }
  function pick(it) { return { status: it.status, applied_at: it.applied_at, notes: it.notes }; }

  function fillPanel(panel, app) {
    var sel = panel.querySelector('.app-status'), date = panel.querySelector('.app-date');
    var notes = panel.querySelector('.app-notes');
    sel.value = app ? app.status : '';
    date.value = app && app.applied_at ? app.applied_at : '';
    date.disabled = !app || app.status === 'saved';
    if (document.activeElement !== notes) notes.value = app && app.notes ? app.notes : '';
  }
  function paintApp(row) {
    var app = apps[keyOf(row)] || null;
    row.dataset.appStatus = app ? app.status : '';
    var chip = row.querySelector('.app-chip');
    if (chip) {
      chip.textContent = app ? APP_LABEL[app.status] : 'Track';
      chip.dataset.appStatus = app ? app.status : '';
      chip.classList.toggle('app-chip-on', !!app);
    }
    var panel = row.querySelector('.app-panel');
    if (panel) fillPanel(panel, app);
  }
  function setApp(key, app) {
    if (app) apps[key] = app; else delete apps[key];
    (rowsByKey[key] || []).forEach(paintApp);
  }

  // Unsent application writes from an earlier page load are shown, not lost.
  sync.restored().filter(function (o) { return o.kind === 'application'; }).forEach(function (o) {
    var next = appFromPayload(o.key, o.payload);
    if (next) apps[o.key] = next; else delete apps[o.key];
  });

  function saveApp(row) {
    var key = keyOf(row), panel = row.querySelector('.app-panel');
    var status = panel.querySelector('.app-status').value;
    var payload = {
      source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, client_ts: L.isoNow(Date.now())
    };
    if (status === '') {
      var had = apps[key];
      if (!had) return;
      if (had.notes && !window.confirm('Stop tracking this job and delete its notes?')) {
        fillPanel(panel, had);
        return;
      }
      payload.status = null;
    } else {
      payload.status = status;
      var date = panel.querySelector('.app-date').value;
      if (status !== 'saved' && date) payload.applied_at = date;
      var notes = panel.querySelector('.app-notes').value;
      payload.notes = notes.trim() === '' ? null : notes;
    }
    setApp(key, appFromPayload(key, payload));
    sync.queue({ kind: 'application', key: key, payload: payload });
    applyFilters();
  }

  document.addEventListener('click', function (evt) {
    var chip = evt.target.closest ? evt.target.closest('.app-chip') : null;
    if (!chip) return;
    // Nested inside <summary>: keep the click from toggling the row.
    evt.preventDefault();
    evt.stopPropagation();
    var row = chip.closest(ROW_SELECTOR), panel = row && row.querySelector('.app-panel');
    if (!panel) return;
    if (row.tagName === 'DETAILS') { row.open = true; panel.hidden = false; }
    else { panel.hidden = !panel.hidden; }
    if (!panel.hidden) panel.querySelector('.app-status').focus();
  });
  document.addEventListener('change', function (evt) {
    var field = evt.target.closest ? evt.target.closest('.app-status, .app-date, .app-notes') : null;
    if (!field) return;
    var row = field.closest(ROW_SELECTOR);
    if (row) saveApp(row);
  });

  function pullApplications() {
    return fetch('/api/applications').then(function (r) { return r.json(); }).then(function (body) {
      L.reconcileApps(apps, body.applications || {}, sync.pending('application')).forEach(function (c) {
        setApp(c.key, c.app ? pick(c.app) : null);
      });
      applyFilters();
    }).catch(function () { /* transient; the outbox status already reflects real save failures */ });
  }
```

3. Extend the `sync` callbacks created in Task 3: in `onOk` add (before the closing brace)

```js
      if (item.kind === 'application') {
        if (!superseded) {
          var it = res.json && res.json.item;
          setApp(item.key, it ? pick(it) : null);
          applyFilters();
        }
        if (res.json && res.json.export_warning) {
          notice('Saved, but the export files could not be refreshed: ' + res.json.export_warning);
        }
      }
```

and in `onRejected` add `if (item.kind === 'application' && !superseded) pullApplications();`.

4. Polling/visibility: in `poll()`, after the feedback-version line add `if (v.applications !== known.applications) { known.applications = v.applications; pullApplications(); }`; in the `visibilitychange` handler add `pullApplications();` next to `pullFeedback();`.
5. Filters: in `readFilters()` add `f.app = appSel.value; f.hideApplied = hideApplied.checked;`; in `writeFilters(f)` add `appSel.value = f.app; hideApplied.checked = f.hideApplied;`; add `appSel, hideApplied` to the array that gets `change` listeners; in `applyFilters()` change the `L.rowPasses(...)` call to pass the fifth argument: `L.rowPasses(factsOf.get(row), f, labels[keyOf(row)], today, apps[keyOf(row)] && apps[keyOf(row)].status)`.
6. Boot block: before `applyFilters();` add `rows.forEach(paintApp);` (next to the existing `rows.forEach(paintRow);`).

- [ ] **Step 2: Verify**

Run: `node --check scripts/templates/radar_live_ui.js && node --test tests/js/*.test.js && uv run pytest tests/test_radar_live_js.py -q`
Expected: all PASS. (There is no automated DOM test for this file — the DOM contract is rendered by Task 7 and exercised by the controller's jsdom run and the manual checklist in Task 9.)

- [ ] **Step 3: Commit**

```bash
git add scripts/templates/radar_live_ui.js
git commit -m "feat(radar): Track chip, application panel, filters and polling in the live UI" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Applications page script (against a fixed DOM contract)

**Files:**
- Create: `scripts/templates/applications_ui.js`

**Interfaces:**
- Consumes: `RadarLive` (`isoNow`, `reconcileApps`), `RadarLiveSync` (Task 3); `window.__APPS_LIVE__ = {versions: {applications: string}}`.
- **DOM contract that Task 7 must render:**
  - `#app-rows` containing one `article.app-row` per application with `data-source-key`, `data-job-id`, `data-status`, `data-applied-at` (`YYYY-MM-DD` or `""`), `data-posting` (`open|closed|removed`); inside: `select.app-status` (the six statuses only), `input.app-date` (`type=date`; `disabled` when status is `saved`), `textarea.app-notes`, `span.app-days`, `button.app-delete`.
  - `#app-counts` containing `[data-count-status="<status>"]` elements, each with a `b` child holding the number, plus `[data-count-status="all"]`.
  - `#app-empty` (paragraph; `hidden` while rows exist).
  - `#live-status`, `#live-notice`, `#live-reload` (hidden banner) containing `#live-reload-link`.
- Produces: no exports. Same endpoints as Task 5; `POST /api/application` payloads are always the full record.

- [ ] **Step 1: Implement**

Create `scripts/templates/applications_ui.js`:

```js
// Behavior for the /applications page. Requires window.RadarLive (radar_live_core.js),
// window.RadarLiveSync (radar_live_sync.js) and window.__APPS_LIVE__ (emitted by
// scripts/render_applications.py). Shares the radar page's outbox (same localStorage key), so a
// write queued while offline on either page is flushed by whichever page is open next.
(function () {
  'use strict';
  var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__APPS_LIVE__;
  if (!L || !S || !boot) return;

  var SEND_URL = { feedback: '/api/feedback', application: '/api/application' };
  var container = document.getElementById('app-rows');
  function $(id) { return document.getElementById(id); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function todayIso() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function safeStorage() {
    try { var s = window.localStorage; s.getItem('job-hunter-probe'); return s; } catch (e) {
      var mem = {};
      return {
        getItem: function (k) { return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
        setItem: function (k, v) { mem[k] = String(v); }
      };
    }
  }
  function rowsList() { return Array.prototype.slice.call(container.querySelectorAll('.app-row')); }
  function keyOf(row) { return row.dataset.sourceKey + '|' + row.dataset.jobId; }

  var noticeTimer = null;
  function notice(message) {
    var el = $('live-notice');
    if (!el) return;
    el.textContent = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { el.textContent = ''; }, 8000);
  }
  function showReload() { var b = $('live-reload'); if (b) b.hidden = false; }
  function renderStatus(state, unsaved) {
    var el = $('live-status');
    if (!el) return;
    el.dataset.state = state;
    el.textContent = state === 'saving' ? 'Saving\u2026'
      : state === 'offline' ? 'Offline (' + unsaved + ' unsaved)' : 'Live';
  }

  function daysText(status, appliedAt) {
    if (status === 'saved' || !appliedAt) return '';
    var n = Math.round((Date.parse(todayIso() + 'T00:00:00Z') - Date.parse(appliedAt + 'T00:00:00Z')) / 86400000);
    if (isNaN(n)) return '';
    return n <= 0 ? 'Applied today' : n === 1 ? 'Applied 1 day ago' : 'Applied ' + n + ' days ago';
  }

  function updateCounts() {
    var counts = { all: 0 };
    rowsList().forEach(function (row) {
      counts.all += 1;
      counts[row.dataset.status] = (counts[row.dataset.status] || 0) + 1;
    });
    Array.prototype.forEach.call(document.querySelectorAll('#app-counts [data-count-status]'), function (el) {
      var b = el.querySelector('b');
      if (b) b.textContent = String(counts[el.dataset.countStatus] || 0);
    });
    var empty = $('app-empty');
    if (empty) empty.hidden = counts.all > 0;
  }

  function refreshRow(row, app) {
    row.dataset.status = app.status;
    row.dataset.appliedAt = app.applied_at || '';
    var sel = row.querySelector('.app-status'), date = row.querySelector('.app-date');
    var notes = row.querySelector('.app-notes');
    sel.value = app.status;
    date.value = app.applied_at || '';
    date.disabled = app.status === 'saved';
    if (document.activeElement !== notes) notes.value = app.notes || '';
    row.querySelector('.app-days').textContent = daysText(app.status, app.applied_at);
    updateCounts();
  }

  function rowApp(row) {
    return {
      status: row.dataset.status, applied_at: row.dataset.appliedAt || null,
      notes: row.querySelector('.app-notes').value.trim() === '' ? null : row.querySelector('.app-notes').value
    };
  }

  var sync = S.createSync({
    storage: safeStorage(),
    storageKey: 'job-hunter-outbox',
    send: function (item) {
      return fetch(SEND_URL[item.kind], {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item.payload)
      }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (json) {
          return { status: res.status, json: json };
        });
      });
    },
    onState: renderStatus,
    onOk: function (item, res, superseded) {
      if (item.kind !== 'application') return;
      var it = res.json && res.json.item;
      var row = rowsList().filter(function (r) { return keyOf(r) === item.key; })[0];
      if (!superseded && row) {
        if (it) refreshRow(row, it); else { row.remove(); updateCounts(); }
      }
      if (res.json && res.json.export_warning) {
        notice('Saved, but the export files could not be refreshed: ' + res.json.export_warning);
      }
    },
    onRejected: function (item, res) {
      notice('Could not save that change: ' + ((res.json && res.json.error) || 'HTTP ' + res.status) +
        ' \u2014 reload to see the current state.');
      showReload();
    },
    onError: function (e) { setTimeout(function () { throw e; }); }
  });

  function saveRow(row) {
    var status = row.querySelector('.app-status').value;
    var date = row.querySelector('.app-date').value;
    var notes = row.querySelector('.app-notes').value;
    var payload = {
      source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, status: status,
      notes: notes.trim() === '' ? null : notes, client_ts: L.isoNow(Date.now())
    };
    if (status !== 'saved' && date) payload.applied_at = date;
    refreshRow(row, {
      status: status,
      applied_at: status === 'saved' ? null : (payload.applied_at || row.dataset.appliedAt || todayIso()),
      notes: payload.notes
    });
    sync.queue({ kind: 'application', key: keyOf(row), payload: payload });
  }

  container.addEventListener('change', function (evt) {
    var field = evt.target.closest ? evt.target.closest('.app-status, .app-date, .app-notes') : null;
    if (!field) return;
    var row = field.closest('.app-row');
    if (row) saveRow(row);
  });
  container.addEventListener('click', function (evt) {
    var del = evt.target.closest ? evt.target.closest('.app-delete') : null;
    if (!del) return;
    var row = del.closest('.app-row');
    if (!row || !window.confirm('Stop tracking this job and delete its notes?')) return;
    var payload = {
      source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, status: null,
      client_ts: L.isoNow(Date.now())
    };
    var key = keyOf(row);
    row.remove();
    updateCounts();
    sync.queue({ kind: 'application', key: key, payload: payload });
  });

  // Pending application writes from an earlier page load: show them as the row's state.
  sync.restored().filter(function (o) { return o.kind === 'application'; }).forEach(function (o) {
    var row = rowsList().filter(function (r) { return keyOf(r) === o.key; })[0];
    if (!row) return;
    if (o.payload.status === null) { row.remove(); return; }
    refreshRow(row, {
      status: o.payload.status,
      applied_at: o.payload.status === 'saved' ? null : (o.payload.applied_at || row.dataset.appliedAt || todayIso()),
      notes: o.payload.notes === undefined ? rowApp(row).notes : o.payload.notes
    });
  });

  // Changes made elsewhere (another tab, the radar page): tell the user, never reshuffle rows.
  var known = (boot.versions || {}).applications;
  function domMap() {
    var map = {};
    rowsList().forEach(function (row) { map[keyOf(row)] = rowApp(row); });
    return map;
  }
  function poll() {
    if (document.visibilityState !== 'visible') return;
    fetch('/api/state').then(function (r) { return r.json(); }).then(function (s) {
      var v = (s.versions || {}).applications;
      if (v === known) return;
      return fetch('/api/applications').then(function (r) { return r.json(); }).then(function (body) {
        known = v;
        if (L.reconcileApps(domMap(), body.applications || {}, sync.pending('application')).length) showReload();
      });
    }).catch(function () { /* server briefly unreachable; the next tick retries */ });
  }
  setInterval(poll, 10000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { poll(); sync.flush(); }
  });
  window.addEventListener('focus', sync.flush);
  var reloadLink = $('live-reload-link');
  if (reloadLink) reloadLink.addEventListener('click', function (evt) { evt.preventDefault(); location.reload(); });

  updateCounts();
  sync.flush();
})();
```

- [ ] **Step 2: Verify**

Run: `node --check scripts/templates/applications_ui.js`
Expected: no output (exit 0). Behavior is verified against the rendered page in Task 7 (render tests), Task 8 (server tests) and Task 9 (smoke).

- [ ] **Step 3: Commit**

```bash
git add scripts/templates/applications_ui.js
git commit -m "feat(radar): Applications page script (edits, delete, counts, foreign-change notice)" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Renderers — live Track chip/panel/toolbar and the Applications page

**Files:**
- Modify: `scripts/render_radar.py`
- Create: `scripts/render_applications.py`, `scripts/templates/applications_template.html`
- Test: `tests/test_render_radar.py`, `tests/test_render_applications.py` (create)

**Interfaces:**
- Consumes: the DOM contracts in Tasks 5–6; `radar_live_core.js`, `radar_live_sync.js`, `radar_live_ui.js`, `applications_ui.js`.
- Produces:
  - `render_radar.LiveState(feedback, versions, archive_name, applications=<default {}>)` — `applications` maps `"source|job"` → an application row dict (`status, applied_at, notes` at least).
  - Live radar output additionally contains: `button.app-chip` + `div.app-panel` per row, `select#live-app`, `input#live-hide-applied`, an `Applications` link (`href="/applications"`) in the live bar, boot JSON `applications` map. **Static output is unchanged (golden test).**
  - `render_applications.render_applications_page(*, applications: list[dict], job_states: dict[str, str], versions: dict[str, str | None], today: date, archive_name: str | None = None) -> str` where `job_states` is `Storage.job_statuses(...)` output (`"source|job"` → `jobs.status`; a missing key means the posting was removed).
  - `render_applications.STATUS_ORDER = ("offer","interviewing","applied","saved","rejected","withdrawn")`, `days_since(applied_at: str | None, today: date) -> int | None`, `posting_state(job_status: str | None) -> Literal["open","closed","removed"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_render_radar.py`:

```python
def _app(status="applied", applied_at="2026-09-20", notes=None):
    return {"status": status, "applied_at": applied_at, "notes": notes}


def _live_apps_html(tmp_path, apps=None):
    inputs = _golden_inputs(tmp_path)
    state = LiveState(
        feedback={}, versions={"archive": "a1", "assessments": "s1", "feedback": "f1", "applications": "p1"},
        archive_name="search.json", applications=apps or {},
    )
    html, _ = render(live=True, live_state=state, **inputs)
    return html


def test_static_render_has_no_application_machinery(tmp_path):
    html, _ = render(**_golden_inputs(tmp_path))
    for needle in ("app-chip", "app-panel", "live-app", "live-hide-applied", "/applications"):
        assert needle not in html


def test_live_rows_get_a_track_chip_and_a_panel_for_both_row_types(tmp_path):
    html = _live_apps_html(tmp_path)
    scored = re.search(r'<details class="row[^>]*data-job-id="1"[^>]*>.*?</details>', html, re.S).group(0)
    assert 'class="app-chip"' in scored and ">Track</button>" in scored
    assert scored.index('class="app-chip"') < scored.index('class="row-detail"') < scored.index("app-panel")
    assert 'select class="app-status"' in scored and 'type="date"' in scored and "app-notes" in scored
    assert "<div class=\"app-panel\" hidden" not in scored  # inside an opened details: never hidden
    plain = re.search(r'<div class="plain-row[^>]*data-job-id="3"[^>]*>.*?\n    </div>', html, re.S).group(0)
    assert 'class="app-chip"' in plain
    assert 'class="app-panel" hidden' in plain  # the chip toggles it


def test_tracked_job_renders_status_date_notes_and_selected_option(tmp_path):
    html = _live_apps_html(tmp_path, {"x|1": _app("interviewing", "2026-09-22", "call <b>Tue</b> & \"bring\" CV")})
    row = re.search(r'<details class="row[^>]*data-job-id="1"[^>]*>.*?</details>', html, re.S).group(0)
    assert 'data-app-status="interviewing"' in row and "app-chip-on" in row
    assert ">Interviewing</button>" in row
    assert '<option value="interviewing" selected>' in row
    assert 'value="2026-09-22"' in row
    assert "call &lt;b&gt;Tue&lt;/b&gt; &amp; \"bring\" CV" in row
    assert "<b>Tue</b>" not in row
    untouched = re.search(r'<details class="row[^>]*data-job-id="2"[^>]*>.*?</details>', html, re.S).group(0)
    assert 'data-app-status=""' in untouched
    assert '<option value="" selected>Not tracking</option>' in untouched
    assert untouched.count(" selected>") == 1


def test_saved_application_disables_the_date_input(tmp_path):
    html = _live_apps_html(tmp_path, {"x|1": _app("saved", None)})
    row = re.search(r'<details class="row[^>]*data-job-id="1"[^>]*>.*?</details>', html, re.S).group(0)
    assert re.search(r'<input type="date" class="app-date"[^>]*disabled', row)


def test_live_toolbar_bar_and_boot_json_carry_the_application_features(tmp_path):
    html = _live_apps_html(tmp_path, {"x|1": _app(notes="</script><img src=x>")})
    assert 'id="live-app"' in html and 'id="live-hide-applied"' in html
    assert '<option value="untracked">' in html
    assert 'href="/applications"' in html
    assert '"applications": {"x|1":' in html
    assert '\\u003c/script\\u003e' in html and "</script><img" not in html
    assert '"applications": "p1"' in html  # versions.applications reaches the client
```

Create `tests/test_render_applications.py`:

```python
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from render_applications import (  # noqa: E402
    STATUS_ORDER,
    days_since,
    posting_state,
    render_applications_page,
)

TODAY = date(2026, 9, 26)
VERSIONS = {"applications": "v1"}


def _app(job_id="1", status="applied", applied_at="2026-09-20", notes=None, title="ADAS Engineer",
         updated_at="2026-09-21T10:00:00+00:00", url="https://example.com/1", **over):
    row = {
        "source_key": "acme", "job_id": job_id, "status": status, "applied_at": applied_at,
        "notes": notes, "company": "Acme", "title": title, "url": url, "location": "Detroit, MI",
        "posted_at": "2026-09-01T00:00:00Z", "score": 82, "salary_evidence": None,
        "created_at": "2026-09-20T10:00:00+00:00", "updated_at": updated_at,
    }
    row.update(over)
    return row


def _page(apps, states=None):
    return render_applications_page(
        applications=apps, job_states=states if states is not None else {}, versions=VERSIONS,
        today=TODAY, archive_name="default_2026-09-26.json",
    )


def test_status_order_and_helpers():
    assert STATUS_ORDER == ("offer", "interviewing", "applied", "saved", "rejected", "withdrawn")
    assert days_since("2026-09-20", TODAY) == 6
    assert days_since("2026-09-26", TODAY) == 0
    assert days_since(None, TODAY) is None
    assert posting_state("active") == "open"
    assert posting_state("closed") == "closed"
    assert posting_state(None) == "removed"


def test_rows_sort_by_status_order_then_applied_date_newest_first():
    apps = [
        _app("1", "saved", None, updated_at="2026-09-21T00:00:00+00:00"),
        _app("2", "applied", "2026-09-10"),
        _app("3", "offer", "2026-09-01"),
        _app("4", "applied", "2026-09-20"),
        _app("5", "withdrawn", "2026-09-05"),
    ]
    html = _page(apps)
    order = re.findall(r'class="app-row"[^>]*data-job-id="(\d+)"', html)
    assert order == ["3", "4", "2", "1", "5"]


def test_counts_days_since_and_posting_state():
    apps = [_app("1", "applied", "2026-09-20"), _app("2", "saved", None), _app("3", "rejected", "2026-09-26")]
    html = _page(apps, {"acme|1": "active", "acme|2": "closed"})  # job 3 is gone
    assert 'data-count-status="all">Total <b>3</b>' in html
    assert 'data-count-status="applied">Applied <b>1</b>' in html
    assert 'data-count-status="offer">Offer <b>0</b>' in html
    assert "Applied 6 days ago" in html and "Applied today" in html
    assert 'data-posting="open"' in html and 'data-posting="closed"' in html and 'data-posting="removed"' in html
    assert "Posting removed" in html


def test_empty_state_is_visible_only_without_applications():
    assert '<p id="app-empty" class="app-empty">' in _page([])
    assert '<p id="app-empty" class="app-empty" hidden>' in _page([_app()])


def test_hostile_text_is_escaped_and_unsafe_urls_are_neutralised():
    apps = [_app(notes="</script><img src=x onerror=alert(1)>", title='<script>alert("t")</script>',
                 url="javascript:alert(1)")]
    html = _page(apps)
    assert "<img src=x" not in html and "<script>alert" not in html
    assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in html
    assert 'href="javascript:' not in html
    assert 'class="app-title" href="#"' in html
    good = _page([_app(url="https://example.com/x?a=1&b=2")])
    assert 'href="https://example.com/x?a=1&amp;b=2"' in good


def test_page_embeds_versions_scripts_and_never_descriptions_or_db_paths():
    html = _page([_app()])
    assert 'window.__APPS_LIVE__ = {"versions": {"applications": "v1"}}' in html
    assert (
        html.index("root.RadarLive = api")
        < html.index("root.RadarLiveSync = api")
        < html.index("var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__APPS_LIVE__")
    )
    for needle in ("jobs.sqlite3", "description"):
        assert needle not in html
    assert 'href="/"' in html  # link back to the radar
    assert 'id="live-status"' in html and 'id="live-reload"' in html and 'id="app-rows"' in html


def test_status_select_lists_the_six_statuses_with_the_current_one_selected():
    html = _page([_app(status="offer")])
    select = re.search(r'<select class="app-status">(.*?)</select>', html, re.S).group(1)
    assert re.findall(r'<option value="(\w+)"', select) == ["saved", "applied", "interviewing", "offer", "rejected", "withdrawn"]
    assert '<option value="offer" selected>' in select


def test_saved_row_has_a_disabled_date_and_no_days_text():
    html = _page([_app(status="saved", applied_at=None)])
    assert re.search(r'<input type="date" class="app-date"[^>]*disabled', html)
    assert '<span class="app-days"></span>' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render_radar.py tests/test_render_applications.py -q`
Expected: collection error (`ModuleNotFoundError: No module named 'render_applications'`; `LiveState` rejects `applications=`).

- [ ] **Step 3: Implement the live radar additions in `scripts/render_radar.py`**

1. `LiveState`: add a defaulted field (keep `frozen=True`; add `field` to the dataclasses import):

```python
    applications: dict[str, dict[str, Any]] = field(default_factory=dict)
```

2. Add helpers above `_row_html` (next to `_live_label`):

```python
_APP_STATUSES = ("saved", "applied", "interviewing", "offer", "rejected", "withdrawn")


def _app_chip_html(app: dict[str, Any] | None) -> str:
    status = app["status"] if app else ""
    label = status.capitalize() if app else "Track"
    on = " app-chip-on" if app else ""
    return (
        f'<button type="button" class="app-chip{on}" data-app-status="{_attr(status)}" '
        f'title="Track this application">{_e(label)}</button>'
    )


def _app_panel_html(app: dict[str, Any] | None, *, hidden: bool) -> str:
    status = app["status"] if app else ""
    options = f'<option value=""{"" if status else " selected"}>Not tracking</option>' + "".join(
        f'<option value="{s}"{" selected" if s == status else ""}>{s.capitalize()}</option>'
        for s in _APP_STATUSES
    )
    applied = _attr(app.get("applied_at")) if app else ""
    disabled = " disabled" if (not app or status == "saved") else ""
    notes = _e(app.get("notes")) if app else ""
    hidden_attr = " hidden" if hidden else ""
    return (
        f'<div class="app-panel"{hidden_attr}>'
        f'<label class="app-field">Status <select class="app-status">{options}</select></label>'
        f'<label class="app-field">Applied <input type="date" class="app-date" value="{applied}"{disabled}></label>'
        f'<label class="app-field app-field-notes">Notes '
        f'<textarea class="app-notes" rows="2" maxlength="4000">\n{notes}</textarea></label>'
        "</div>"
    )
```

Add `_APP_STATUSES`-based options for the toolbar in step 4.

3. Thread an `apps: dict[str, dict[str, Any]] | None = None` keyword through `_row_html`, `_never_reviewed_row_html`, `_rows_html`, `_never_reviewed_rows_html` (same way `feedback` is threaded), and at every call site in `render()` that passes `feedback=live_state.feedback if live_state else None` also pass `apps=live_state.applications if live_state else None`. Inside `_row_html` and `_never_reviewed_row_html`, after computing `label`, add:

```python
    app = apps.get(f"{row['source_key']}|{row['job_id']}") if (live and apps) else None
    app_chip = _app_chip_html(app) if live else ""
    app_panel = _app_panel_html(app, hidden=False) if live else ""
```

(for the never-reviewed builder use `candidate[...]` keys and `hidden=True`). Then make exactly these **inline** edits to the two templates so static output stays byte-identical — the new slots go on the **same line** as their neighbours, never on a new line:

- `_row_html`: `          {feedback_buttons}` → `          {feedback_buttons}{app_chip}`; and the `.row-detail` last line `        {f'<div class="detail-meta">{sponsorship_note}</div>' if sponsorship_note else ""}` → the same line followed directly by `{app_panel}`.
- `_never_reviewed_row_html`: `          {feedback_buttons}` → `          {feedback_buttons}{app_chip}`; and the line `      </div>` that closes `.plain-row-top` (the one immediately before the final `    </div>'''`) → `      </div>{app_panel}`.

Also add `data-app-status` to the live row-root attributes: extend `_live_row_attrs` with an `app_status: str = ""` keyword appended as ` data-app-status="{_attr(app_status)}"`, and pass `app_status=app["status"] if app else ""` from both callers.

4. `_LIVE_TOOLBAR`: inside the `<span class="live-toolbar-extra">…</span>`, before the Sort label, add

```
      <label class="toolbar-field">Application <select id="live-app" class="toolbar-select"><option value="">any</option><option value="untracked">not tracked</option><option value="saved">saved</option><option value="applied">applied</option><option value="interviewing">interviewing</option><option value="offer">offer</option><option value="rejected">rejected</option><option value="withdrawn">withdrawn</option></select></label>
      <label class="toolbar-field"><input type="checkbox" id="live-hide-applied"> Hide applied</label>
```

`_LIVE_STYLE`: append

```
  .app-chip { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 999px; border: 1px dashed var(--line); background: transparent; color: var(--ink-soft); cursor: pointer; }
  .app-chip:hover { border-color: var(--accent); color: var(--accent); }
  .app-chip-on { border-style: solid; border-color: var(--accent); background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .app-panel { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 12px 18px; align-items: flex-end; padding: 10px 16px 14px 74px; }
  .plain-row > .app-panel { padding-left: 74px; border-top: 1px dashed var(--line); }
  .app-panel[hidden] { display: none; }
  .app-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--ink-soft); }
  .app-field select, .app-field input, .app-field textarea { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: inherit; }
  .app-field-notes { flex: 1 1 260px; }
  .live-link { color: var(--accent); font-weight: 600; text-decoration: none; padding: 4px 10px; border-radius: 8px; background: var(--surface); border: 1px solid var(--line); }
```

`_live_bar_html`: add `'<a class="live-link" href="/applications">Applications</a>'` as the first child of the `live-bar` div (before the status span).

`_live_script_html`: add to `boot`:

```python
        "applications": {
            k: {"status": v["status"], "applied_at": v.get("applied_at"), "notes": v.get("notes")}
            for k, v in live_state.applications.items()
        },
```

- [ ] **Step 4: Implement the Applications page**

Create `scripts/templates/applications_template.html`:

```html
<!DOCTYPE html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --paper: #F3F6F7; --ink: #14191F; --ink-soft: #4B5560; --surface: #FFFFFF; --line: #DCE3E7;
    --accent: #0C7F91; --accent-soft: #E4F1F3; --muted: #6B7480; --danger: #C1443A;
    --status-good: #1D9A66; --status-warning: #B4790F; --shadow: 0 1px 2px rgba(20, 25, 31, 0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #10151A; --ink: #E9EDEF; --ink-soft: #A6B0B8; --surface: #171E24; --line: #2A333A;
      --accent: #3FC1D4; --accent-soft: #17323A; --muted: #8A95A0; --danger: #E2695E;
      --status-good: #3FCC8C; --status-warning: #E3AA45; --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink); font: 15px/1.5 "IBM Plex Sans", system-ui, sans-serif; }
  main { max-width: 1000px; margin: 0 auto; padding: 32px 16px 96px; }
  h1 { margin: 0 0 4px; font-size: 28px; }
  .app-nav a { color: var(--accent); font-weight: 600; text-decoration: none; }
  .subhead { color: var(--ink-soft); margin: 0 0 20px; }
  #app-counts { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 24px; }
  #app-counts [data-count-status] { padding: 6px 12px; border: 1px solid var(--line); border-radius: 999px; background: var(--surface); font-size: 13px; }
  #app-counts b { font-variant-numeric: tabular-nums; }
  .app-row { display: grid; grid-template-columns: 1fr; gap: 12px; padding: 14px 16px; margin-bottom: 10px; background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--muted); border-radius: 10px; box-shadow: var(--shadow); }
  .app-row[data-status="offer"] { border-left-color: var(--status-good); }
  .app-row[data-status="interviewing"], .app-row[data-status="applied"] { border-left-color: var(--accent); }
  .app-row[data-status="rejected"] { border-left-color: var(--danger); }
  .app-title { font-weight: 600; color: var(--ink); text-decoration: none; }
  .app-title:hover { color: var(--accent); }
  .app-sub { color: var(--ink-soft); font-size: 13px; }
  .posting-open { color: var(--status-good); }
  .posting-closed, .posting-removed { color: var(--status-warning); }
  .app-controls { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: flex-end; }
  .app-controls label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--ink-soft); }
  .app-controls select, .app-controls input, .app-controls textarea { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); color: inherit; }
  .app-notes-label { flex: 1 1 260px; }
  .app-days { color: var(--ink-soft); font-size: 13px; align-self: center; }
  .app-delete { font: inherit; font-size: 13px; padding: 6px 12px; border: 1px solid var(--line); border-radius: 8px; background: transparent; color: var(--danger); cursor: pointer; }
  .app-empty { color: var(--muted); padding: 24px 0; }
  .live-bar { position: fixed; bottom: 24px; right: 24px; z-index: 10; display: flex; flex-direction: column; align-items: flex-end; gap: 6px; font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  .live-pill, .live-notice, .live-reload, .live-archive { padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line); background: var(--surface); box-shadow: var(--shadow); }
  .live-pill[data-state="live"] { color: var(--accent); }
  .live-pill[data-state="offline"] { background: var(--accent); color: var(--surface); }
  .live-notice:empty { display: none; }
  .live-archive { color: var(--muted); }
</style>
<main>
  <p class="app-nav"><a href="/">&larr; Radar</a></p>
  <h1>Applications</h1>
  <p class="subhead">__SUBHEAD__</p>
  <div id="app-counts">__COUNTS__</div>
  <p id="app-empty" class="app-empty"__EMPTY_HIDDEN__>No tracked applications yet &mdash; open the radar and press <b>Track</b> on a job.</p>
  <div id="app-rows">__ROWS__</div>
</main>
<div class="live-bar">
  <span id="live-status" class="live-pill" role="status" data-state="live">Live</span>
  <span id="live-notice" class="live-notice" role="alert"></span>
  <span id="live-reload" class="live-reload" hidden>Changed elsewhere &mdash; <a href="#" id="live-reload-link">Reload</a></span>
  <span class="live-archive">__ARCHIVE__</span>
</div>
__SCRIPTS__
```

Create `scripts/render_applications.py`:

```python
"""Render the live server's /applications page: every tracked application, sorted by status then
applied date, with counts, days-since-applied and whether the posting is still open, closed, or
removed (deleted by `cleanup`). Pure presentation of rows the server reads from SQLite — never
descriptions, never database paths. All text is HTML-escaped; embedded JSON is script-safe."""

from __future__ import annotations

import html
from datetime import date
from typing import Any, Literal

import render_radar
from render_radar import _attr, _e, _json_for_script

STATUS_ORDER = ("offer", "interviewing", "applied", "saved", "rejected", "withdrawn")
_LIFECYCLE = ("saved", "applied", "interviewing", "offer", "rejected", "withdrawn")
_TEMPLATE_PATH = render_radar._TEMPLATE_DIR / "applications_template.html"
_SCRIPTS = ("radar_live_core.js", "radar_live_sync.js", "applications_ui.js")


def days_since(applied_at: str | None, today: date) -> int | None:
    if not applied_at:
        return None
    return (today - date.fromisoformat(applied_at)).days


def posting_state(job_status: str | None) -> Literal["open", "closed", "removed"]:
    if job_status == "active":
        return "open"
    if job_status == "closed":
        return "closed"
    return "removed"


def _days_text(status: str, applied_at: str | None, today: date) -> str:
    days = days_since(applied_at, today) if status != "saved" else None
    if days is None:
        return ""
    if days <= 0:
        return "Applied today"
    return "Applied 1 day ago" if days == 1 else f"Applied {days} days ago"


def _safe_href(url: str) -> str:
    return html.escape(url, quote=True) if url.lower().startswith(("http://", "https://")) else "#"


def _sort_key(app: dict[str, Any]) -> tuple[int, str, str]:
    order = STATUS_ORDER.index(app["status"]) if app["status"] in STATUS_ORDER else len(STATUS_ORDER)
    # newest applied date first (inverted string ordering), undated last, then most recently edited
    applied = app.get("applied_at") or ""
    inverted = "".join(chr(0x10FFFF - ord(c)) for c in applied) if applied else "\U0010ffff"
    updated = "".join(chr(0x10FFFF - ord(c)) for c in app.get("updated_at") or "")
    return order, inverted, updated


def _row_html(app: dict[str, Any], *, posting: str, today: date) -> str:
    status = app["status"]
    options = "".join(
        f'<option value="{s}"{" selected" if s == status else ""}>{s.capitalize()}</option>'
        for s in _LIFECYCLE
    )
    applied = app.get("applied_at") or ""
    disabled = " disabled" if status == "saved" else ""
    meta = " &middot; ".join(
        part for part in (
            _e(app["company"]),
            _e(app.get("location")) if app.get("location") else "",
            f"Score {int(app['score'])}" if app.get("score") is not None else "",
        ) if part
    )
    return (
        f'<article class="app-row" data-source-key="{_attr(app["source_key"])}" '
        f'data-job-id="{_attr(app["job_id"])}" data-status="{_attr(status)}" '
        f'data-applied-at="{_attr(applied)}" data-posting="{posting}">'
        f'<div class="app-main"><a class="app-title" href="{_safe_href(app["url"])}" target="_blank" '
        f'rel="noopener">{_e(app["title"])}</a>'
        f'<div class="app-sub">{meta} &middot; <span class="posting-{posting}">Posting {posting}</span></div></div>'
        '<div class="app-controls">'
        f'<label>Status <select class="app-status">{options}</select></label>'
        f'<label>Applied <input type="date" class="app-date" value="{_attr(applied)}"{disabled}></label>'
        f'<span class="app-days">{_e(_days_text(status, applied or None, today))}</span>'
        f'<label class="app-notes-label">Notes <textarea class="app-notes" rows="2" maxlength="4000">'
        f'\n{_e(app.get("notes"))}</textarea></label>'
        '<button type="button" class="app-delete">Delete</button></div></article>'
    )


def render_applications_page(
    *, applications: list[dict[str, Any]], job_states: dict[str, str], versions: dict[str, str | None],
    today: date, archive_name: str | None = None,
) -> str:
    ordered = sorted(applications, key=_sort_key)
    rows = "".join(
        _row_html(a, posting=posting_state(job_states.get(f"{a['source_key']}|{a['job_id']}")), today=today)
        for a in ordered
    )
    counts = {s: 0 for s in _LIFECYCLE}
    for app in applications:
        counts[app["status"]] = counts.get(app["status"], 0) + 1
    counts_html = f'<span data-count-status="all">Total <b>{len(applications)}</b></span>' + "".join(
        f'<span data-count-status="{s}">{s.capitalize()} <b>{counts[s]}</b></span>' for s in STATUS_ORDER
    )
    scripts = [f"<script>window.__APPS_LIVE__ = {_json_for_script({'versions': versions})};</script>"]
    for name in _SCRIPTS:
        source = (render_radar._TEMPLATE_DIR / name).read_text(encoding="utf-8")
        if "</script" in source.lower():
            raise ValueError(f"{name} must not contain a script terminator")
        scripts.append(f"<script>\n{source}\n</script>")
    page = _TEMPLATE_PATH.read_text(encoding="utf-8")
    subhead = f"{len(applications)} tracked application{'s' if len(applications) != 1 else ''}. Changes save immediately."
    archive = _e(archive_name) if archive_name else "live"
    return (
        page.replace("__TITLE__", "Applications")
        .replace("__SUBHEAD__", _e(subhead))
        .replace("__COUNTS__", counts_html)
        .replace("__EMPTY_HIDDEN__", " hidden" if applications else "")
        .replace("__ARCHIVE__", archive)
        .replace("__ROWS__", rows)
        .replace("__SCRIPTS__", "\n".join(scripts))
    )
```

(Substitution order matters: `__ROWS__` and `__SCRIPTS__` are replaced last so job text or script source containing another token cannot be re-substituted.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_render_radar.py tests/test_render_applications.py tests/test_radar_live_js.py -q && uv run ruff check scripts tests`
Expected: PASS — including the golden static test. If the golden test fails, an inline slot gained whitespace: fix the slot, never the golden.

- [ ] **Step 6: Commit**

```bash
git add scripts/render_radar.py scripts/render_applications.py scripts/templates/applications_template.html tests/test_render_radar.py tests/test_render_applications.py
git commit -m "feat(radar): application chip/panel/filters in live mode and the Applications page" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Server — application routes, exports refresh, versions, and Phase A carry-overs

**Files:**
- Modify: `scripts/serve_radar.py`
- Test: `tests/test_serve_radar.py`

**Interfaces:**
- Consumes: Task 1 storage (`apply_application`, `get_application`, `application_map`, `export_applications`, `job_statuses`, tombstone readers), Task 2 `write_applications_exports`, Task 7 `LiveState(applications=…)` / `render_applications_page`.
- Produces:
  - `serve_radar.ApplicationWrite` (`extra="forbid"`: `source_key, job_id, client_ts`, optional `status: ApplicationStatus | None`, `applied_at: date | None`, `notes: str | None` (≤4000); **a field that is absent from the JSON means "unchanged"**, an explicit `null` means delete (status) / clear (notes) / invalid (applied_at)).
  - `serve_radar.write_application(storage, req, *, now, today) -> tuple[int, dict]`
  - Routes: `POST /api/application`; `GET /api/applications` → `{ok, applications: {key: item}}`; `GET /applications` (HTML); `GET /api/state` now also returns `versions.applications` and `counts.applications`; write replies `{ok, item}` | `{ok, deleted:true}` | `{ok, stale:true, item:<row|null>}`, plus `export_warning` when the export files could not be refreshed. An item is the application row (no descriptions): `source_key, job_id, status, applied_at, notes, company, title, url, location, posted_at, score, salary_evidence, created_at, updated_at`.
  - Phase A carry-overs: untagging feedback for a job that is gone but has a tombstone returns 200 (idempotent); the handler has a 15 s socket `timeout`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_serve_radar.py` (add `import os`-style imports already present; add `import time` and `import socket` at the top):

```python
def post_app(env, seconds_ago, job_id="1", **fields):
    body = {"source_key": "acme", "job_id": job_id, "client_ts": _ts(seconds_ago)}
    body.update(fields)
    return env.request("POST", "/api/application", body)


def test_application_create_update_and_delete_round_trip(env):
    status, _, body = post_app(env, 60, status="applied", notes="referral")
    assert status == 200 and body["ok"] is True
    item = body["item"]
    assert (item["status"], item["notes"], item["company"], item["score"]) == ("applied", "referral", "Acme", 82)
    assert item["applied_at"] == datetime.now().date().isoformat()  # defaults to the server's local today
    assert "description" not in json.dumps(body)
    # partial update: only the notes; status/date unchanged
    _, _, body = post_app(env, 30, notes="second call")
    assert (body["item"]["status"], body["item"]["notes"]) == ("applied", "second call")
    # list endpoint and state version
    listing = env.request("GET", "/api/applications")[2]
    assert listing["applications"]["acme|1"]["notes"] == "second call"
    # delete
    status, _, body = post_app(env, 5, status=None)
    assert status == 200 and body == {"ok": True, "deleted": True}
    assert env.request("GET", "/api/applications")[2]["applications"] == {}


def test_application_snapshot_is_server_derived_and_client_metadata_is_rejected(env):
    assert post_app(env, 5, status="saved", company="Evil Corp")[0] == 400  # extra="forbid"
    assert post_app(env, 4, status="saved", url="https://evil.example")[0] == 400
    post_app(env, 3, status="saved")
    with Storage(env.db) as storage:
        row = storage.get_application("acme", "1")
    assert (row["company"], row["title"], row["url"]) == ("Acme", "ADAS Engineer", "https://example.com/1")


def test_application_validation_errors(env):
    assert post_app(env, 5)[0] == 400  # nothing to change
    assert post_app(env, 5, status="hired")[0] == 400
    assert post_app(env, 5, status="saved", applied_at="2026-09-01")[0] == 400  # saved has no date
    assert post_app(env, 5, notes="no status yet")[0] == 400  # creating needs a status
    assert post_app(env, 5, status="applied", applied_at="2026-02-30")[0] == 400  # not a real date
    assert post_app(env, 5, status="applied", notes="x" * 4001)[0] == 400
    post_app(env, 4, status="applied")
    assert post_app(env, 3, applied_at=None)[0] == 400  # a date can never be cleared
    assert post_app(env, -3600, status="applied")[0] == 400  # future client_ts
    assert env.request("GET", "/api/applications")[2]["applications"]["acme|1"]["status"] == "applied"


def test_application_unknown_job_is_404_but_deleting_a_known_deletion_is_idempotent(env):
    assert post_app(env, 5, job_id="nope", status="saved")[0] == 404
    assert post_app(env, 5, job_id="nope", status=None)[0] == 404
    post_app(env, 60, status="applied")
    with Storage(env.db) as storage:  # job later removed by `cleanup`
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    assert post_app(env, 30, notes="still editable")[0] == 200  # existing snapshot is enough
    assert post_app(env, 20, status=None)[0] == 200
    assert post_app(env, 10, status=None)[0] == 200  # repeated delete of a removed job converges
    assert post_app(env, 5, status="saved")[0] == 404  # ...but a removed job cannot be newly tracked


def test_late_application_writes_cannot_overwrite_newer_edits_or_resurrect_deleted_rows(env):
    post_app(env, 30, status="applied", notes="newer")
    status, _, body = post_app(env, 60, status="offer", notes="older")
    assert status == 200 and body["stale"] is True and body["item"]["status"] == "applied"
    post_app(env, 20, status=None)
    status, _, body = post_app(env, 25, status="applied")
    assert body["stale"] is True and body["item"] is None
    assert env.request("GET", "/api/applications")[2]["applications"] == {}


def test_application_writes_refresh_both_export_files(env):
    post_app(env, 10, status="applied", notes="=HYPERLINK(\"http://evil\")")
    data = env.root / "data"
    exported = json.loads((data / "applications.json").read_text())
    assert exported[0]["job_id"] == "1" and exported[0]["status"] == "applied"
    csv_text = (data / "applications.csv").read_text()
    assert csv_text.splitlines()[0].startswith("source_key,job_id,company")
    assert "'=HYPERLINK" in csv_text  # formula guard
    before = (data / "applications.json").read_text()
    post_app(env, 5, notes="changed")
    assert (data / "applications.json").read_text() != before


def test_export_failure_never_fails_or_reverts_the_save(env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(serve_radar, "write_applications_exports", boom)
    status, _, body = post_app(env, 10, status="applied")
    assert status == 200 and body["ok"] is True and "disk full" in body["export_warning"]
    assert body["item"]["status"] == "applied"
    with Storage(env.db) as storage:
        assert storage.get_application("acme", "1")["status"] == "applied"


def test_state_versions_and_counts_include_applications(env):
    v0 = env.request("GET", "/api/state")[2]
    post_app(env, 30, status="applied")
    v1 = env.request("GET", "/api/state")[2]
    assert v1["versions"]["applications"] != v0["versions"]["applications"]
    assert v1["counts"]["applications"] == 1
    post_app(env, 10, notes="same row count, different content")
    v2 = env.request("GET", "/api/state")[2]["versions"]
    assert v2["applications"] != v1["versions"]["applications"]
    assert v2["feedback"] == v0["versions"]["feedback"]  # feedback version is independent


def test_radar_page_boots_with_application_state_and_link(env):
    post_app(env, 30, status="interviewing", notes="phone screen")
    html = env.request("GET", "/")[2]
    assert 'href="/applications"' in html
    assert '"applications": {"acme|1": {"applied_at":' in html
    assert 'data-app-status="interviewing"' in html


def test_applications_page_lists_rows_and_marks_removed_postings(env):
    post_app(env, 30, status="applied", notes="</script><img src=x onerror=alert(1)>")
    post_app(env, 20, job_id="2", status="saved")
    with Storage(env.db) as storage:
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='2'")
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    status, response, html = env.request("GET", "/applications")
    assert status == 200 and response.getheader("Cache-Control") == "no-store"
    assert 'data-posting="removed"' in html and 'data-posting="closed"' in html
    assert "&lt;/script&gt;&lt;img src=x" in html and "<img src=x" not in html
    assert 'window.__APPS_LIVE__ = {"versions":' in html
    assert "jobs.sqlite3" not in html


def test_applications_page_works_with_no_archive_and_no_applications(env):
    os.remove(env.archive)  # /applications does not depend on the radar archive
    status, _, html = env.request("GET", "/applications")
    assert status == 200 and 'id="app-empty" class="app-empty">' in html


def test_repeated_feedback_untag_of_a_removed_job_is_idempotent(env):
    env.post_feedback("okay", 60)
    with Storage(env.db) as storage:
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    assert env.post_feedback(None, 30)[0] == 200
    assert env.post_feedback(None, 10)[0] == 200  # was a 404 in Phase A
    assert env.post_feedback("okay", 5)[0] == 404  # a removed job still cannot be newly tagged


def test_a_slow_post_body_times_out_instead_of_pinning_a_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_radar.RadarHandler, "timeout", 1)
    e = Env(tmp_path)
    try:
        sock = socket.create_connection(("127.0.0.1", e.port), timeout=10)
        sock.sendall(
            f"POST /api/feedback HTTP/1.1\r\nHost: 127.0.0.1:{e.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 500\r\n\r\n{".encode()
        )
        started = time.monotonic()
        data = sock.recv(4096)  # the server gives up and closes; it must not wait forever
        assert time.monotonic() - started < 8
        assert data == b"" or b"HTTP/1.1" in data or b"HTTP/1.0" in data
        sock.close()
        assert e.request("GET", "/api/state")[0] == 200  # server still healthy
    finally:
        e.close()
```

Also change the Phase A test `test_unknown_job_is_404_but_untagging_an_existing_label_of_a_removed_job_works` only if it now conflicts — it should still pass unchanged (its second half untags an existing label).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_serve_radar.py -q`
Expected: the new tests FAIL (404 for `/api/application`, no `applications` versions, etc.); existing tests pass.

- [ ] **Step 3: Implement**

In `scripts/serve_radar.py`:

1. Imports: `from datetime import UTC, date, datetime, timedelta`; `from job_hunter.applications_export import write_applications_exports`; `from job_hunter.models import ApplicationStatus, FeedbackLabel, JobFeedback`; `import render_applications` (next to `import render_radar`).
2. Replace `FeedbackWrite` with a shared base plus two models:

```python
class _KeyedWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=500)
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


class FeedbackWrite(_KeyedWrite):
    label: FeedbackLabel | None


class ApplicationWrite(_KeyedWrite):
    """Absent field = unchanged. `status: null` deletes; `notes: null` clears; `applied_at: null`
    is rejected by storage (a date can only be replaced, or dropped by moving back to `saved`)."""

    status: ApplicationStatus | None = None
    applied_at: date | None = None
    notes: str | None = Field(default=None, max_length=4000)
```

3. Feedback idempotent untag — in `write_feedback`, change the delete branch's 404 condition to:

```python
        if existing is None and snapshot is None and storage.get_feedback_tombstone(*key) is None:
            return 404, {"ok": False, "error": "unknown job"}
```

4. Add after `write_feedback`:

```python
_APPLICATION_FIELDS = ("status", "applied_at", "notes")


def _public_application(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def write_application(
    storage: Storage, req: ApplicationWrite, *, now: datetime, today: date
) -> tuple[int, dict[str, Any]]:
    """One validated application write. The job snapshot comes from `jobs`/`assessments`
    (never the client) and is used only when tracking starts; last-writer-wins by `client_ts`."""
    if req.client_ts > now + FUTURE_TOLERANCE:
        return 400, {"ok": False, "error": "client_ts is in the future"}
    event_at = min(req.client_ts, now)
    changes = {name: getattr(req, name) for name in _APPLICATION_FIELDS if name in req.model_fields_set}
    if not changes:
        return 400, {"ok": False, "error": "nothing to change: send status, applied_at or notes"}
    key = (req.source_key, req.job_id)
    existing = storage.get_application(*key)
    job = storage.get_job_snapshot(*key)
    snapshot = {**job, "score": storage.get_assessment_score(*key)} if job else None
    deleting = "status" in changes and changes["status"] is None
    if existing is None and snapshot is None and not (
        deleting and storage.get_application_tombstone(*key) is not None
    ):
        return 404, {"ok": False, "error": "unknown job"}
    try:
        outcome, row = storage.apply_application(
            *key, event_at=event_at, changes=changes, snapshot=snapshot, today=today
        )
    except ValueError as exc:  # includes pydantic ValidationError from the Application invariants
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid application"
        return 400, {"ok": False, "error": message}
    if outcome == "stale":
        return 200, {"ok": True, "stale": True, "item": _public_application(row)}
    if outcome == "deleted":
        return 200, {"ok": True, "deleted": True}
    return 200, {"ok": True, "item": _public_application(row)}
```

5. Versions: rename `_feedback_version` to `_rows_version` (same body) and change `_versions` to

```python
def _versions(
    cfg: ServerConfig, rows: list[dict[str, Any]], archive: Path | None,
    app_rows: list[dict[str, Any]],
) -> dict[str, str | None]:
    return {
        "archive": _file_version(archive),
        "assessments": _file_version(cfg.args.assessments),
        "feedback": _rows_version(rows),
        "applications": _rows_version(app_rows),
    }
```

6. `render_page`: also read `app_rows = storage.export_applications()` and `apps = storage.application_map()` inside the `with Storage(...)`, build `LiveState(feedback=feedback, versions=_versions(cfg, rows, archive, app_rows), archive_name=archive.name, applications=apps)`.
7. Add a page function:

```python
def render_applications(cfg: ServerConfig) -> str:
    try:
        archive_name: str | None = _resolve_archive(cfg.args).name
    except FileNotFoundError:
        archive_name = None  # the Applications page does not depend on the radar archive
    with Storage(cfg.settings.database_path) as storage:
        rows = storage.export_applications()
        feedback_rows = storage.export_job_feedback()
        states = storage.job_statuses((r["source_key"], r["job_id"]) for r in rows)
    return render_applications.render_applications_page(
        applications=rows, job_states=states,
        versions=_versions(cfg, feedback_rows, None, rows) | {"archive": None},
        today=date.today(), archive_name=archive_name,
    )
```

(name the local function `render_applications_html` to avoid shadowing the imported module `render_applications`; call `render_applications.render_applications_page(...)` inside it.)

8. `do_GET`: extend `/api/state` to read `app_rows` and return `"versions": _versions(cfg, rows, archive, app_rows)`, `"counts": {"feedback": len(rows), "applications": len(app_rows)}`; add branches

```python
            elif path == "/applications":
                self._send(200, render_applications_html(cfg).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/applications":
                with Storage(cfg.settings.database_path) as storage:
                    mapping = storage.application_map()
                self._json(200, {"ok": True, "applications": {k: _public_application(v) for k, v in mapping.items()}})
```

9. `do_POST`: replace the fixed-path check and the model/handler section with a small route table. Define at module level

```python
_POST_ROUTES = {"/api/feedback": FeedbackWrite, "/api/application": ApplicationWrite}
```

and in `do_POST`: `model = _POST_ROUTES.get(self.path.split("?", 1)[0])`; `if model is None: 404`; validate with `model.model_validate(json.loads(body))`; then dispatch:

```python
        try:
            with Storage(self.server.cfg.settings.database_path) as storage:
                if isinstance(req, FeedbackWrite):
                    status, response = write_feedback(storage, req, now=datetime.now(UTC))
                else:
                    status, response = write_application(
                        storage, req, now=datetime.now(UTC), today=date.today()
                    )
                    if status == 200 and not response.get("stale"):
                        try:
                            write_applications_exports(
                                self.server.cfg.settings.database_path.parent, storage.export_applications()
                            )
                        except Exception as exc:  # noqa: BLE001 - the save is already committed
                            traceback.print_exc()
                            response["export_warning"] = str(exc)
            self._json(status, response)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
```

10. Add `timeout = 15  # seconds: a client that stalls mid-request cannot pin a thread forever` as a class attribute of `RadarHandler`.
11. Update the module docstring's first paragraph to mention application tracking, and `non_loopback_warning`'s text is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_serve_radar.py tests/test_render_radar.py tests/test_render_applications.py tests/test_storage.py -q && uv run ruff check scripts tests src`
Expected: PASS. If the slow-body test flakes, its assertions are deliberately loose (server must close/respond within 8 s and stay healthy); do not weaken them further.

- [ ] **Step 5: Commit**

```bash
git add scripts/serve_radar.py tests/test_serve_radar.py
git commit -m "feat(radar): application routes, Applications page, export refresh, idempotent untag" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: Docs, skill version, full verification, smoke

**Files:**
- Modify: `docs/SPEC.md`, `README.md`, `CLAUDE.md`, `skills/job-radar/SKILL.md`, `docs/live-radar-dashboard-plan.md`

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Update `docs/SPEC.md`**

- After §8.5 add a new subsection **§8.5b `applications` — one row per `(source_key, job_id)`, a human-tracked application**: columns (`status` ∈ saved/applied/interviewing/offer/rejected/withdrawn, `applied_at` local date, `notes` ≤ 4000, the snapshot fields `company/title/url/location/posted_at/score/salary_evidence` taken once at creation from `jobs`/`assessments`, `created_at`, `updated_at`); invariants (saved ⇒ no date; first non-saved status defaults the date to the server's local today; explicit date must be real; a date can't be cleared; snapshot immutable); last-writer-wins by event time with `application_tombstones` (migration v4); no foreign key to `jobs`, so `cleanup` never deletes a row (a posting deleted by cleanup shows as "removed"); written only by `scripts/serve_radar.py`; exported to `data/applications.json`/`.csv` (fixed columns, formula guard) after each write and by `job-hunter export-applications`; an export refresh failure is reported as `export_warning`, never fails the save.
- §8.6: extend the tombstone sentence to also cover `application_tombstones` and add "`applications` rows are never deleted by cleanup".
- §9 CLI reference: add `export-applications`. §10 Scripts: add `render_applications.py` and extend the `serve_radar.py` row with `/applications`, `/api/applications`, `POST /api/application`.

- [ ] **Step 2: Update `README.md` and `CLAUDE.md`**

- README live-mode note: add the Track chip/panel, the Applications page (`/applications`), the two extra filters (Application, Hide applied), `job-hunter export-applications`, and that application data lives in SQLite (exports are refreshed on each save).
- `CLAUDE.md`: extend the `scripts/serve_radar.py` bullet (application routes; every UI edit sends the full record; export files refreshed after each committed write, failure → `export_warning`); extend the `storage.py` description (six tables → add `applications`/`application_tombstones`; never deleted by `cleanup`); add `applications_export.py` (CSV formula guard) and `radar_live_sync.js` (shared outbox engine, node-tested) to the relevant lists.

- [ ] **Step 3: Skill and design-doc updates**

- `skills/job-radar/SKILL.md`: bump `version: 1.3.0` → `1.4.0`; extend the "Live option" note with one sentence: the live report also tracks applications (Track chip → `/applications`, `job-hunter export-applications`).
- `docs/live-radar-dashboard-plan.md`: set the top `Status` line to "Phase A implemented (merged); Phase B implemented on branch `live-radar-phase-b`"; in §3.2 note that `application_tombstones` was added alongside `applications` in migration v4; in §4.2 note the shipped panel design (a `Track` chip in `.row-end`; the editor panel lives in `.row-detail` for scored rows and toggles under the row for unreviewed rows; edits autosave and always send the full record); in §5.2 note `GET /api/applications` returns `{ok, applications: {key: item}}`.
- Confirm only `job-radar` changed: `grep -n "^version:" skills/*/SKILL.md`.

- [ ] **Step 4: Full verification**

Run:

```bash
uv run ruff check .
uv run pytest -q
node --test tests/js/radar_live_core.test.js tests/js/radar_live_sync.test.js
node --check scripts/templates/radar_live_ui.js && node --check scripts/templates/applications_ui.js
```

Expected: ruff clean; pytest = only the 2 baseline `test_collector.py` failures; node tests pass. Report the baseline failures separately.

- [ ] **Step 5: Manual smoke on a temp project (never the real `data/`)**

```bash
export SMOKE=$(mktemp -d)
uv run python <path-to>/seed_smoke_project.py "$SMOKE"     # the Phase A seed script (11 jobs)
uv run python scripts/serve_radar.py --project "$SMOKE" --open
```

In the browser check each, ticking it off:

- [ ] Every row shows a **Track** chip; clicking it on a scored row opens the row and focuses the status select; on an unreviewed (NR) row it toggles a panel under the row.
- [ ] Choosing **Applied** sets the date to today, the chip reads "Applied", and `sqlite3 "$SMOKE/data/jobs.sqlite3" "select job_id,status,applied_at,notes,company,score from applications"` shows the row (company/score server-derived); `$SMOKE/data/applications.json` and `.csv` exist and match.
- [ ] Editing notes saves on blur; changing status then notes quickly leaves **both** changes in the row.
- [ ] Choose **Saved** → the date input disables and the date clears; choose **Not tracking** (with notes) → confirm dialog → the row disappears and `applications` is empty; cancelling the dialog restores the panel.
- [ ] Toolbar: **Application → applied** shows only tracked-applied rows; **Hide applied** hides applied/interviewing/offer/rejected/withdrawn but keeps saved and untracked; filters survive reload via the URL hash.
- [ ] **/applications** (bottom-right "Applications" link): rows sorted offer → interviewing → applied → saved → rejected → withdrawn, counts correct, "Applied N days ago", posting state; edit a status/date/notes and delete a row there; the radar page (in another tab) converges after focus/poll.
- [ ] Offline: stop the server, change a status on the radar → "Offline (1 unsaved)"; restart the server → it flushes and reads "Live"; the same on `/applications`.
- [ ] Simulate `cleanup` removing a job (`sqlite3 "$SMOKE/data/jobs.sqlite3" "delete from jobs where job_id='1'"`) → reload `/applications`: the row is still there and reads "Posting removed"; it can still be edited/deleted.
- [ ] `uv run python scripts/render_radar.py --project "$SMOKE" --output "$SMOKE/static.html"` → static page has no Track chips or live pill and still exports feedback.
- [ ] `uv run job-hunter export-applications --project "$SMOKE"` rewrites both files identically (run twice, `diff`).

Record the outcome in the PR description; remove the temp project: `rm -rf "$SMOKE"`.

- [ ] **Step 6: Commit**

```bash
git add docs/SPEC.md README.md CLAUDE.md skills/job-radar/SKILL.md docs/live-radar-dashboard-plan.md
git commit -m "docs: live radar Phase B — applications, exports, skill version" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review (spec coverage, done inline)

- **Spec §3.1 models:** `ApplicationStatus`, `Application` → Task 1. **§3.2 migration:** v4 `applications` (+ `application_tombstones`, an addition so a deleted application cannot be resurrected) → Task 1. **§3.3 storage API:** `get_application`, `application_map`/`export_applications` (ordered `updated_at DESC`), guarded write/delete → Task 1 (the spec's `upsert_application`/`delete_application` are realized as the single `apply_application`, since create/update/delete share one event-time rule). `delete_closed_jobs` regression → Task 1.
- **Application rules:** creation only for a known job (404 otherwise), first-applied defaulting date, saved ⇒ null date, explicit date validation, snapshot immutability, delete confirmation (UI) → Tasks 1, 5, 6, 8.
- **§4.2 template/UI:** compact Track chip in `.row-end`, full controls in `.row-detail` (plain rows: toggled panel — a documented design choice, since unreviewed rows have no detail), Applications page, application filters + Hide applied, live bar link → Tasks 5–7.
- **§4.3 client rules (Phase B parts):** outbox/optimistic/retry for applications through the shared engine, tab reconciliation (`reconcileApps`), polling → Tasks 3–6.
- **§5 server:** routes, versions, request checks reused, `export_warning` → Task 8. **§5.4 exports:** helper, CSV guard, CLI, refresh after write, failure tolerated → Tasks 2, 8.
- **Phase A carry-overs:** idempotent untag, POST socket timeout → Task 8 (the `applyFilters`-after-reply carry-over shipped in Phase A's final fix wave).
- **Docs/skills:** Task 9. **Tests/acceptance (spec §8):** application upsert/partial/delete, snapshot immutability, export order, cleanup preservation, stale/tombstone protection (Task 1); routes, unknown-job 404, server-derived snapshot, application survives cleanup and archive disappearance, export refresh (Task 8); acceptance #4 (statuses, cleanup survival, /applications, both exports) → Tasks 1, 2, 8, 9.
- **Placeholder scan:** none (every code step contains the code; the one "see Phase A seed script" reference in Task 9 points at an existing file created in the previous phase's session and is only a manual-smoke convenience).
- **Type consistency:** `apply_application(source_key, job_id, *, event_at, changes, snapshot, today)`, `get_application`, `application_map`, `export_applications`, `job_statuses`, tombstone readers, `LiveState(..., applications=...)`, `render_applications_page(applications=, job_states=, versions=, today=, archive_name=)`, `write_application(storage, req, *, now, today)`, `write_applications_exports(directory, rows)`, `createSync(opts)`/`{queue, flush, restored, items, pending}`, `reconcileApps`, `rowPasses(..., appStatus)` are used identically across Tasks 1–8.
