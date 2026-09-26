from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from .location import code_appears_in_own_text, evaluate_location, is_ambiguous_state_country_code
from .models import (
    Application,
    ApplicationStatus,
    Assessment,
    HealthStatus,
    Job,
    JobFeedback,
    SourceHealth,
    WorkArrangement,
)
from .salary import evaluate_salary
from .sponsorship import evaluate_sponsorship


def _migrate_v1_add_sponsorship_columns(connection: sqlite3.Connection) -> None:
    """`jobs.visa_sponsorship`/`sponsorship_evidence`, added when `sponsorship.py` shipped —
    `CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists, so a
    database created before these columns existed needs them added explicitly. Column-presence-
    guarded (safe to run against a database that already has them, from `CREATE TABLE`'s own
    current definition or a prior un-versioned run of this same check) — existing rows backfill
    the next time each job is successfully re-fetched, same as any other collected field."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "visa_sponsorship" not in existing:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN visa_sponsorship TEXT NOT NULL DEFAULT 'unmentioned'"
        )
    if "sponsorship_evidence" not in existing:
        connection.execute("ALTER TABLE jobs ADD COLUMN sponsorship_evidence TEXT")


def _migrate_v2_add_salary_evidence_column(connection: sqlite3.Connection) -> None:
    """`jobs.salary_evidence`, added when `salary.py` shipped — same column-presence-guarded
    shape as migration 1, for the same reason."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "salary_evidence" not in existing:
        connection.execute("ALTER TABLE jobs ADD COLUMN salary_evidence TEXT")


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


#: Ordered, 1-indexed migration steps — entry `N` (1-based) upgrades a database from schema
#: version `N-1` to version `N`. Tracked via SQLite's own built-in `PRAGMA user_version` integer
#: (docs/agent-runtime-audit.md's "no explicit schema-version marker" finding) rather than a
#: separate table — `_migrate()` applies only the entries a given database's current
#: `user_version` hasn't seen yet, then advances `user_version` to `len(_MIGRATIONS)`. These first
#: two entries are a *refactor* of the mechanism, not a behavior change: they're the exact same
#: column-presence checks `_migrate()` already ran unconditionally on every call before this —
#: wrapping them in numbered, skippable steps is what lets a *future* migration (a rename, a type
#: change, a data backfill with side effects — something that can't just be re-run harmlessly)
#: rely on an accurate "has this database already seen this specific change" instead of
#: re-deriving it from scratch each time.
_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1_add_sponsorship_columns,
    _migrate_v2_add_salary_evidence_column,
    _migrate_v3_create_feedback_tombstones,
    _migrate_v4_create_application_tables,
]


_MISSING = object()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        # Two job-hunter/agent processes (e.g. a search run and a concurrent review run) can
        # legitimately hit the same SQLite file at once; without a busy_timeout, a writer that
        # loses the race to WAL/lock contention fails immediately with "database is locked"
        # instead of waiting briefly for the other transaction to finish.
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                source_key TEXT NOT NULL, company TEXT NOT NULL, job_id TEXT NOT NULL,
                source_platform TEXT NOT NULL, title TEXT NOT NULL, canonical_url TEXT NOT NULL,
                location_raw TEXT, city TEXT, state TEXT, country TEXT, work_arrangement TEXT,
                us_eligible INTEGER NOT NULL, location_confidence TEXT, location_evidence TEXT,
                visa_sponsorship TEXT NOT NULL DEFAULT 'unmentioned', sponsorship_evidence TEXT,
                department TEXT, employment_type TEXT, posted_at TEXT, description TEXT,
                salary_min REAL, salary_max REAL, salary_currency TEXT, salary_evidence TEXT,
                content_hash TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', missing_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source_key, job_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
                status TEXT NOT NULL, sources_attempted INTEGER NOT NULL DEFAULT 0,
                sources_succeeded INTEGER NOT NULL DEFAULT 0, jobs_observed INTEGER NOT NULL DEFAULT 0,
                candidates_returned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS source_health (
                source_key TEXT PRIMARY KEY, company TEXT NOT NULL, last_attempt_at TEXT,
                last_success_at TEXT, last_job_count INTEGER, consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_status TEXT, last_error_type TEXT, last_error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS assessments (
                source_key TEXT NOT NULL, job_id TEXT NOT NULL, company TEXT NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, content_hash TEXT,
                score INTEGER NOT NULL, recommended INTEGER NOT NULL,
                matches TEXT NOT NULL, gaps TEXT NOT NULL, resume_path TEXT,
                assessed_at TEXT NOT NULL,
                PRIMARY KEY(source_key, job_id)
            );
            CREATE TABLE IF NOT EXISTS job_feedback (
                source_key TEXT NOT NULL, job_id TEXT NOT NULL, company TEXT NOT NULL,
                title TEXT NOT NULL, department TEXT, score INTEGER,
                label TEXT NOT NULL, recorded_at TEXT NOT NULL,
                PRIMARY KEY(source_key, job_id)
            );
            """
        )
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        """Applies every `_MIGRATIONS` entry a database's own `PRAGMA user_version` (SQLite's
        built-in integer schema-version pragma, defaulting to `0` for a database that's never
        set it) hasn't seen yet, then advances `user_version` to `len(_MIGRATIONS)` — see
        `_MIGRATIONS`' own docstring for why this is a mechanism refactor, not a behavior change,
        for the two migrations that exist today. Idempotent by construction: a database already
        at the current version runs zero migrations on a second call (confirmed in
        `tests/test_storage.py`)."""
        current_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        for version, migration in enumerate(_MIGRATIONS, start=1):
            if version <= current_version:
                continue
            migration(self.connection)
        if current_version < len(_MIGRATIONS):
            # PRAGMA doesn't accept bound parameters for its value -- safe here since
            # len(_MIGRATIONS) is a fixed, program-controlled integer, never user input.
            self.connection.execute(f"PRAGMA user_version = {len(_MIGRATIONS)}")

    def begin_run(self, run_id: str, started_at: datetime) -> None:
        self.connection.execute(
            "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
            (run_id, started_at.isoformat()),
        )
        self.connection.commit()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        attempted: int,
        succeeded: int,
        observed: int,
        candidates: int,
    ) -> None:
        self.connection.execute(
            """UPDATE runs SET completed_at=?, status=?, sources_attempted=?, sources_succeeded=?,
               jobs_observed=?, candidates_returned=? WHERE run_id=?""",
            (
                datetime.now(UTC).isoformat(),
                status,
                attempted,
                succeeded,
                observed,
                candidates,
                run_id,
            ),
        )
        self.connection.commit()

    def get_job(self, source_key: str, job_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM jobs WHERE source_key=? AND job_id=?", (source_key, job_id)
        ).fetchone()

    def upsert_job(self, job: Job) -> Job:
        prior = self.get_job(job.source_key, job.job_id)
        job.is_new = prior is None
        job.is_changed = bool(
            prior and job.content_hash and prior["content_hash"] != job.content_hash
        )
        if prior:
            job.first_seen_at = datetime.fromisoformat(prior["first_seen_at"])
        values = {
            "source_key": job.source_key,
            "company": job.company,
            "job_id": job.job_id,
            "source_platform": job.source_platform,
            "title": job.title,
            "canonical_url": job.url,
            "location_raw": job.location_raw,
            "city": job.city,
            "state": job.state,
            "country": job.country,
            "work_arrangement": job.work_arrangement.value,
            "us_eligible": int(job.us_eligible),
            "location_confidence": job.location_confidence.value,
            "location_evidence": job.location_evidence,
            "visa_sponsorship": job.visa_sponsorship.value,
            "sponsorship_evidence": job.sponsorship_evidence,
            "department": job.department,
            "employment_type": job.employment_type,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "description": job.description,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_currency": job.salary_currency,
            "salary_evidence": job.salary_evidence,
            "content_hash": job.content_hash,
            "first_seen_at": job.first_seen_at.isoformat(),
            "last_seen_at": job.last_seen_at.isoformat(),
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        updates = ", ".join(
            f"{key}=excluded.{key}"
            for key in values
            if key not in {"source_key", "job_id", "first_seen_at"}
        )
        self.connection.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT(source_key, job_id) DO UPDATE SET {updates}, status='active', missing_count=0",
            values,
        )
        self.connection.commit()
        return job

    def reevaluate_sponsorship(self) -> int:
        """Re-run sponsorship.evaluate_sponsorship against every stored job's *existing*
        description — purely local, no network involved. Needed because a job's
        visa_sponsorship column is only ever set by upsert_job, i.e. only on a fresh
        successful collection of that specific job; a source that's failing (rate limits,
        outages) or a sponsorship.py phrase-list improvement never retroactively updates
        rows already sitting in the database with a stale or default value. Returns how
        many rows' classification actually changed."""
        rows = self.connection.execute(
            "SELECT source_key, job_id, description, visa_sponsorship, sponsorship_evidence FROM jobs"
        ).fetchall()
        changed = 0
        for row in rows:
            decision = evaluate_sponsorship(row["description"])
            if (
                decision.status.value != row["visa_sponsorship"]
                or decision.evidence != row["sponsorship_evidence"]
            ):
                self.connection.execute(
                    "UPDATE jobs SET visa_sponsorship=?, sponsorship_evidence=? "
                    "WHERE source_key=? AND job_id=?",
                    (decision.status.value, decision.evidence, row["source_key"], row["job_id"]),
                )
                changed += 1
        self.connection.commit()
        return changed

    def reevaluate_salary(self) -> int:
        """Re-run salary.evaluate_salary against every stored job's *existing*
        description — same rationale and shape as reevaluate_sponsorship above: a job's
        salary_evidence column is only ever set by upsert_job (on a fresh successful
        collection), so this is what backfills every already-stored job collected
        before salary_evidence existed, or picks up a salary.py pattern improvement,
        with no network involved."""
        rows = self.connection.execute(
            "SELECT source_key, job_id, description, salary_evidence FROM jobs"
        ).fetchall()
        changed = 0
        for row in rows:
            decision = evaluate_salary(row["description"])
            if decision.evidence != row["salary_evidence"]:
                self.connection.execute(
                    "UPDATE jobs SET salary_min=?, salary_max=?, salary_currency=?, salary_evidence=? "
                    "WHERE source_key=? AND job_id=?",
                    (
                        decision.min_value,
                        decision.max_value,
                        "USD" if decision.evidence else None,
                        decision.evidence,
                        row["source_key"],
                        row["job_id"],
                    ),
                )
                changed += 1
        self.connection.commit()
        return changed

    def reevaluate_location(self) -> int:
        """Re-run evaluate_location, purely locally (no network), against every stored job
        whose current classification came from an ambiguous U.S. state/country code (e.g.
        "IN"/"DE"/"CA", each also a real country's own code) — needed for the same reason
        as reevaluate_sponsorship/reevaluate_salary above: us_eligible/state/country are
        only ever set by upsert_job on a fresh successful collection, so a location.py
        disambiguation fix never retroactively corrects a job already sitting in the
        database with a stale classification.

        Deliberately narrower than reevaluate_sponsorship/reevaluate_salary: only touches a
        row when the stored ambiguous code is textually present in that job's own
        location_raw (`location.code_appears_in_own_text`) — e.g. Magna's "Maharashtra, IN"
        or Volkswagen's "München, DE, 80807", where the wrong classification is entirely
        self-contained in the stored text. A row where an ambiguous-looking stored state
        instead came from a richer structured field an adapter fetched but never persisted
        verbatim (e.g. a Workday detail fetch resolving state="CA" from full detail text,
        while location_raw stored only a generic "2 Locations" listing-level placeholder)
        has no local evidence to safely re-derive from — re-running it without that
        external context would wrongly flip a genuinely-U.S. job to non-U.S. Confirmed live
        against production data: this scoping avoids flipping 84 real Johnson & Johnson/
        NVIDIA/Dematic jobs while still correctly fixing every confirmed Magna/Volkswagen/
        Google/Intuitive misclassification. Returns how many rows' classification actually
        changed."""
        rows = self.connection.execute(
            "SELECT source_key, job_id, location_raw, state, country, work_arrangement, "
            "description, us_eligible, location_confidence, location_evidence FROM jobs "
            "WHERE location_evidence LIKE 'recognized U.S. state:%'"
        ).fetchall()
        changed = 0
        for row in rows:
            stored_state = row["state"]
            if not stored_state or not is_ambiguous_state_country_code(stored_state):
                continue
            if not code_appears_in_own_text(stored_state, row["location_raw"]):
                continue
            try:
                arrangement = (
                    WorkArrangement(row["work_arrangement"]) if row["work_arrangement"] else None
                )
            except ValueError:
                arrangement = None
            decision = evaluate_location(
                row["location_raw"], description=row["description"], arrangement=arrangement
            )
            if (
                decision.us_eligible != bool(row["us_eligible"])
                or decision.evidence != row["location_evidence"]
                or (decision.state or None) != row["state"]
                or (decision.country or None) != row["country"]
                or decision.confidence.value != row["location_confidence"]
            ):
                self.connection.execute(
                    "UPDATE jobs SET state=?, country=?, us_eligible=?, location_confidence=?, "
                    "location_evidence=? WHERE source_key=? AND job_id=?",
                    (
                        decision.state,
                        decision.country,
                        int(decision.us_eligible),
                        decision.confidence.value,
                        decision.evidence,
                        row["source_key"],
                        row["job_id"],
                    ),
                )
                changed += 1
        self.connection.commit()
        return changed

    def mark_missing(
        self, source_key: str, observed_ids: Iterable[str], stale_before: datetime | None = None
    ) -> None:
        """Increment missing_count for this source's active jobs not present in
        observed_ids, then close anything that's missed 3 runs in a row.

        stale_before excludes jobs already older than a source's own recency-based
        pagination cutoff (see apple.py/adp_recruiting.py) from this accounting entirely
        — such a source stops paginating once postings are provably stale, so an
        already-old job will *always* look "missing" from that point on. Without this
        exclusion it would get falsely marked closed after 3 runs, even if still live on
        the real site; excluding it just freezes its last-known status instead.
        """
        ids = list(observed_ids)
        clauses = ["source_key=?", "status='active'"]
        params: list[Any] = [source_key]
        if ids:
            clauses.append(f"job_id NOT IN ({','.join('?' for _ in ids)})")  # noqa: S608
            params.extend(ids)
        if stale_before is not None:
            clauses.append("(posted_at IS NULL OR posted_at >= ?)")
            params.append(stale_before.isoformat())
        self.connection.execute(
            f"UPDATE jobs SET missing_count=missing_count+1 WHERE {' AND '.join(clauses)}",  # noqa: S608
            params,
        )
        self.connection.execute(
            "UPDATE jobs SET status='closed' WHERE source_key=? AND missing_count>=3", (source_key,)
        )
        self.connection.commit()

    def update_health(self, health: SourceHealth) -> None:
        prior = self.connection.execute(
            "SELECT * FROM source_health WHERE source_key=?", (health.source_key,)
        ).fetchone()
        succeeded = health.status in {HealthStatus.OK, HealthStatus.WARNING}
        failures = 0 if succeeded else ((prior["consecutive_failures"] if prior else 0) + 1)
        last_success = (
            health.attempted_at.isoformat()
            if succeeded
            else (prior["last_success_at"] if prior else None)
        )
        self.connection.execute(
            """INSERT INTO source_health(source_key, company, last_attempt_at, last_success_at,
               last_job_count, consecutive_failures, last_status, last_error_type, last_error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_key) DO UPDATE SET company=excluded.company,
               last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at,
               last_job_count=excluded.last_job_count, consecutive_failures=excluded.consecutive_failures,
               last_status=excluded.last_status, last_error_type=excluded.last_error_type,
               last_error_message=excluded.last_error_message""",
            (
                health.source_key,
                health.company,
                health.attempted_at.isoformat(),
                last_success,
                health.job_count,
                failures,
                health.status.value,
                health.error_type,
                health.message,
            ),
        )
        self.connection.commit()

    def previous_job_count(self, source_key: str) -> int | None:
        row = self.connection.execute(
            "SELECT last_job_count FROM source_health WHERE source_key=?", (source_key,)
        ).fetchone()
        return row[0] if row else None

    def health_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute("SELECT * FROM source_health ORDER BY source_key")
        ]

    def stats(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for status in ("active", "closed"):
            result[status] = self.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status=?", (status,)
            ).fetchone()[0]
        result["runs"] = self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        result["sources"] = self.connection.execute(
            "SELECT COUNT(*) FROM source_health"
        ).fetchone()[0]
        return result

    def export_active(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection.execute("SELECT * FROM jobs WHERE status='active'")
        ]

    def upsert_assessment(self, assessment: Assessment) -> None:
        self.connection.execute(
            """INSERT INTO assessments(source_key, job_id, company, title, url, content_hash,
               score, recommended, matches, gaps, resume_path, assessed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_key, job_id) DO UPDATE SET company=excluded.company,
               title=excluded.title, url=excluded.url, content_hash=excluded.content_hash,
               score=excluded.score, recommended=excluded.recommended, matches=excluded.matches,
               gaps=excluded.gaps, resume_path=excluded.resume_path, assessed_at=excluded.assessed_at""",
            (
                assessment.source_key,
                assessment.job_id,
                assessment.company,
                assessment.title,
                assessment.url,
                assessment.content_hash,
                assessment.score,
                int(assessment.recommended),
                json.dumps(assessment.matches),
                json.dumps(assessment.gaps),
                assessment.resume_path,
                assessment.assessed_at.isoformat(),
            ),
        )
        self.connection.commit()

    @staticmethod
    def _row_to_assessment(row: sqlite3.Row) -> Assessment:
        return Assessment(
            source_key=row["source_key"],
            job_id=row["job_id"],
            company=row["company"],
            title=row["title"],
            url=row["url"],
            content_hash=row["content_hash"],
            score=row["score"],
            recommended=bool(row["recommended"]),
            matches=json.loads(row["matches"]),
            gaps=json.loads(row["gaps"]),
            resume_path=row["resume_path"],
            assessed_at=row["assessed_at"],
        )

    def all_assessments(self) -> dict[tuple[str, str], Assessment]:
        """Every recorded assessment, keyed by (source_key, job_id), for the collector to
        attach to a matching candidate — only when that candidate's current content_hash
        still matches (see `Assessment`'s docstring)."""
        rows = self.connection.execute("SELECT * FROM assessments").fetchall()
        return {(row["source_key"], row["job_id"]): self._row_to_assessment(row) for row in rows}

    def get_valid_assessment(
        self, source_key: str, job_id: str, content_hash: str | None
    ) -> Assessment | None:
        """A single-job lookup for callers (e.g. `scripts/review_with_lm_studio.py`) that
        want to skip already-reviewed jobs one at a time rather than loading every
        assessment up front. Returns None if never assessed, or if assessed against
        different content (the posting changed since that review — see `Assessment`'s
        docstring)."""
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE source_key=? AND job_id=?", (source_key, job_id)
        ).fetchone()
        if row is None or row["content_hash"] != content_hash:
            return None
        return self._row_to_assessment(row)

    def partition_candidates_for_review(
        self, candidates: list[dict[str, Any]], *, force: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        """Splits an archive's candidate dicts (each needing at least `source_key`/`job_id`/
        `content_hash` keys) into (to_review, skipped_cached), applying the identical
        `get_valid_assessment` cache-hit rule `scripts/review_with_lm_studio.py` applies before
        ever calling the model. Shared so a caller that only wants a cheap pre-flight count —
        `job-hunter pipeline`'s "N need review" status line, printed before spawning the review
        subprocess — doesn't have to duplicate this loop or actually run a review to get it."""
        to_review: list[dict[str, Any]] = []
        skipped_cached = 0
        for candidate in candidates:
            content_hash = candidate.get("content_hash")
            if not force and self.get_valid_assessment(candidate["source_key"], candidate["job_id"], content_hash):
                skipped_cached += 1
                continue
            to_review.append(candidate)
        return to_review, skipped_cached

    def export_assessments(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM assessments ORDER BY assessed_at DESC"
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["matches"] = json.loads(item["matches"])
            item["gaps"] = json.loads(item["gaps"])
            item["recommended"] = bool(item["recommended"])
            results.append(item)
        return results

    def upsert_job_feedback(self, feedback: JobFeedback) -> None:
        """Upsert, never append — a later label for the same (source_key, job_id) replaces the
        earlier one. This is load-bearing: a reviewer correcting an earlier click (e.g. relabeling
        a job from "okay" to "irrelevant" after reconsidering) must land on one current row, not
        accumulate contradictory history. See docs/feedback-exclusion-plan.md section 8."""
        self.connection.execute(
            """INSERT INTO job_feedback(source_key, job_id, company, title, department, score,
               label, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_key, job_id) DO UPDATE SET company=excluded.company,
               title=excluded.title, department=excluded.department, score=excluded.score,
               label=excluded.label, recorded_at=excluded.recorded_at""",
            (
                feedback.source_key,
                feedback.job_id,
                feedback.company,
                feedback.title,
                feedback.department,
                feedback.score,
                feedback.label,
                feedback.recorded_at.isoformat(),
            ),
        )
        self.connection.commit()

    def export_job_feedback(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM job_feedback ORDER BY recorded_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

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

    def find_stale_closed_jobs(self, before: datetime) -> list[dict[str, Any]]:
        """Jobs eligible for `job-hunter cleanup`: `status='closed'` and not observed
        (`last_seen_at`) since `before`. Read-only — `last_seen_at` is never touched by
        `mark_missing()` when it flips a job to closed, so it's already exactly "the last time
        this job was confirmed present," the right anchor for "days since closed" with no
        schema migration needed. Used for both a dry-run preview and, immediately before
        `delete_closed_jobs`, to know exactly what's about to be removed (for the pre-delete
        export — see docs/retention-cleanup-plan.md section 3.8/4)."""
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status='closed' AND last_seen_at < ? ORDER BY last_seen_at",
            (before.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_closed_jobs(self, before: datetime) -> dict[str, list[dict[str, Any]]]:
        """Deletes every closed job last seen before `before`, cascading to that job's own
        `assessments`/`job_feedback` rows — an explicit choice (docs/retention-cleanup-plan.md
        section 3.6/4), not an oversight: neither table has a real foreign key to `jobs`, so
        leaving them behind wouldn't break anything, but a deleted job's assessment/feedback has
        nowhere else to attach once its own row is gone. Returns exactly what was deleted from
        each table — the caller must export this *before* calling this method returns to disk,
        since the rows won't exist to re-query afterward."""
        jobs = self.find_stale_closed_jobs(before)
        deleted: dict[str, list[dict[str, Any]]] = {"jobs": jobs, "assessments": [], "job_feedback": []}
        for job in jobs:
            key = (job["source_key"], job["job_id"])
            assessment_row = self.connection.execute(
                "SELECT * FROM assessments WHERE source_key=? AND job_id=?", key
            ).fetchone()
            if assessment_row:
                deleted["assessments"].append(dict(assessment_row))
            feedback_row = self.connection.execute(
                "SELECT * FROM job_feedback WHERE source_key=? AND job_id=?", key
            ).fetchone()
            if feedback_row:
                deleted["job_feedback"].append(dict(feedback_row))
            self.connection.execute("DELETE FROM jobs WHERE source_key=? AND job_id=?", key)
            self.connection.execute("DELETE FROM assessments WHERE source_key=? AND job_id=?", key)
            self.connection.execute("DELETE FROM job_feedback WHERE source_key=? AND job_id=?", key)
        self.connection.commit()
        return deleted

    def vacuum(self) -> None:
        """Rewrites the whole file to actually reclaim the disk space `delete_closed_jobs`
        frees — SQLite's DELETE only frees pages for internal reuse, it does not shrink the
        file on disk on its own (docs/retention-cleanup-plan.md section 3.2). Must be called
        with no other transaction in progress; `delete_closed_jobs` already commits before
        returning, so a caller invoking these in sequence is safe."""
        self.connection.execute("VACUUM")
