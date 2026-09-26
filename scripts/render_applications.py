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
