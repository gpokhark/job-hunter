from __future__ import annotations

import html
import re
from typing import Any

from ..models import JobDetail, JobSummary
from .json_api import ConfigurableJsonAdapter

_WORK_MODEL = re.compile(r"^(?:hybrid|in[-\s]?office|on[-\s]?site|distributed|remote|flexible)$", re.I)
_OFFICES_URL_RE = re.compile(r"^(https://[^/]+/v1/boards/[^/]+)/jobs(?:$|[?#])")


def _is_work_model_only(location: str | None) -> bool:
    """True when a Greenhouse location string carries only a work model ("Hybrid",
    "Distributed; Hybrid") with no actual geography at all. Confirmed as a real pattern
    on some Greenhouse boards (not Anthropic's, checked live — this is a real bug class
    for a *future* Greenhouse company, not one we've hit yet): the real city lives only
    in a separate `/offices` endpoint the `/jobs` list never returns, so a plain
    `location.name` mapping leaves `evaluate_location` with nothing but the work-model
    word and no U.S. evidence. Anything with a place in it ("Hybrid - London") already
    has usable text and is left alone. Mirrors career-ops' `greenhouse.mjs`
    `isWorkModelOnly()`, which hit this exact pattern independently."""
    if not location:
        return False
    parts = [part.strip() for part in location.split(";") if part.strip()]
    return bool(parts) and all(_WORK_MODEL.match(part) for part in parts)


def _offices_url(list_url: str) -> str | None:
    """boards/{slug}/jobs -> boards/{slug}/offices. None for any other shape (e.g. a
    single-job URL), which disables enrichment rather than guessing at an endpoint."""
    match = _OFFICES_URL_RE.match(list_url)
    return f"{match.group(1)}/offices" if match else None


def _build_office_map(payload: Any) -> dict[Any, set[str]]:
    """job_id -> set(office names), walking offices -> departments -> jobs. A job
    listed under several offices keeps all of them, matching a genuinely multi-site
    role."""
    office_map: dict[Any, set[str]] = {}

    def walk(offices: Any) -> None:
        if not isinstance(offices, list):
            return
        for office in offices:
            if not isinstance(office, dict):
                continue
            name = str(office.get("name") or "").strip()
            if name:
                for department in office.get("departments") or []:
                    if not isinstance(department, dict):
                        continue
                    for job in department.get("jobs") or []:
                        if not isinstance(job, dict) or job.get("id") is None:
                            continue
                        office_map.setdefault(job["id"], set()).add(name)
            walk(office.get("children"))

    walk(payload.get("offices") if isinstance(payload, dict) else None)
    return office_map


class GreenhouseAdapter(ConfigurableJsonAdapter):
    """Same JSON-listing kernel as ConfigurableJsonAdapter — Greenhouse's own `content`
    field is HTML-entity-double-encoded (confirmed live against Anthropic's board:
    literally the characters `&lt;div class=&quot;...&quot;&gt;`, not `<div
    class="...">`), needing one extra html.unescape() before it's real HTML — the same
    double-encoding shape apple.py already handles for an unrelated reason (a JSON string
    embedded in a JS assignment). Every other field (title, location, department) is
    plain text and unaffected.

    Also enriches `location_raw` for any job whose location is work-model-only (see
    `_is_work_model_only`) by fetching that board's `/offices` endpoint once — but only
    when at least one job actually needs it, since that endpoint can be large (career-ops'
    own comment: "Datadog's /offices is 2.8MB") and most boards, Anthropic's included,
    never need it at all. Best-effort: a board with no `/offices`, or a failed fetch,
    falls back to the bare work-model string rather than failing the whole listing —
    this is enrichment on top of an already-complete job list, not the adapter's primary
    data, so it doesn't carry the same fail-loudly obligation `SchemaError`/`AdapterError`
    do elsewhere in this codebase."""

    async def fetch_summaries(self) -> list[JobSummary]:
        summaries = await super().fetch_summaries()
        if not any(_is_work_model_only(summary.location_raw) for summary in summaries):
            return summaries
        offices_url = _offices_url(self.company.config.get("list_url", ""))
        if not offices_url:
            return summaries
        try:
            response = await self.request("GET", offices_url)
            office_map = _build_office_map(response.json())
        except Exception:
            return summaries
        for summary in summaries:
            if not _is_work_model_only(summary.location_raw):
                continue
            offices = office_map.get(summary.raw.get("id"))
            if offices:
                summary.location_raw = " · ".join([summary.location_raw, *sorted(offices)])
        return summaries

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        detail = await super().fetch_detail(summary)
        if detail.description:
            detail.description = html.unescape(detail.description)
        return detail
