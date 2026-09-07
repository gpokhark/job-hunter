from __future__ import annotations

import json
import re

from ..models import JobDetail, JobSummary
from ..normalizer import parse_flexible_date, stringify
from .base import JobAdapter, SchemaError

_SESSION_JWT_RE = re.compile(r'"sessionJWT":"([^"]+)"')


class PaycomAdapter(JobAdapter):
    """Paycom's "career-page" ATS widget (paycomonline.net/v4/ats/web.php/portal/<id>/...)
    is a heavy client-rendered SPA (`sprawl.min.js`) with no job data anywhere in its own
    plain HTML — but every page load (career-page or a job detail page alike) embeds a
    short-lived (~2 hour, confirmed via its own `exp`/`iat` JWT claims) anonymous bearer
    token in a `var configsFromHost = {"sessionJWT": "..."}` block — the same "public
    frontend key minted by an unauthenticated page load" shape as csod.py's
    `csod.context.token` and bosch.py's static Bearer key, not a session/CSRF replay.
    Rendering the career-page once with Playwright (capturing XHR) found it replayed as
    an `Authorization: Bearer` header on `POST .../api/ats/job-posting-previews/search`
    — confirmed genuinely anonymous and requiring no browser at request time by re-minting
    the token from a second, independent plain httpx GET and replaying it with the same
    result. That listing endpoint's own `description` field is a truncated preview, not
    the full posting (confirmed directly against a real job: cut off mid-sentence with
    "..."), so fetch_detail fetches the equivalent per-job page
    (`GET .../api/ats/job-postings/{id}`, requiring its own freshly-minted token the same
    way — this adapter has no cross-call state, so it re-mints once per detail fetch
    rather than caching the summary-fetch's token, a fine tradeoff at the catalog sizes
    seen so far but a real one to revisit if a much larger Paycom tenant is onboarded
    later) and reads its `description`/`qualifications` HTML plus the embedded
    `googleJobJson` field — a JSON-*string* (needs its own `json.loads`) holding a
    standard schema.org JobPosting, whose `datePosted` is used since the listing-level
    `postedOn` field was confirmed always empty on this tenant."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        career_page_url = cfg.get("career_page_url")
        search_url = cfg.get("search_url")
        public_base_url = cfg.get("public_base_url")
        if not career_page_url or not search_url or not public_base_url:
            raise SchemaError("career_page_url/search_url/public_base_url is not configured")
        token = await self._fetch_token(career_page_url)
        headers = {"Authorization": f"Bearer {token}"}
        page_size = int(cfg.get("page_size", 200))
        jobs: list[JobSummary] = []
        skip = 0
        total: int | None = None
        for _ in range(int(cfg.get("max_pages", 20))):
            payload = {
                "skip": skip,
                "take": page_size,
                "filtersForQuery": {
                    "distanceFrom": 0,
                    "workEnvironments": [],
                    "positionTypes": [],
                    "educationLevels": [],
                    "categories": [],
                    "travelTypes": [],
                    "shiftTypes": [],
                    "otherFilters": [],
                    "keywordSearchText": "",
                    "location": "",
                    "sortOption": "",
                },
            }
            response = await self.request("POST", search_url, json=payload, headers=headers)
            data = response.json()
            items = data.get("jobPostingPreviews")
            if not isinstance(items, list):
                raise SchemaError("Paycom search response missing jobPostingPreviews list")
            if total is None:
                total = int(data.get("jobPostingPreviewsCount", 0) or 0)
            if not items:
                break
            for item in items:
                job_id = stringify(item.get("jobId"))
                title = stringify(item.get("jobTitle"))
                if not job_id or not title:
                    raise SchemaError("Paycom job posting preview missing jobId/jobTitle")
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform=self.company.platform or "paycom",
                        company=self.company.company,
                        job_id=job_id,
                        title=title,
                        url=f"{public_base_url.rstrip('/')}/jobs/{job_id}",
                        location_raw=stringify(item.get("locations")),
                        raw=item,
                    )
                )
            skip += len(items)
            if skip >= total:
                break
        return jobs

    async def _fetch_token(self, page_url: str) -> str:
        response = await self.request("GET", page_url)
        match = _SESSION_JWT_RE.search(response.text)
        if not match:
            raise SchemaError("Paycom page did not embed a sessionJWT")
        return match.group(1)

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        cfg = self.company.config
        detail_url_template = cfg.get("detail_api_url")
        if not detail_url_template:
            raise SchemaError("detail_api_url is not configured")
        token = await self._fetch_token(summary.url)
        headers = {"Authorization": f"Bearer {token}"}
        url = detail_url_template.format(id=summary.job_id)
        response = await self.request("GET", url, headers=headers)
        posting = response.json().get("jobPosting") or {}
        parts = [text for text in (posting.get("description"), posting.get("qualifications")) if text]
        posted_at = None
        google_job_json = posting.get("googleJobJson")
        if google_job_json:
            try:
                posted_at = parse_flexible_date(json.loads(google_job_json).get("datePosted"))
            except (json.JSONDecodeError, AttributeError):
                posted_at = None
        return JobDetail(description="\n\n".join(parts) or None, posted_at=posted_at)
