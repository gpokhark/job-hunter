from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from ..models import JobDetail, JobSummary
from ..normalizer import normalize_text
from ..prefilter import is_recent
from .base import JobAdapter, SchemaError
from .json_api import _date, _stringify

_HYDRATION_DATA = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);', re.S
)


def _extract_hydration_data(text: str) -> dict:
    """jobs.apple.com's React Router SPA server-renders every page (search results and
    job detail alike) with a full JSON snapshot of that page's data embedded as
    `window.__staticRouterHydrationData = JSON.parse("...")`. The argument is a
    double-encoded JSON string — the outer JS string-literal escaping (`\\"`, etc.) has
    to be undone with one json.loads before the inner text is itself parseable JSON.
    No browser is needed at all: a plain GET already returns this, including exact
    posting dates and full descriptions — found by inspecting the plain HTML response
    directly rather than assuming the site needed a stealth browser."""
    match = _HYDRATION_DATA.search(text)
    if not match:
        raise SchemaError("Apple careers page missing __staticRouterHydrationData")
    try:
        unescaped = json.loads('"' + match.group(1) + '"')
        return json.loads(unescaped)
    except json.JSONDecodeError as exc:
        raise SchemaError("Apple hydration data is not valid JSON") from exc


def _location_fields(location: dict) -> tuple[str | None, str | None, str | None]:
    city = _stringify(location.get("city") or location.get("name"))
    state = _stringify(location.get("stateProvince"))
    country = _stringify(location.get("countryName"))
    return city, state, country


class AppleAdapter(JobAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        base_url = cfg.get("list_url")
        if not base_url:
            raise SchemaError("list_url is not configured")
        # httpx's params= replaces a URL's existing query string rather than merging
        # with it, so a bare "page" param would silently drop list_url's own
        # "location=united-states-USA" filter — split it out and merge explicitly.
        parsed = urlsplit(base_url)
        base_params = dict(parse_qsl(parsed.query))
        request_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        page_size = int(cfg.get("page_size", 20))
        max_pages = int(cfg.get("max_pages", 250))

        async def _fetch_page(page: int) -> dict:
            response = await self.request(
                "GET", request_url, params={**base_params, "page": str(page)}
            )
            data = _extract_hydration_data(response.text)
            search = data.get("loaderData", {}).get("search")
            if not search or not isinstance(search.get("searchResults"), list):
                raise SchemaError("Apple search page missing loaderData.search.searchResults")
            return search

        first = await _fetch_page(1)
        all_results: list[dict] = list(first["searchResults"])
        total = int(first.get("totalRecords", 0))
        # ~4,500 US postings at 20/page is >200 sequential requests if fetched one at a
        # time; every page beyond the first is independent, so fan them out concurrently
        # once the first page has told us how many there are.
        total_pages = min(max_pages, -(-total // page_size)) if total else 1
        if total_pages > 1:
            concurrency = int(cfg.get("max_concurrent_pages", 10))
            semaphore = asyncio.Semaphore(concurrency)

            async def _bounded_page(page: int) -> dict:
                async with semaphore:
                    return await _fetch_page(page)

            # This listing is confirmed sorted newest-first (verified across the full
            # catalog, not assumed). Fetch in concurrent batches rather than one big
            # fan-out so pagination can actually stop once a batch's oldest item is
            # already past the recency cutoff, instead of always walking all ~230 pages
            # for a catalog where most postings are well outside the 30-day window.
            page = 2
            while page <= total_pages:
                batch_end = min(page + concurrency - 1, total_pages)
                batch = await asyncio.gather(*(_bounded_page(p) for p in range(page, batch_end + 1)))
                stop = False
                for search in batch:
                    results = search["searchResults"]
                    all_results.extend(results)
                    last_posted = _date(results[-1].get("postDateInGMT")) if results else None
                    if self.max_posting_age_days is not None and not is_recent(
                        last_posted, self.max_posting_age_days
                    ):
                        stop = True
                if stop:
                    break
                page = batch_end + 1

        jobs: list[JobSummary] = []
        seen_ids: set[str] = set()
        for item in all_results:
            req_id = str(item.get("reqId") or "").strip()
            title = str(item.get("postingTitle") or "").strip()
            if not req_id or not title:
                raise SchemaError("Apple job entry missing reqId/postingTitle")
            if req_id in seen_ids:
                continue
            seen_ids.add(req_id)
            locations = item.get("locations") or []
            city, state, country = _location_fields(locations[0] if locations else {})
            location_raw = ", ".join(part for part in (city, state, country) if part) or None
            slug = item.get("transformedPostingTitle") or ""
            jobs.append(
                JobSummary(
                    source_key=self.source_key,
                    source_platform=self.company.platform or "apple",
                    company=self.company.company,
                    job_id=req_id,
                    title=title,
                    url=urljoin(base_url, f"/en-us/details/{req_id}/{slug}"),
                    location_raw=location_raw,
                    city=city,
                    state=state,
                    country=country,
                    posted_at=_date(item.get("postDateInGMT")),
                    raw={"jobSummary": item.get("jobSummary")},
                )
            )
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        data = _extract_hydration_data(response.text)
        job = (data.get("loaderData", {}).get("jobDetails") or {}).get("jobsData")
        if not job:
            return JobDetail(description=_stringify(summary.raw.get("jobSummary")))
        description = job.get("description") or summary.raw.get("jobSummary")
        locations = job.get("locations") or []
        city, state, country = _location_fields(locations[0] if locations else {})
        location_raw = ", ".join(part for part in (city, state, country) if part) or None
        return JobDetail(
            description=normalize_text(description) if description else None,
            location_raw=location_raw,
            city=city,
            state=state,
            country=country,
            posted_at=_date(job.get("postDateInGMT")),
        )
