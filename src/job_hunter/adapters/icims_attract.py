from __future__ import annotations

from ..models import JobSummary
from .base import SchemaError, nested
from .json_api import ConfigurableJsonAdapter


class IcimsAttractAdapter(ConfigurableJsonAdapter):
    """iCIMS's "Attract" (Jibe-branded) career-site widget exposes its own job search
    as a plain, unauthenticated same-origin JSON endpoint on the careers.<company>.com
    host itself (e.g. careers.rivian.com/api/jobs) — completely separate from the
    <tenant>.icims.com ATS host a job's own apply link points at. Confirmed live
    (Rivian): plain GET, no cookies/session/CSRF token required, full plain-text
    description/qualifications/responsibilities and structured city/state/country/
    country_code already inline in the *listing* response — no per-job detail fetch
    needed, same shape as this repo's Ashby adapter.

    Pagination is ordinary page/limit query params (unlike Oracle HCM's offset=N
    embedded in the URL string), and limit is capped server-side at 100 (confirmed:
    101+ returns HTTP 422) — well below Rivian's full catalog (738), so a single
    request only returns the first page, which itself only spans a few days once
    sorted newest-first (confirmed: page 1 sorted by posted_date covers ~4 days for
    Rivian), so a full paginated crawl every run is required to reliably cover the
    default 30-day recency window, not just an early-stop optimization.
    `config: {paginate: true}` loops page=1.. until either an empty page or the
    running item count reaches total_path, reusing `_items_to_jobs()` per page exactly
    like `oracle_hcm.py` does."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        if not cfg.get("paginate"):
            return await super().fetch_summaries()
        url = cfg.get("list_url")
        if not url:
            raise SchemaError("list_url is not configured")
        items_path = cfg.get("items_path", "")
        total_path = cfg.get("total_path")
        if not total_path:
            raise SchemaError("paginate:true requires total_path")
        limit = int(cfg.get("limit", 100))
        base_params = dict(cfg.get("params") or {})
        jobs: list[JobSummary] = []
        seen = 0
        total = None
        for page in range(1, int(cfg.get("max_pages", 20)) + 1):
            params = {**base_params, "page": page, "limit": limit}
            response = await self.request("GET", url, params=params)
            payload = response.json()
            items = nested(payload, items_path, payload)
            if not isinstance(items, list):
                raise SchemaError(f"items_path did not resolve to a list: {items_path}")
            if total is None:
                total = int(nested(payload, total_path, 0) or 0)
            if not items:
                break
            jobs.extend(self._items_to_jobs(items, url))
            seen += len(items)
            if seen >= total:
                break
        return jobs
