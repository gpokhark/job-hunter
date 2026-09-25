from __future__ import annotations

from datetime import UTC, datetime

from ..models import JobDetail, JobSummary
from ..normalizer import extract_job_posting_ld, stringify
from .base import SchemaError
from .json_api import ConfigurableJsonAdapter

# JSON-LD `text` fields that together make up the posting body (see fetch_detail).
_BODY_FIELDS = ("description", "responsibilities", "qualifications", "jobBenefits")


def _parse_month_first_date(value: object) -> datetime | None:
    """Brose's JSON-LD reports `datePosted` as "MM-DD-YYYY" (e.g. "09-25-2026") — not ISO,
    and unlike "YYYY-MM-DD" it can't safely go through parse_flexible_date (a day-first
    reading of the same digits would silently produce a wrong date rather than fail)."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%m-%d-%Y").replace(tzinfo=UTC)
    except ValueError:
        return None


class BroseAdapter(ConfigurableJsonAdapter):
    """brose.com's career listing page is a client-rendered table fed by one static, public,
    unauthenticated JSON file (`/de-en/technisch/joblist.json`, found in the page's own
    inline `$.ajax` call — not the career2.successfactors.eu link its job pages point to,
    which is a login-flow shell with no job content). It's a top-level list of
    {title, url, place[], country, division[], mode, jobId, ...}: no dates and no
    descriptions, so the generic `ConfigurableJsonAdapter` listing config covers it as-is.

    Each job's own page (`item.url`, the same page a human opens) carries a schema.org
    JobPosting JSON-LD block with `datePosted`, structured `addressRegion`/`addressCountry`,
    and the body split across four fields — the plain `description` is only the intro, while
    responsibilities/qualifications/benefits hold the rest (and the qualifications a
    visa-sponsorship statement would live in), so all four are concatenated. This overrides
    only `fetch_detail`, because the generic one expects the detail URL to return JSON."""

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        posting = extract_job_posting_ld(response.text)
        if posting is None:
            raise SchemaError("Brose job page missing JobPosting JSON-LD")
        body = "\n\n".join(
            text.strip() for name in _BODY_FIELDS if (text := stringify(posting.get(name)))
        )
        address = (posting.get("jobLocation") or {}).get("address") or {}
        if not isinstance(address, dict):
            address = {}
        return JobDetail(
            description=body or None,
            state=stringify(address.get("addressRegion")),
            country=stringify(address.get("addressCountry")),
            posted_at=_parse_month_first_date(posting.get("datePosted")),
        )
