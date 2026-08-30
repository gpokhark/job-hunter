from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

_RELATIVE_TODAY = re.compile(r"\bposted\s+today\b", re.I)
_RELATIVE_YESTERDAY = re.compile(r"\bposted\s+yesterday\b", re.I)
_RELATIVE_DAYS_AGO = re.compile(r"\bposted\s+(\d+)(\+?)\s*days?\s+ago\b", re.I)
# type="application/ld+json" can appear anywhere in the opening tag (e.g. Astemo's
# <script id="js-job-posting" type="application/ld+json">), not necessarily right after
# <script — anchoring on "<script type=..." specifically misses tags like that one.
_LD_JSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_relative_posted(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse relative posting text ("Posted Today", "Posted 3 Days Ago", "Posted 30+ Days
    Ago") into an absolute UTC datetime, as used by Workday and similar ATS listings. A
    "N+" bucket is treated as N+1 days old — imprecise beyond N days, but precise enough
    to fall on the correct side of any day-based recency cutoff."""
    if not text:
        return None
    now = now or datetime.now(UTC)
    if _RELATIVE_TODAY.search(text):
        return now
    if _RELATIVE_YESTERDAY.search(text):
        return now - timedelta(days=1)
    match = _RELATIVE_DAYS_AGO.search(text)
    if match:
        days = int(match.group(1)) + (1 if match.group(2) else 0)
        return now - timedelta(days=days)
    return None


_DISPLAY_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d")


def parse_display_date(text: str | None) -> datetime | None:
    """Parse a human-formatted date shown in a UI (e.g. SuccessFactors' "Aug 10, 2026")
    into a UTC datetime. Tries a handful of common formats used by these career sites."""
    cleaned = normalize_text(text)
    if not cleaned:
        return None
    for fmt in _DISPLAY_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


_LIFERAY_PUBLICATION_DATE = re.compile(r'publicationDate\s*:\s*"([^"]+)"')
_LIFERAY_DATE_FORMAT = "%b %d, %Y %I:%M:%S %p"


def parse_liferay_publication_date(text: str) -> datetime | None:
    """Find a Liferay DDM career page's inline `JobOfferData.publicationDate` JS-object
    literal (e.g. "Jun 24, 2026 6:42:50 AM" — used by Honda Research Institute USA and
    potentially other Liferay-DDM-backed sites) and parse it to a UTC datetime."""
    match = _LIFERAY_PUBLICATION_DATE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _LIFERAY_DATE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def extract_job_posting_ld(text: str) -> dict | None:
    """Find a schema.org JobPosting JSON-LD block in a page's HTML — a common SEO
    convention (used for Google Jobs rich results) across many otherwise-unrelated career
    site platforms, not specific to any one adapter."""
    for match in _LD_JSON.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        candidates: list = data.get("@graph", [data]) if isinstance(data, dict) else []
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return None


def description_hash(description: str | None) -> str | None:
    normalized = normalize_text(description)
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else None


def canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def fallback_job_id(company: str, title: str, location: str | None, url: str) -> str:
    payload = "|".join(
        [
            normalize_text(company).lower(),
            normalize_text(title).lower(),
            normalize_text(location).lower(),
            canonical_url(url),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
