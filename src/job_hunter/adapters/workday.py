from __future__ import annotations

import logging

from ..location import detect_arrangement
from ..models import JobDetail, JobSummary
from ..normalizer import fallback_job_id, parse_flexible_date, parse_relative_posted, stringify
from .base import SchemaError
from .json_api import ConfigurableJsonAdapter

logger = logging.getLogger(__name__)


class WorkdayAdapter(ConfigurableJsonAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        if not cfg.get("workday_native"):
            return await super().fetch_summaries()
        url = cfg.get("list_url")
        if not url:
            raise SchemaError("list_url is not configured")
        limit = int(cfg.get("page_size", 20))
        offset = 0
        jobs: list[JobSummary] = []
        total: int | None = None
        while offset < int(cfg.get("max_jobs", 2000)):
            payload = {
                "appliedFacets": cfg.get("applied_facets", {}),
                "limit": limit,
                "offset": offset,
                "searchText": "",
            }
            response = await self.request("POST", url, json=payload)
            data = response.json()
            items = data.get("jobPostings")
            if not isinstance(items, list):
                raise SchemaError("Workday response lacks jobPostings list")
            skipped = 0
            for item in items:
                title = stringify(item.get("title"))
                path = stringify(item.get("externalPath"))
                if not title or not path:
                    # Confirmed live (Valeo 2026-09-08: 1 of 1016, NVIDIA: 32 of the 2000
                    # cap): tenants return individual listing entries with every content
                    # field null except bulletFields (a bare "JR2018101"/"REQ2026077100"
                    # identifier) — the tenant's own count of them is still included in
                    # "total", but there is nothing for a collector to act on and, for a
                    # few rows, nothing a person could even apply to. Skipping them keeps
                    # one malformed entry from failing the entire 1,000+-job listing; the
                    # whole-page check below still fails loudly if the platform genuinely
                    # changed its response shape.
                    skipped += 1
                    logger.warning(
                        "%s: skipping malformed Workday listing entry (no title/externalPath): %r",
                        self.source_key,
                        (item.get("bulletFields") or [None])[0],
                    )
                    continue
                # public_base_url must be Workday's native candidate-facing host
                # (".../en-US/{site}/"), never the CXS API host used by list_url — the CXS
                # host returns raw JSON when opened in a browser, not a page a human can
                # read. fetch_detail below reconstructs the real CXS API url independently.
                display_url = f"{cfg.get('public_base_url', url).rstrip('/')}/{path.lstrip('/')}"
                location = stringify(item.get("locationsText") or item.get("bulletFields"))
                native_id = stringify(item.get("jobId"))
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform="workday",
                        company=self.company.company,
                        job_id=native_id
                        or fallback_job_id(self.company.company, title, location, display_url),
                        title=title,
                        url=display_url,
                        location_raw=location,
                        # Workday's own "remoteType" facet (e.g. "Hybrid", "Onsite", "Remote",
                        # "Remote/Hybrid") is real structured signal independent of whatever
                        # location_raw happens to say — run it through the same text-based
                        # detect_arrangement() used everywhere else so "Remote/Hybrid" resolves
                        # to HYBRID via the same hybrid-beats-remote precedence, not a
                        # bespoke mapping here.
                        work_arrangement=detect_arrangement(stringify(item.get("remoteType"))),
                        posted_at=parse_relative_posted(stringify(item.get("postedOn"))),
                        raw=item,
                    )
                )
            if items and skipped == len(items):
                # An entire page of entries with nothing but a bare identifier is the
                # platform having structurally changed its response shape, not a few
                # individually-withdrawn postings — fail the source loudly (and it will
                # show up in source_health), never silently loop forever or return an
                # empty page as if the tenant had no jobs at all.
                raise SchemaError(
                    f"Workday listing page at offset {offset} contains only malformed "
                    f"(title/externalPath-less) postings"
                )
            offset += len(items)
            if total is None:
                # Some Workday tenants only report an accurate "total" on the first page and
                # report 0 on every later page; capture it once instead of re-reading it.
                total = int(data.get("total", offset))
            if not items or offset >= total:
                break
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        if not self.company.config.get("workday_native"):
            return await super().fetch_detail(summary)
        cfg = self.company.config
        path = stringify((summary.raw or {}).get("externalPath"))
        if not path:
            raise SchemaError("workday summary missing externalPath for detail fetch")
        list_url = cfg.get("list_url", "")
        cxs_base = list_url.rsplit("/jobs", 1)[0]
        api_url = f"{cxs_base}/{path.lstrip('/')}"
        response = await self.request("GET", api_url)
        data = response.json()
        info = data.get("jobPostingInfo", data)
        # None (not UNKNOWN) when remoteType is genuinely absent from this response — JobDetail's
        # work_arrangement=None is what tells evaluate_location to fall back to parsing
        # location_raw text instead, the same fallback it had before remoteType was read at all.
        remote_type = stringify(info.get("remoteType"))
        return JobDetail(
            description=stringify(info.get("jobDescription")),
            location_raw=stringify(info.get("location")),
            employment_type=stringify(info.get("timeType")),
            # startDate is an absolute date, more precise than the summary's relative postedOn text.
            posted_at=parse_flexible_date(info.get("startDate")) or parse_relative_posted(stringify(info.get("postedOn"))),
            work_arrangement=detect_arrangement(remote_type) if remote_type else None,
        )
