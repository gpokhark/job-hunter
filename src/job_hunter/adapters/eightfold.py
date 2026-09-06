from __future__ import annotations

from ..models import JobDetail, JobSummary
from ..normalizer import parse_flexible_date, stringify
from .base import SchemaError
from .json_api import ConfigurableJsonAdapter


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

    Forvia Faurecia (jobs.faurecia.com) is the same Eightfold platform but a different,
    older API generation at `/api/apply/v2/jobs` — Deere's `/api/pcsx/search` path
    returned a same-shaped 403 ("PCSX is not enabled for this user") here, the same
    "guessed a plausible path, got a same-shaped-but-wrong error" pattern Deere's own
    docstring above describes for the *other* direction. Faurecia's response has no
    `data` wrapper (fields sit at the top level), snake_case names
    (`job_description`/`canonicalPositionUrl`/`t_create` instead of
    `jobDescription`/`positionUrl`/`postedTs`), an already-absolute detail URL, and puts
    the job id in the detail URL's path rather than a `position_id` query param — all
    handled below by shape-detecting `data` (present vs. absent), an opt-in
    `posted_field` config override, a `positionUrl`/`canonicalPositionUrl` fallback that
    only prefixes `public_base_url` when the value isn't already absolute, and an opt-in
    `detail_id_in_path` flag — Deere's existing config is unaffected by any of these
    since they all fall back to Deere's original shape by default.
    """

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        list_url = cfg.get("list_url")
        if not list_url:
            raise SchemaError("list_url is not configured")
        base_url = cfg.get("public_base_url", list_url)
        params = dict(cfg.get("params", {}))
        posted_field = cfg.get("posted_field", "postedTs")
        jobs: list[JobSummary] = []
        start = 0
        total: int | None = None
        for _ in range(int(cfg.get("max_pages", 50))):
            response = await self.request("GET", list_url, params={**params, "start": start})
            payload = response.json()
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            items = data.get("positions")
            if not isinstance(items, list):
                raise SchemaError("Eightfold pcsx response lacks positions list")
            if total is None:
                total = int(data.get("count", 0))
            if not items:
                break
            for item in items:
                title = stringify(item.get("name"))
                native_id = stringify(item.get("id"))
                if not title or not native_id:
                    raise SchemaError("Eightfold position lacks name/id")
                raw_url = str(item.get("positionUrl") or item.get("canonicalPositionUrl") or "")
                url = (
                    raw_url
                    if raw_url.startswith("http")
                    else f"{base_url.rstrip('/')}/{raw_url.lstrip('/')}"
                )
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform="eightfold",
                        company=self.company.company,
                        job_id=native_id,
                        title=title,
                        url=url,
                        location_raw=stringify(item.get("standardizedLocations") or item.get("locations")),
                        department=stringify(item.get("department")),
                        posted_at=parse_flexible_date(item.get(posted_field)),
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
        if cfg.get("detail_id_in_path"):
            url = f"{detail_url.rstrip('/')}/{summary.job_id}"
            params = dict(cfg.get("detail_params", {}))
        else:
            url = detail_url
            params = {**cfg.get("detail_params", {}), "position_id": summary.job_id}
        response = await self.request("GET", url, params=params)
        payload = response.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return JobDetail(
            description=stringify(data.get("jobDescription") or data.get("job_description")),
            location_raw=stringify(data.get("standardizedLocations") or data.get("locations")),
        )
