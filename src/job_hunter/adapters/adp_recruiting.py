from __future__ import annotations

from ..models import JobDetail, JobSummary
from ..normalizer import normalize_text
from ..prefilter import is_recent
from .base import JobAdapter, SchemaError
from .json_api import _date, _stringify

_LIST_URL = (
    "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/"
    "apply-custom-filters"
)
_SELECT = ",".join(
    [
        "reqId",
        "jobTitle",
        "publishedJobTitle",
        "jobDescription",
        "jobQualifications",
        "postingDate",
        "requisitionLocations",
    ]
)


class AdpRecruitingAdapter(JobAdapter):
    """ADP Recruiting Management (RM) career-site backend — used by Stellantis
    (`stellantisexternalcx`) and potentially other ADP RM customers. Its Angular SPA
    front end (myjobs.adp.com/{domain}/cx/...) never renders any content server-side,
    but the public, unauthenticated `/public/staffing/v1/career-site/{domain}` endpoint
    returns a `myJobsToken` value that the same front end replays as a `myjobstoken`
    request header on every subsequent API call — a two-step public handshake, not a
    login/CSRF replay, discovered by rendering the page once and reading its own
    generated XHR calls. The listing endpoint
    (`.../job-requisitions/apply-custom-filters`) returns full job descriptions,
    qualifications, and a posting date in one shot, so no separate per-job detail fetch
    is needed."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        domain = cfg.get("career_site_domain")
        if not domain:
            raise SchemaError("career_site_domain is not configured")
        token_response = await self.request(
            "GET", f"https://myjobs.adp.com/public/staffing/v1/career-site/{domain}"
        )
        token = token_response.json().get("myJobsToken")
        if not token:
            raise SchemaError("ADP career-site response missing myJobsToken")
        headers = {"myjobstoken": token, "accept": "application/json"}
        page_size = int(cfg.get("page_size", 100))
        jobs: list[JobSummary] = []
        skip = 0
        for _ in range(int(cfg.get("max_pages", 30))):
            params = {
                "$select": _SELECT,
                "$top": str(page_size),
                "$skip": str(skip),
                "$filter": "",
                "tz": "America/Detroit",
            }
            response = await self.request("GET", _LIST_URL, params=params, headers=headers)
            payload = response.json()
            items = payload.get("jobRequisitions")
            if not isinstance(items, list):
                raise SchemaError("ADP job-requisitions response missing jobRequisitions list")
            page_jobs = [self._parse_job(item, domain) for item in items]
            jobs.extend(page_jobs)
            skip += page_size
            if skip >= int(payload.get("count", 0)) or not items:
                break
            # This listing is confirmed sorted newest-first (verified across the full
            # catalog, not assumed) — once a page's oldest item is already past the
            # recency cutoff, every later page is guaranteed older still, so stop here
            # instead of walking the rest of a ~1,000-job catalog for postings that
            # would just be filtered out as stale anyway.
            if self.max_posting_age_days is not None and not is_recent(
                page_jobs[-1].posted_at, self.max_posting_age_days
            ):
                break
        return jobs

    def _parse_job(self, item: dict, domain: str) -> JobSummary:
        req_id = str(item.get("reqId") or "").strip()
        title = str(item.get("publishedJobTitle") or item.get("jobTitle") or "").strip()
        if not req_id or not title:
            raise SchemaError("ADP job requisition missing reqId/title")
        locations = item.get("requisitionLocations") or []
        address = (locations[0].get("address") or {}) if locations else {}
        city = _stringify(address.get("cityName"))
        state = _stringify((address.get("countrySubdivisionLevel1") or {}).get("longName"))
        country = _stringify((address.get("country") or {}).get("longName"))
        location_raw = ", ".join(part for part in (city, state, country) if part) or None
        description = "\n\n".join(
            normalize_text(part)
            for part in (item.get("jobDescription"), item.get("jobQualifications"))
            if part
        ) or None
        return JobSummary(
            source_key=self.source_key,
            source_platform=self.company.platform or "adp_recruiting",
            company=self.company.company,
            job_id=req_id,
            title=title,
            url=f"https://myjobs.adp.com/{domain}/cx/job-details?reqId={req_id}",
            location_raw=location_raw,
            city=city,
            state=state,
            country=country,
            posted_at=_date(item.get("postingDate")),
            raw={"description": description},
        )

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail(description=summary.raw.get("description"))
