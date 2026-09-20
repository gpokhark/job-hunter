from __future__ import annotations

from typing import Any

from .base import AdapterError
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
    """

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
        jobs = []
        max_results = self.collection.max_results if hasattr(self.collection, 'max_results') else 1000

        while url and len(jobs) < max_results:
            await self._pace()
            response = await self.request("GET", url)

            if response is None:
                break

            tree = HTMLParser(response.text)
            cards = tree.css(cfg.get("card_selector", "[data-job-id]"))

            if not cards and not jobs:
                raise SchemaError("no job cards matched configured selector")

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
            elif cfg.get("page_parameter") and len(cards) >= int(cfg.get("page_size", 25)):
                url = _page_url(
                    url,
                    cfg.get("page_parameter", "page"),
                    cfg.get("page_size", 25),
                    len(jobs) + 1,
                )
            else:
                break

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
            # On timeout/WAF challenge, return empty HTML so the parent
            # adapter can gracefully stop pagination rather than failing
            # the entire run.  The parent's fetch_summaries checks
            # "if not cards and not jobs" — if we already have jobs
            # from earlier pages, an empty page just returns what we have.
            return _StealthResponse("")
        return _StealthResponse(response.html_content)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
