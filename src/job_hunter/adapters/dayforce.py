from __future__ import annotations

import html as html_module

from ..models import JobDetail, JobSummary
from ..normalizer import normalize_text, parse_flexible_date, stringify
from ..prefilter import is_recent
from .base import JobAdapter, SchemaError

_BASE_URL = "https://jobs.dayforcehcm.com"


class DayforceAdapter(JobAdapter):
    """Ceridian Dayforce Candidate Portal — used by JTEKT
    (jobs.dayforcehcm.com/en-US/JTEKT/CANDIDATEPORTAL). Its visible portal is a Next.js
    SPA with no job data in its own server-rendered HTML; rendering the listing once
    with Playwright and reading its own XHR calls showed the real backend is
    `POST /api/geo/{clientNamespace}/jobposting/search`. That endpoint 403s with no
    cookie/header at all — not a Cloudflare bot block (no JS challenge, no
    cf-mitigated header), but a NextAuth double-submit CSRF guard: a genuinely
    anonymous `GET /api/auth/csrf` (no login, no session) hands back a csrfToken and a
    matching `__Host-next-auth.csrf-token` cookie, both of which must be replayed
    (token as an `x-csrf-token` header, cookie via the same client) on the search POST
    — the same "public two-call handshake" shape as adp_recruiting.py's myJobsToken,
    confirmed live with plain httpx and this repo's own non-browser User-Agent, no
    browser needed. The search response's own `jobPostings[]` items carry the full,
    untruncated job description plus structured postingLocations inline, so no
    separate per-job detail fetch is needed — same shape as adp_recruiting.py's
    apply-custom-filters endpoint. Pagination is a `paginationStart` offset in the
    POST body (fixed page size of 25 — `paginationSize`/`top`/`pageSize` overrides in
    the body are silently ignored, confirmed live), continuing until `offset + count
    >= maxCount`. Listing order is confirmed sorted newest-first by
    postingStartTimestampUTC across the full catalog, so a `max_posting_age_days`-based
    early pagination stop is safe here the same way it is for apple.py/
    adp_recruiting.py."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        namespace = cfg.get("client_namespace")
        if not namespace:
            raise SchemaError("client_namespace is not configured")
        job_board_code = cfg.get("job_board_code", "CANDIDATEPORTAL")
        culture_code = cfg.get("culture_code", "en-US")

        csrf_response = await self.request("GET", f"{_BASE_URL}/api/auth/csrf")
        token = csrf_response.json().get("csrfToken")
        if not token:
            raise SchemaError("Dayforce /api/auth/csrf response missing csrfToken")
        headers = {"x-csrf-token": token, "Accept": "application/json"}

        jobs: list[JobSummary] = []
        offset = 0
        for _ in range(int(cfg.get("max_pages", 20))):
            body = {
                "clientNamespace": namespace,
                "jobBoardCode": job_board_code,
                "cultureCode": culture_code,
                "distanceUnit": 0,
                "paginationStart": offset,
            }
            response = await self.request(
                "POST",
                f"{_BASE_URL}/api/geo/{namespace}/jobposting/search",
                json=body,
                headers=headers,
            )
            payload = response.json()
            items = payload.get("jobPostings")
            if not isinstance(items, list):
                raise SchemaError("Dayforce jobposting/search response missing jobPostings list")
            page_jobs = [
                self._parse_job(item, namespace, job_board_code, culture_code) for item in items
            ]
            jobs.extend(page_jobs)
            count = int(payload.get("count", 0) or 0)
            max_count = int(payload.get("maxCount", 0) or 0)
            offset += count
            if offset >= max_count or count == 0:
                break
            if (
                self.max_posting_age_days is not None
                and page_jobs
                and not is_recent(page_jobs[-1].posted_at, self.max_posting_age_days)
            ):
                break
        return jobs

    def _parse_job(
        self, item: dict, namespace: str, job_board_code: str, culture_code: str
    ) -> JobSummary:
        job_id = str(item.get("jobPostingId") or "").strip()
        title = str(item.get("jobTitle") or "").strip()
        if not job_id or not title:
            raise SchemaError("Dayforce job posting missing jobPostingId/jobTitle")
        # A posting can list multiple postingLocations — locationType 1 is the actual
        # worksite address, locationType 2 is a nearby metro-area label used only for
        # search display (confirmed live: e.g. "JTEKT North America, ... Plymouth,
        # Michigan" type 1 alongside a "Detroit, MI, USA" type 2 for the same job).
        # Prefer type 1, same "take the primary one" convention csod.py uses for its
        # own multi-location shape.
        locations = item.get("postingLocations") or []
        primary = next((loc for loc in locations if loc.get("locationType") == 1), None)
        primary = primary or (locations[0] if locations else {})
        city = stringify(primary.get("cityName"))
        state = stringify(primary.get("stateCode"))
        country = stringify(primary.get("isoCountryCode"))
        location_raw = (
            stringify(primary.get("formattedAddress"))
            or ", ".join(part for part in (city, state, country) if part)
            or None
        )
        description = item.get("jobDescription")
        return JobSummary(
            source_key=self.source_key,
            source_platform=self.company.platform or "dayforce",
            company=self.company.company,
            job_id=job_id,
            title=title,
            url=f"{_BASE_URL}/{culture_code}/{namespace}/{job_board_code}/jobs/{job_id}",
            location_raw=location_raw,
            city=city,
            state=state,
            country=country,
            posted_at=parse_flexible_date(item.get("postingStartTimestampUTC")),
            raw={
                "description": normalize_text(html_module.unescape(description))
                if description
                else None
            },
        )

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail(description=summary.raw.get("description"))
