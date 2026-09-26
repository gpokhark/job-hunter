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
