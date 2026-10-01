"""Background collection: `job-hunter collect start|status|stop` and `job-hunter snapshot`.

A detached process runs `Collector.search()` while the user filters/reviews/renders against
whatever SQLite already holds. The runner writes a progress state file after every source and
never writes an archive mid-run (so it cannot clobber `refilter_archive.py`'s in-place rewrite);
`snapshot` builds an archive on demand from that state plus SQLite. The runner holds only the
`collector` lock, never the shared `job-hunter` lock. See
docs/superpowers/specs/2026-09-30-rate-limit-and-background-collection-design.md section 5.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from .atomic import atomic_write_text
from .collector import Collector, select_companies
from .config import CandidateProfile, CompanyConfig, Settings
from .models import SourceHealth
from .runlock import RunLockHeld, pid_alive, process_start_time, run_lock
from .search_archive import archive_path

LOGGER = logging.getLogger(__name__)

STATE_PATH = Path("data/collect/state.json")
LOG_PATH = Path("data/collect/collector.log")

# Aliases so tests can patch them without touching the shared `subprocess`/`os` modules.
_popen = subprocess.Popen
_kill = os.kill

SourceStatus = Literal["pending", "running", "ok", "warning", "failed", "unsupported", "skipped"]
RunStatus = Literal["running", "complete", "stopped", "failed"]
_FINISHED: frozenset[str] = frozenset({"ok", "warning", "failed", "unsupported"})


class SourceProgress(BaseModel):
    source_key: str
    company: str
    status: SourceStatus = "pending"
    job_count: int = 0
    message: str | None = None
    error_type: str | None = None
    failure_kind: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    attempted_at: datetime | None = None
    finished_at: datetime | None = None


class CollectState(BaseModel):
    run_id: str
    pid: int
    pid_start_time: str | None = None
    status: RunStatus = "running"
    slow: bool = False
    companies_filter: str | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    archive: str | None = None
    error: str | None = None
    sources: list[SourceProgress]


class CollectorRunning(RuntimeError):
    pass


def read_state(path: Path = STATE_PATH) -> CollectState | None:
    try:
        return CollectState.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def write_state(state: CollectState, path: Path = STATE_PATH) -> None:
    state.updated_at = datetime.now(UTC)
    atomic_write_text(path, state.model_dump_json(indent=2) + "\n")


def is_live(state: CollectState) -> bool:
    """PID alive AND still the same process (guards against PID reuse), like `pipeline-status`."""
    if not pid_alive(state.pid):
        return False
    if state.pid_start_time is not None:
        current = process_start_time(state.pid)
        if current is not None and current != state.pid_start_time:
            return False
    return True


def effective_status(state: CollectState) -> str:
    if state.status == "running" and not is_live(state):
        return "abandoned"
    return state.status


def _apply_health(source: SourceProgress, health: SourceHealth) -> None:
    source.status = health.status.value  # type: ignore[assignment]
    source.job_count = health.job_count
    source.message = health.message
    source.error_type = health.error_type
    source.failure_kind = health.failure_kind
    source.http_status = health.http_status
    source.retry_after_seconds = health.retry_after_seconds
    source.attempted_at = health.attempted_at
    source.finished_at = datetime.now(UTC)


async def run_collection(
    settings: Settings,
    companies: list[CompanyConfig],
    profile: CandidateProfile,
    *,
    slow: bool,
    companies_filter: str | None = None,
    state_path: Path = STATE_PATH,
    stop_event: asyncio.Event | None = None,
) -> CollectState:
    with run_lock("collector"):
        now = datetime.now(UTC)
        state = CollectState(
            run_id=str(uuid4()), pid=os.getpid(), pid_start_time=process_start_time(os.getpid()),
            slow=slow, companies_filter=companies_filter, started_at=now, updated_at=now,
            sources=[SourceProgress(source_key=c.key, company=c.company) for c in companies],
        )
        write_state(state, state_path)
        by_key = {s.source_key: s for s in state.sources}

        stop = stop_event or asyncio.Event()
        if stop_event is None:
            # Not the main thread / unsupported platform: stop via stop_event only.
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)

        def progress(event: str, key: str, health: SourceHealth | None) -> None:
            source = by_key[key]
            if event == "start":
                source.status = "running"
                source.attempted_at = datetime.now(UTC)
            elif event == "skipped":
                source.status = "skipped"
            elif event == "done" and health is not None:
                _apply_health(source, health)
            write_state(state, state_path)

        delay = 0.0
        if slow:
            background = settings.collection.background
            settings = settings.model_copy(
                update={
                    "collection": settings.collection.model_copy(
                        update={"max_concurrent_sources": background.max_concurrent_sources}
                    )
                }
            )
            delay = background.source_delay_seconds

        try:
            result = await Collector(
                settings, companies, profile, source_delay_seconds=delay
            ).search(progress=progress, stop_event=stop)

            if stop.is_set():
                state.status = "stopped"
                for source in state.sources:
                    if source.status in {"pending", "running"}:
                        source.status = "skipped"
            else:
                path = archive_path(None, companies=companies_filter)
                atomic_write_text(
                    path,
                    json.dumps(result.model_dump(mode="json"), indent=2, default=str, ensure_ascii=False)
                    + "\n",
                )
                state.archive = str(path)
                state.status = "complete"
        except Exception as exc:  # recorded, not swallowed: the state file is the report
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            state.completed_at = datetime.now(UTC)
            # Best-effort: a state-write failure must not replace the original error.
            try:
                write_state(state, state_path)
            except Exception:
                LOGGER.warning("could not record failed collection state", exc_info=True)
            raise
        state.completed_at = datetime.now(UTC)
        write_state(state, state_path)
        return state


def start_background(
    *, project_root: Path, companies: str | None, slow: bool, state_path: Path = STATE_PATH
) -> int:
    existing = read_state(state_path)
    if existing is not None and existing.status == "running" and is_live(existing):
        raise CollectorRunning(
            f"a collector is already running (pid {existing.pid}, run {existing.run_id}); "
            "use `job-hunter collect status` or `collect stop`"
        )
    log_path = Path(project_root) / LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "job_hunter.background", "--project", str(project_root)]
    if companies:
        cmd += ["--companies", companies]
    if slow:
        cmd.append("--slow")
    with log_path.open("ab") as handle:
        proc = _popen(
            cmd, cwd=project_root, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True,
        )
    return proc.pid


def request_stop(state_path: Path = STATE_PATH) -> bool:
    """Cooperative stop: SIGTERM sets the runner's stop event; in-flight sources finish."""
    state = read_state(state_path)
    if state is None or state.status != "running" or not is_live(state):
        return False
    _kill(state.pid, signal.SIGTERM)
    return True


def format_status(state: CollectState) -> list[str]:
    finished = sum(1 for s in state.sources if s.status in _FINISHED)
    lines = [
        f"collector run {state.run_id}: {effective_status(state)} (pid {state.pid}), "
        f"{finished}/{len(state.sources)} sources finished"
    ]
    for source in state.sources:
        if source.failure_kind:
            reason = source.failure_kind.replace("_", " ")
            if source.http_status:
                reason += f" (HTTP {source.http_status})"
            lines.append(f"  {reason}: {source.company} — kept {source.job_count} job(s)")
        elif source.status == "failed":
            lines.append(f"  failed: {source.company} — {source.message or 'no message'}")
    if state.archive:
        lines.append(f"archive: {state.archive}")
    return lines


def cli_collect(args, state_path: Path = STATE_PATH) -> int:
    command = args.collect_command
    if command == "start":
        try:
            pid = start_background(
                project_root=Path.cwd(), companies=args.companies, slow=args.slow,
                state_path=state_path,
            )
        except CollectorRunning as exc:
            print(f"job-hunter: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"started": True, "pid": pid, "state": str(state_path), "log": str(LOG_PATH)}))
        return 0
    if command == "stop":
        if request_stop(state_path):
            print("stop requested; in-flight sources will finish, the rest are skipped")
            return 0
        print("job-hunter: no live collector to stop", file=sys.stderr)
        return 2
    state = read_state(state_path)
    if state is None:
        print("job-hunter: no collection has been started in this project", file=sys.stderr)
        return 2
    if args.json:
        payload = state.model_dump(mode="json")
        payload["status"] = effective_status(state)
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(format_status(state)))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the detached process: `python -m job_hunter.background`."""
    from .config import load_companies, load_profile, load_settings
    from .rootutil import add_project_argument, chdir_to_project_root

    parser = argparse.ArgumentParser(prog="job_hunter.background")
    parser.add_argument("--companies", default=None)
    parser.add_argument("--slow", action="store_true")
    add_project_argument(parser)
    args = parser.parse_args(argv)
    chdir_to_project_root(args.project)
    settings = load_settings()
    companies = select_companies(load_companies(), args.companies)
    state = asyncio.run(
        run_collection(
            settings, companies, load_profile(),
            slow=args.slow, companies_filter=args.companies,
        )
    )
    return 0 if state.status in {"complete", "stopped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())


def snapshot_archive(state: CollectState, *, now: datetime | None = None) -> dict:
    """A minimal archive for refilter: `source_health` holds only sources that have *finished*
    (so refilter's `_successful_source_scope` and the radar's Collection Issues reflect exactly
    what has been collected so far); `candidates` is empty — refilter rebuilds it from SQLite."""
    now = now or datetime.now(UTC)
    health = [
        {
            "source_key": s.source_key, "company": s.company, "status": s.status,
            "job_count": s.job_count, "message": s.message, "error_type": s.error_type,
            "failure_kind": s.failure_kind, "http_status": s.http_status,
            "retry_after_seconds": s.retry_after_seconds,
            "attempted_at": (s.attempted_at or now).isoformat(),
        }
        for s in state.sources
        if s.status in _FINISHED
    ]
    succeeded = sum(1 for h in health if h["status"] in {"ok", "warning"})
    return {
        "run": {
            "run_id": state.run_id,
            "started_at": state.started_at.isoformat(),
            "completed_at": now.isoformat(),
        },
        "summary": {
            "sources_attempted": len(health),
            "sources_succeeded": succeeded,
            "sources_failed": len(health) - succeeded,
            "jobs_observed": sum(h["job_count"] for h in health),
            "us_eligible": 0,
            "prefilter_candidates": 0,
            "stale_excluded": 0,
            "partial_failure": 0 < succeeded < len(health),
        },
        "source_health": health,
        "candidates": [],
    }


def write_snapshot(state: CollectState, *, now: datetime | None = None) -> Path:
    # Its own name: never the same-day default archive a foreground `search --archive` writes.
    path = archive_path("collect-snapshot", companies=state.companies_filter)
    # Held only for the write, released before refilter (which takes the same lock itself).
    with run_lock("job-hunter"):
        atomic_write_text(
            path, json.dumps(snapshot_archive(state, now=now), indent=2, ensure_ascii=False) + "\n"
        )
    return path


def refilter_snapshot(path: Path, project_root: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "scripts/refilter_archive.py", "--project", str(project_root),
         "--search", str(path), "--no-report"],
        cwd=project_root, check=False,
    )
    return proc.returncode


def cli_snapshot(state_path: Path = STATE_PATH) -> int:
    state = read_state(state_path)
    if state is None:
        print(
            "job-hunter: no collection state found; run `job-hunter collect start` first",
            file=sys.stderr,
        )
        return 2
    if not any(s.status in _FINISHED for s in state.sources):
        print("job-hunter: no source has finished yet; try again shortly", file=sys.stderr)
        return 2
    try:
        path = write_snapshot(state)
    except RunLockHeld as exc:
        print(
            f"job-hunter: another job-hunter run is in progress ({exc}); "
            "try again when it finishes",
            file=sys.stderr,
        )
        return 2
    code = refilter_snapshot(path, Path.cwd())
    if code != 0:
        print(f"job-hunter: refilter of {path} failed (exit {code})", file=sys.stderr)
        return code
    print(path)
    print(f"next: job-hunter pipeline --no-scrape --search {path}   (add --review to score new jobs)")
    return 0
