import logging

import httpx
import pytest

from job_hunter.adapters.stealth_html import StealthHtmlAdapter
from job_hunter.config import CollectionConfig, CompanyConfig


class _RaisingSession:
    async def fetch(self, *args, **kwargs):
        raise TimeoutError("WAF challenge not cleared")


@pytest.mark.asyncio
async def test_request_returns_empty_response_and_logs_on_fetch_failure(monkeypatch, caplog):
    """The stealth browser session can raise for many reasons (timeout, WAF challenge, a
    browser-launch failure) — `request()` deliberately treats all of them as "empty page" so
    pagination can stop gracefully instead of failing the whole run, but the real exception
    must still be logged rather than silently discarded (see stealth_html.py's comment)."""
    company = CompanyConfig(key="waymo", company="Waymo", adapter="stealth_html", config={})

    async def _fake_ensure_session():
        return _RaisingSession()

    async with httpx.AsyncClient() as client:
        adapter = StealthHtmlAdapter(company, client, CollectionConfig(max_retries=0))
        monkeypatch.setattr(adapter, "_ensure_session", _fake_ensure_session)
        with caplog.at_level(logging.WARNING):
            response = await adapter.request("GET", "https://careers.withwaymo.com/jobs")

    assert response.text == ""
    assert any(
        "waymo" in record.message and "TimeoutError" in record.message for record in caplog.records
    )
