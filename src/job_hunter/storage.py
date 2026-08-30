from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import HealthStatus, Job, SourceHealth


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
            """
        )
        self.connection.commit()

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
