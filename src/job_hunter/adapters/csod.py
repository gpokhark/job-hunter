from __future__ import annotations

import html as html_module
import re

from ..models import JobDetail, JobSummary
from ..normalizer import (
    extract_job_posting_ld,
    normalize_text,
    parse_display_date,
    parse_flexible_date,
    stringify,
)
from .base import JobAdapter, SchemaError

_TOKEN_RE = re.compile(r'"token":"([^"]+)"')


class CsodAdapter(JobAdapter):
    """Cornerstone OnDemand (CSOD) "Career Site Player" — used by Forvia Hella
    (hella.csod.com). The visible careersite (`ux/ats/careersite/{id}/home`) is an
    Angular shell with no job data in its own plain HTML, but every page load embeds a
    short-lived anonymous JWT in `csod.context.token` (`sub: -102`, a guest user, not a
    logged-in one) whose own `rurls` claim scopes it to a fixed allowlist of API paths —
    the same "public frontend key minted by an unauthenticated page load" shape as
    bosch.py's static Bearer key and adp_recruiting.py's myJobsToken, not a session/CSRF
    replay. Rendering the listing page once with Playwright (capturing XHR) showed it
    replayed as a Bearer header on `POST .../rec-job-search/external/jobs` — confirmed
    genuinely anonymous by re-minting the token from a second, independent plain
    httpx/curl GET (no browser, no cookies) and replaying it with the same result.
    That listing endpoint's own `externalDescription` field is a truncated plain-text
    summary, not the full posting (confirmed directly: missing the qualifications/
    benefits/contact sections present on the requisition's own detail page) — so
    fetch_detail instead fetches the public, token-free requisition detail page
    (`.../home/requisition/{id}`) and reads its schema.org JobPosting JSON-LD block via
    the same extract_job_posting_ld helper html_paginated.py uses for an unrelated
    platform. One quirk confirmed live: Hella's JSON-LD uses PascalCase keys
    (DatePosted/Title/Description/HiringOrganization) rather than the schema.org-standard
    camelCase html_paginated.py's own usage assumes, so both cases are checked here."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        token_url = cfg.get("token_url")
        list_api_url = cfg.get("list_api_url")
        career_site_id = cfg.get("career_site_id")
        detail_url_template = cfg.get("detail_url_template")
        if not token_url or not list_api_url or not career_site_id or not detail_url_template:
            raise SchemaError(
                "token_url/list_api_url/career_site_id/detail_url_template is not configured"
            )
        token = await self._fetch_token(token_url)
        page_size = int(cfg.get("page_size", 250))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        jobs: list[JobSummary] = []
        for page in range(1, int(cfg.get("max_pages", 10)) + 1):
            payload = {
                "careerSiteId": career_site_id,
                "careerSitePageId": career_site_id,
                "pageNumber": page,
                "pageSize": page_size,
                "cultureId": 1,
                "searchText": "",
                "cultureName": "en-US",
                "states": [],
                "countryCodes": [],
                "cities": [],
                "placeID": "",
                "radius": None,
                "postingsWithinDays": None,
                "customFieldCheckboxKeys": [],
                "customFieldDropdowns": [],
                "customFieldRadios": [],
            }
            response = await self.request("POST", list_api_url, json=payload, headers=headers)
            data = response.json().get("data") or {}
            items = data.get("requisitions")
            if not isinstance(items, list):
                raise SchemaError("rec-job-search response missing data.requisitions list")
            if not items:
                break
            jobs.extend(self._parse_job(item, detail_url_template) for item in items)
            total = int(data.get("totalCount", 0) or 0)
            if len(jobs) >= total:
                break
        return jobs

    async def _fetch_token(self, token_url: str) -> str:
        # A plain, anonymous GET — the same page a browser loads with no session at all.
        # The embedded JWT (sub: -102, a guest user) is a public frontend credential, not
        # something requiring login; see class docstring.
        response = await self.request("GET", token_url)
        match = _TOKEN_RE.search(response.text)
        if not match:
            raise SchemaError("careersite page did not embed a csod.context.token")
        return match.group(1)

    def _parse_job(self, item: dict, detail_url_template: str) -> JobSummary:
        req_id = str(item.get("requisitionId") or "").strip()
        title = str(item.get("displayJobTitle") or "").strip()
        if not req_id or not title:
            raise SchemaError("CSOD requisition missing requisitionId/displayJobTitle")
        # A posting can list multiple locations (confirmed live, e.g. one German role
        # spanning three cities) — take the first, the same convention adp_recruiting.py
        # uses for the same shape; location.py's structured-field path still applies.
        locations = item.get("locations") or []
        first = locations[0] if locations else {}
        city = stringify(first.get("city"))
        state = stringify(first.get("state"))
        country = stringify(first.get("country"))
        location_raw = ", ".join(part for part in (city, state, country) if part) or None
        return JobSummary(
            source_key=self.source_key,
            source_platform=self.company.platform or "csod",
            company=self.company.company,
            job_id=req_id,
            title=title,
            url=detail_url_template.format(id=req_id),
            location_raw=location_raw,
            city=city,
            state=state,
            country=country,
            # "M/D/YYYY" display format (e.g. "7/30/2026") — parse_display_date's
            # "%m/%d/%Y" format string, not parse_flexible_date (ISO/epoch only).
            posted_at=parse_display_date(item.get("postingEffectiveDate")),
            raw=item,
        )

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        # Public HTML, no token needed — confirmed via plain curl for both a US and a
        # non-US requisition.
        response = await self.request("GET", summary.url)
        posting = extract_job_posting_ld(response.text)
        if not posting:
            return JobDetail()
        description = posting.get("Description") or posting.get("description")
        posted_at = posting.get("DatePosted") or posting.get("datePosted")
        return JobDetail(
            description=normalize_text(html_module.unescape(description)) if description else None,
            posted_at=parse_flexible_date(posted_at),
        )
