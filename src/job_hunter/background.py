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
import os
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from .atomic import atomic_write_text
from .collector import Collector, select_companies
from .config import CandidateProfile, CompanyConfig, Settings
from .models import SourceHealth
from .runlock import pid_alive, process_start_time, run_lock
from .search_archive import archive_path

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
        except Exception as exc:  # recorded, not swallowed: the state file is the report
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            state.completed_at = datetime.now(UTC)
            write_state(state, state_path)
            raise

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
        state.completed_at = datetime.now(UTC)
        write_state(state, state_path)
        return state


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
