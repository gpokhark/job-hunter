from __future__ import annotations

from ..models import JobDetail, JobSummary
from ..normalizer import fallback_job_id, parse_relative_posted
from .base import SchemaError
from .json_api import ConfigurableJsonAdapter, _date, _stringify


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
            for item in items:
                title = _stringify(item.get("title"))
                path = _stringify(item.get("externalPath"))
                if not title or not path:
                    raise SchemaError("Workday posting lacks title/externalPath")
                detail_url = f"{cfg.get('public_base_url', url).rstrip('/')}/{path.lstrip('/')}"
                location = _stringify(item.get("locationsText") or item.get("bulletFields"))
                native_id = _stringify(item.get("jobId"))
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform="workday",
                        company=self.company.company,
                        job_id=native_id
                        or fallback_job_id(self.company.company, title, location, detail_url),
                        title=title,
                        url=detail_url,
                        location_raw=location,
                        posted_at=parse_relative_posted(_stringify(item.get("postedOn"))),
                        raw=item,
                    )
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
        response = await self.request("GET", summary.url)
        data = response.json()
        info = data.get("jobPostingInfo", data)
        return JobDetail(
            description=_stringify(info.get("jobDescription")),
            location_raw=_stringify(info.get("location")),
            employment_type=_stringify(info.get("timeType")),
            # startDate is an absolute date, more precise than the summary's relative postedOn text.
            posted_at=_date(info.get("startDate")) or parse_relative_posted(_stringify(info.get("postedOn"))),
        )
