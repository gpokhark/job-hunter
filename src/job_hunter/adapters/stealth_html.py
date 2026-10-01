from __future__ import annotations

from typing import Any

from .base import AdapterError, ListingTimeout
from .html_paginated import HtmlPaginatedAdapter, _page_url


class _StealthResponse:
    """Minimal stand-in for an httpx.Response, exposing only what
    HtmlPaginatedAdapter reads from a response (.text)."""

    def __init__(self, text: str):
        self.text = text


class StealthHtmlAdapter(HtmlPaginatedAdapter):
    """Same card/pagination parsing as HtmlPaginatedAdapter, but fetches through a
    stealth headless browser session (Scrapling's AsyncStealthySession) instead of
    plain httpx, for sites behind a Cloudflare/Akamai-style bot-management challenge.

    Only use this for a source that has no other viable path (mark it `unsupported`
    instead if a real anonymous endpoint exists) — this crosses into deliberately
    defeating a site's own anti-automation controls, with the ToS and resource-cost
    implications that carries. Requires the optional `stealth` dependency group
    (`uv sync --extra stealth` followed by `uv run scrapling install`).

    A blocked or challenged page must never look like the end of the listing: `request()`
    raises on any fetch failure, and `strict_pagination` makes an empty continuation page (or
    running out of `max_pages`) raise too. Either way the collector keeps the pages already
    fetched, records a `warning`, and skips `mark_missing`, so a partial run closes no job.
    """

    strict_pagination = True

    def __init__(self, company, client, collection, max_posting_age_days=None):
        super().__init__(company, client, collection, max_posting_age_days)
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            try:
                from scrapling.fetchers import AsyncStealthySession
            except ImportError as exc:
                raise AdapterError(
                    "the 'stealth' dependency group is not installed "
                    "(uv sync --extra stealth && uv run scrapling install)"
                ) from exc
            self._session = AsyncStealthySession(headless=True)
            await self._session.__aenter__()
        return self._session

    async def fetch_summaries(self, start_url: str | None = None) -> list:
        """Override to skip detail-page fetching when configured. Waymo's WAF
        blocks almost every detail page, so we extract everything from listing cards."""
        if self.company.config.get("skip_detail_fetch"):
            return await self._fetch_summaries_no_details(start_url)
        return await super().fetch_summaries(start_url)

    async def _fetch_summaries_no_details(self, start_url: str | None = None):
        """Fetch listing pages and extract all fields from card HTML — no detail page navigation."""
        # Lazy imports — selectolax is only in the stealth extra
        from urllib.parse import urljoin

        from selectolax.parser import HTMLParser

        from ..models import JobSummary
        from ..normalizer import fallback_job_id, normalize_text
        from .base import SchemaError

        cfg = self.company.config
        start_url = start_url or cfg.get("list_url")
        if not start_url:
            raise SchemaError("list_url is not configured")

        url = start_url
        jobs = self.begin_listing()
        max_results = self.collection.max_results if hasattr(self.collection, 'max_results') else 1000
        max_pages = int(cfg.get("max_pages", 20))
        page_size = int(cfg.get("page_size", 25))
        seen_urls: set[str] = set()

        for page in range(max_pages):
            if not url or len(jobs) >= max_results:
                break
            if url in seen_urls:
                raise AdapterError(f"pagination loop: {url} was already fetched")
            seen_urls.add(url)
            await self._pace()
            response = await self.request("GET", url)

            tree = HTMLParser(response.text)
            cards = tree.css(cfg.get("card_selector", "[data-job-id]"))

            if not cards:
                if not jobs:
                    raise SchemaError("no job cards matched configured selector")
                # The loop asked for this page because the previous one promised more, so a
                # blank/challenge page is a blocked fetch, not the end of the listing.
                raise AdapterError(
                    f"page returned no job cards after {len(jobs)} job(s) were already "
                    f"collected; not treating it as the end of the listing: {url}"
                )

            for card in cards:
                if len(jobs) >= max_results:
                    break

                # Title from link text
                title_node = card.css_first(cfg.get("title_selector", "h3.card-title a"))
                if not title_node:
                    title_node = card.css_first(cfg.get("link_selector", "a"))
                title = title_node.text() if title_node else "N/A"
                title = normalize_text(title)

                # Link from href
                link_node = card.css_first(cfg.get("link_selector", "a"))
                href = link_node.attributes.get("href") if link_node else None
                detail_url = urljoin(url, href) if href else url

                # Location
                location = ""
                location_node = card.css_first(cfg.get("location_selector", "[data-testid='job-location']"))
                if location_node:
                    location = normalize_text(location_node.text())

                # Department
                department = ""
                dept_node = card.css_first(cfg.get("department_selector"))
                if dept_node:
                    department = normalize_text(dept_node.text())

                # Employment type
                emp_type = ""
                emp_node = card.css_first(cfg.get("employment_type_selector"))
                if emp_node:
                    emp_type = normalize_text(emp_node.text())

                # Summary/description
                description = ""
                desc_node = card.css_first(cfg.get("description_selector"))
                if desc_node:
                    description = normalize_text(desc_node.text())

                # Job ID from URL
                job_id = fallback_job_id(self.company.company, title, location, detail_url)

                # Location string for display
                location_parts = [p for p in [location, department, emp_type] if p]
                location_raw = ", ".join(location_parts) if location_parts else location

                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform=self.company.platform or "html",
                        company=self.company.company,
                        job_id=job_id,
                        title=title,
                        url=detail_url,
                        location_raw=location_raw,
                        department=department if department else None,
                        employment_type=emp_type if emp_type else None,
                        raw={"description": description},
                    )
                )

            # Pagination
            next_node = tree.css_first(cfg.get("next_selector", "a.next"))
            next_href = next_node.attributes.get("href") if next_node else None
            if next_href:
                url = urljoin(url, next_href)
            elif cfg.get("page_parameter") and len(cards) >= page_size:
                # Row-offset style, same as HtmlPaginatedAdapter.
                url = _page_url(start_url, cfg["page_parameter"], (page + 1) * page_size)
            elif cfg.get("page_number_parameter") and len(cards) >= page_size:
                # 1-indexed page number, same as HtmlPaginatedAdapter.
                url = _page_url(start_url, cfg["page_number_parameter"], page + 2)
            else:
                url = None

        if url:
            raise AdapterError(
                f"stopped after {len(jobs)} job(s) at max_pages={max_pages}/"
                f"max_results={max_results} with more pages available; raise the limit "
                "rather than truncate the listing"
            )

        if not jobs:
            raise SchemaError("no jobs fetched")

        return jobs

    async def fetch_detail(self, summary):
        """Skip detail-page fetching — all info is already in the listing card."""
        if self.company.config.get("skip_detail_fetch"):
            from ..models import JobDetail
            description = ""
            if hasattr(summary, 'raw') and summary.raw:
                description = summary.raw.get("description", "")
            return JobDetail(
                description=description or "",
                location_raw=summary.location_raw,
                department=summary.department,
                employment_type=summary.employment_type,
            )
        return await super().fetch_detail(summary)

    async def request(self, method: str, url: str, **kwargs: Any) -> _StealthResponse:
        session = await self._ensure_session()
        cfg = self.company.config
        try:
            response = await session.fetch(
                url,
                network_idle=True,
                timeout=self.collection.timeout_seconds * 1000,
                wait_selector=cfg.get("wait_selector"),
            )
        except Exception as exc:
            # Scrapling raises different types for a timeout vs. a WAF challenge vs. a
            # browser-launch failure, so this is broad by necessity — but every one of them
            # means "this page was NOT fetched". Returning an empty page here used to make a
            # blocked page 2 look like the end of the listing (reported `ok`, then
            # `mark_missing` closed everything unreached). Raise instead: the collector keeps
            # the pages already fetched and records a warning naming the cause.
            reason = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, TimeoutError) or type(exc).__name__.endswith("TimeoutError"):
                raise ListingTimeout(f"stealth fetch of {url} timed out ({reason})") from exc
            raise AdapterError(f"stealth fetch of {url} failed ({reason})") from exc
        return _StealthResponse(response.html_content)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
