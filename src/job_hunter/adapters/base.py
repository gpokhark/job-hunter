from __future__ import annotations

import asyncio
import email.utils
import random
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx

from ..config import CollectionConfig, CompanyConfig
from ..models import HealthStatus, JobDetail, JobSummary, SourceHealth


class AdapterError(RuntimeError):
    pass


class SchemaError(AdapterError):
    pass


class JobAdapter(ABC):
    def __init__(
        self,
        company: CompanyConfig,
        client: httpx.AsyncClient,
        collection: CollectionConfig,
        max_posting_age_days: int | None = None,
    ):
        self.company = company
        self.client = client
        self.collection = collection
        # Only used by adapters whose listing is confirmed sorted newest-first (see
        # apple.py, adp_recruiting.py) to stop paginating once postings are provably
        # older than this — None means "don't assume a sort order, fetch everything."
        self.max_posting_age_days = max_posting_age_days
        # Opt-in per-source throttle (company.config["min_request_interval_seconds"])
        # for a site fronted by a request-rate-based bot challenge rather than a
        # per-status-code block (confirmed live against Waymo's careers.withwaymo.com,
        # behind a CloudFront WAF: a burst of requests — pagination plus this
        # collector's own concurrent fetch_detail calls, which share one adapter
        # instance per source — flips it into a sticky challenge window). The lock
        # serializes and paces *every* request this adapter instance makes, regardless
        # of how many run concurrently at the collector level, since the WAF counts
        # requests per IP, not per coroutine. Default 0 (no pacing) leaves every other
        # adapter's behavior unchanged.
        self._request_lock = asyncio.Lock()
        self._last_request_at: float | None = None

    @property
    def source_key(self) -> str:
        return self.company.key

    async def _pace(self) -> None:
        interval = float(self.company.config.get("min_request_interval_seconds", 0) or 0)
        if interval <= 0:
            return
        async with self._request_lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            if self._last_request_at is not None:
                wait = interval - (now - self._last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = loop.time()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        retryable = {429, 500, 502, 503, 504}
        # Per-source override of how many retries a sticky rate-limit challenge gets —
        # its backoff window is much longer than a transient 429/5xx, so the same
        # global collection.max_retries budget can be too small to ever clear it.
        # Defaults to collection.max_retries so every other adapter is unaffected.
        max_retries = int(self.company.config.get("max_retries", self.collection.max_retries))
        for attempt in range(max_retries + 1):
            await self._pace()
            try:
                response = await self.client.request(method, url, **kwargs)
                # AWS WAF's rate-based bot challenge answers with a plain HTTP 202 and
                # an empty body — not an error status, so indistinguishable from a
                # genuinely empty listing/detail page by status code alone (confirmed
                # live: Waymo's html_paginated adapter read this as "0 cards, no
                # next-link" and silently stopped paginating rather than erroring).
                # The x-amzn-waf-action header is the only reliable signal.
                is_waf_challenge = response.headers.get("x-amzn-waf-action") == "challenge"
                if response.status_code not in retryable and not is_waf_challenge:
                    response.raise_for_status()
                    return response
                if attempt == max_retries:
                    if is_waf_challenge:
                        raise AdapterError(
                            f"WAF challenge not cleared after {attempt + 1} attempts: {url}"
                        )
                    response.raise_for_status()
                if is_waf_challenge:
                    # The challenge window is sticky and self-clears only once the IP
                    # goes quiet for a while — a short jittered backoff (right for a
                    # transient 429/5xx) just re-triggers it on the next attempt.
                    delay = min(60.0, 5.0 * (2**attempt)) + random.uniform(0, 1.0)
                else:
                    retry_after = response.headers.get("Retry-After")
                    delay = _retry_delay(retry_after, attempt)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == max_retries:
                    raise
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @abstractmethod
    async def fetch_summaries(self) -> list[JobSummary]: ...

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail()

    async def aclose(self) -> None:
        """Release any adapter-owned resources (e.g. a persistent browser session).
        Default no-op; override in adapters that hold long-lived resources."""
        return None

    async def healthcheck(self) -> SourceHealth:
        try:
            jobs = await self.fetch_summaries()
            return SourceHealth(
                source_key=self.source_key,
                company=self.company.company,
                status=HealthStatus.OK,
                job_count=len(jobs),
            )
        except Exception as exc:  # health boundary intentionally captures source-local failures
            return SourceHealth(
                source_key=self.source_key,
                company=self.company.company,
                status=HealthStatus.FAILED,
                error_type=type(exc).__name__,
                message=str(exc),
            )


def _retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return min(float(value), 30.0)
        except ValueError:
            try:
                target = email.utils.parsedate_to_datetime(value)
                return max(0.0, min((target - datetime.now(target.tzinfo)).total_seconds(), 30.0))
            except (TypeError, ValueError):
                pass
    return min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)


def nested(data: Any, path: str, default: Any = None) -> Any:
    current = data
    for part in path.split(".") if path else []:
        if isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current
