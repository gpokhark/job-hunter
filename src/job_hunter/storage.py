from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Assessment, HealthStatus, Job, JobFeedback, SourceHealth
from .sponsorship import evaluate_sponsorship


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
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
                salary_min REAL, salary_max REAL, salary_currency TEXT, content_hash TEXT,
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
        """CREATE TABLE IF NOT EXISTS never adds a column to a table that already exists —
        an existing database predates visa_sponsorship/sponsorship_evidence, so add them
        explicitly if missing. Existing rows backfill the next time each job is
        successfully re-fetched, same as any other collected field."""
        existing = {row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")}
        if "visa_sponsorship" not in existing:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN visa_sponsorship TEXT NOT NULL DEFAULT 'unmentioned'"
            )
        if "sponsorship_evidence" not in existing:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN sponsorship_evidence TEXT")

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
