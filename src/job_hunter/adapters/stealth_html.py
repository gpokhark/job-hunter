from __future__ import annotations

from typing import Any

from .base import AdapterError
from .html_paginated import HtmlPaginatedAdapter


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
            raise AdapterError(f"stealth fetch failed for {url}: {exc}") from exc
        return _StealthResponse(response.html_content)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
