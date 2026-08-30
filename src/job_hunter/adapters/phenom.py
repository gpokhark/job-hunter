from __future__ import annotations

import html
import json
import re
from urllib.parse import urljoin

from ..models import JobDetail, JobSummary
from ..normalizer import extract_job_posting_ld, normalize_text
from .base import JobAdapter, SchemaError
from .json_api import _date, _stringify


def _extract_embedded_json(text: str, key: str) -> dict:
    """Phenom career sites eager-load their first page of search results as a plain
    (non-escaped) JSON object embedded in the page for SEO, keyed by e.g.
    "eagerLoadRefineSearch". There is no closing-brace-count in the surrounding markup,
    so the object is located by brace-matching from its opening '{'."""
    marker = f'"{key}"'
    key_index = text.find(marker)
    if key_index == -1:
        raise SchemaError(f"embedded data key {key!r} not found in Phenom search page")
    brace_start = text.find("{", key_index)
    if brace_start == -1:
        raise SchemaError(f"malformed embedded data for key {key!r}")
    depth = 0
    for index in range(brace_start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace_start : index + 1])
                except json.JSONDecodeError as exc:
                    raise SchemaError(f"embedded data for key {key!r} is not valid JSON") from exc
    raise SchemaError(f"unterminated embedded data for key {key!r}")


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "role"


class PhenomAdapter(JobAdapter):
    """Phenom People career sites render their search-results page mostly client-side,
    but eager-load the current page of matches into an embedded JSON blob for SEO
    (config: embedded_data_key, default "eagerLoadRefineSearch"). Pagination is via the
    `from` (row offset) and `s` (constant "1") query params — found by rendering the page
    through a real browser to read its own generated pagination links, since a plain
    fetch's static shell never includes them and the more commonly-guessed `start`/`num`
    params are silently ignored. Once known, `from`/`s` work over plain httpx; no browser
    is needed at request time."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        base_url = cfg.get("list_url")
        if not base_url:
            raise SchemaError("list_url is not configured")
        embedded_key = cfg.get("embedded_data_key", "eagerLoadRefineSearch")
        page_size = int(cfg.get("page_size", 10))
        jobs: list[JobSummary] = []
        seen_ids: set[str] = set()
        offset = 0
        for _ in range(int(cfg.get("max_pages", 30))):
            params = {"keywords": cfg.get("keywords", "")}
            if offset:
                params.update(**{"from": offset, "s": "1"})
            response = await self.request("GET", base_url, params=params)
            blob = _extract_embedded_json(response.text, embedded_key)
            items = (blob.get("data") or {}).get("jobs")
            if not isinstance(items, list):
                raise SchemaError("Phenom embedded search data missing jobs list")
            new_ids = {_stringify(item.get("jobId")) or _stringify(item.get("reqId")) for item in items}
            if not items or not (new_ids - seen_ids):
                break
            for item in items:
                job_id = _stringify(item.get("jobId")) or _stringify(item.get("reqId"))
                title = _stringify(item.get("title"))
                if not job_id or not title:
                    raise SchemaError("Phenom job entry missing jobId/title")
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                url = urljoin(base_url, f"job/{job_id}/{_slugify(title)}")
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform=self.company.platform or "phenom",
                        company=self.company.company,
                        job_id=job_id,
                        title=title,
                        url=url,
                        location_raw=_stringify(
                            item.get("locationName") or item.get("cityStateCountry")
                        ),
                        city=_stringify(item.get("city")),
                        state=_stringify(item.get("state")),
                        country=_stringify(item.get("country")),
                        department=_stringify(item.get("department")),
                        employment_type=_stringify(item.get("type")),
                        posted_at=_date(item.get("postedDate")),
                        raw=item,
                    )
                )
            total_hits = int(blob.get("totalHits", 0))
            offset += page_size
            if offset >= total_hits:
                break
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        posting = extract_job_posting_ld(response.text)
        if not posting:
            return JobDetail()
        description = posting.get("description")
        # Honda's JSON-LD datePosted has been observed drifting forward over time
        # (verified: read back as "today" for a job whose listing-level postedDate was
        # ~2 weeks earlier) — it looks like a "last shown as active" timestamp, not a
        # stable original-post date. fetch_summaries's postedDate (listing JSON) is the
        # more trustworthy of the two; this only overrides it when a detail fetch
        # actually happens (see collector.py's should_detail).
        return JobDetail(
            description=normalize_text(html.unescape(description)) if description else None,
            posted_at=_date(posting.get("datePosted")),
            employment_type=_stringify(posting.get("employmentType")),
        )
