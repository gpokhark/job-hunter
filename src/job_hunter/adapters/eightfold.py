from __future__ import annotations

from ..models import JobDetail, JobSummary
from .base import SchemaError
from .json_api import ConfigurableJsonAdapter, _date, _stringify


class EightfoldAdapter(ConfigurableJsonAdapter):
    """Eightfold's public "pcsx" (public candidate search experience) API backs many
    customer career sites — confirmed here for John Deere (careers.deere.com), whose
    visible listing page (`/careers?query=...`) renders no job data server-side at all;
    the real endpoint was found only by rendering it once with Playwright and reading
    its actual XHR calls, not by guessing — an initial guess at a path pattern seen on
    other Eightfold deployments (`/api/apply/v2/jobs`) returned a same-shaped-but-wrong
    403 `{"message": "Not authorized for PCSX"}`; the "PCSX" in that error is what
    pointed at the real path, `/api/pcsx/search` (list) and `/api/pcsx/position_details`
    (detail), both genuinely public with no cookies/session/auth needed.

    Pagination is real (`start` advances through different jobs, confirmed at
    start=0/10/20) but the page size is hardcoded at 10 server-side — every plausible
    override tried (`num`, `limit`, `size`, `pageSize`, `per_page`) was silently
    ignored — so this loops in fixed strides of whatever the server actually returns
    until `data.count` (a stable, real total, confirmed unaffected by any of those
    params) is reached. The default sort is `distance`, not date — postedTs across a
    single page was confirmed non-monotonic, so no early-pagination-stop is attempted.
    """

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        list_url = cfg.get("list_url")
        if not list_url:
            raise SchemaError("list_url is not configured")
        base_url = cfg.get("public_base_url", list_url)
        params = dict(cfg.get("params", {}))
        jobs: list[JobSummary] = []
        start = 0
        total: int | None = None
        for _ in range(int(cfg.get("max_pages", 50))):
            response = await self.request("GET", list_url, params={**params, "start": start})
            payload = response.json()
            data = payload.get("data")
            if not isinstance(data, dict):
                raise SchemaError("Eightfold pcsx response missing data object")
            items = data.get("positions")
            if not isinstance(items, list):
                raise SchemaError("Eightfold pcsx response lacks positions list")
            if total is None:
                total = int(data.get("count", 0))
            if not items:
                break
            for item in items:
                title = _stringify(item.get("name"))
                native_id = _stringify(item.get("id"))
                if not title or not native_id:
                    raise SchemaError("Eightfold position lacks name/id")
                position_url = str(item.get("positionUrl") or "").lstrip("/")
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform="eightfold",
                        company=self.company.company,
                        job_id=native_id,
                        title=title,
                        url=f"{base_url.rstrip('/')}/{position_url}",
                        location_raw=_stringify(item.get("standardizedLocations") or item.get("locations")),
                        department=_stringify(item.get("department")),
                        posted_at=_date(item.get("postedTs")),
                        raw=item,
                    )
                )
            start += len(items)
            if start >= total:
                break
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        cfg = self.company.config
        detail_url = cfg.get("detail_url")
        if not detail_url:
            raise SchemaError("detail_url is not configured")
        params = {**cfg.get("detail_params", {}), "position_id": summary.job_id}
        response = await self.request("GET", detail_url, params=params)
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SchemaError("Eightfold pcsx detail response missing data object")
        return JobDetail(
            description=_stringify(data.get("jobDescription")),
            location_raw=_stringify(data.get("standardizedLocations") or data.get("locations")),
        )
