from __future__ import annotations

from typing import Any

from ..models import JobSummary
from .json_api import ConfigurableJsonAdapter


def _format_location(item: dict[str, Any], primary: str | None) -> str | None:
    """Folds Ashby's `secondaryLocations` (extra hiring regions on a multi-location
    posting) and a `workplaceType`/`isRemote`-derived "Remote" tag into one combined
    location string. Using only the primary `location` field hides a posting's other
    eligible regions — e.g. a role whose primary label is "Toronto" but that's also
    open to "United States" via `secondaryLocations` reads as Canada-only, and
    `evaluate_location`'s structured-country rejection only ever defers to
    `location_raw` text when that text actually contains U.S. evidence (see
    location.py's "Multi-location text can override an exclusively non-US structured
    primary location" branch) — so without this fold-in, a genuinely US-eligible
    multi-location posting would be wrongly excluded. Independently confirmed by
    career-ops' own `ashby.mjs` provider, which carries the identical fix for the
    identical reason."""
    parts: list[str] = []
    if primary:
        parts.append(primary)
    for secondary in item.get("secondaryLocations") or []:
        if not isinstance(secondary, dict):
            continue
        location = secondary.get("location")
        if isinstance(location, str) and location.strip():
            parts.append(location.strip())
        postal = ((secondary.get("address") or {}).get("postalAddress")) or {}
        for key in ("addressLocality", "addressCountry"):
            value = postal.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

    # workplaceType wins whenever present: boards in the wild carry isRemote=true
    # together with workplaceType="Hybrid" for office-anchored roles, and trusting
    # isRemote alone would mislabel those "Remote" and defeat the arrangement/location
    # signal. isRemote is only the fallback for payloads that omit workplaceType.
    workplace_type = str(item.get("workplaceType") or "").strip().lower()
    is_remote = workplace_type == "remote" if workplace_type else bool(item.get("isRemote"))
    if is_remote and not any("remote" in part.lower() for part in parts):
        parts.append("Remote")

    seen: set[str] = set()
    deduped: list[str] = []
    for part in parts:
        key = part.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(part)
    return " · ".join(deduped) if deduped else primary


class AshbyAdapter(ConfigurableJsonAdapter):
    """Same JSON-listing kernel as ConfigurableJsonAdapter, except `location_raw` is
    enriched with secondary hiring regions and a derived remote tag — see
    `_format_location` above."""

    async def fetch_summaries(self) -> list[JobSummary]:
        summaries = await super().fetch_summaries()
        for summary in summaries:
            summary.location_raw = _format_location(summary.raw, summary.location_raw)
        return summaries
