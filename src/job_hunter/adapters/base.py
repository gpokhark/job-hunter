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

    @property
    def source_key(self) -> str:
        return self.company.key

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        retryable = {429, 500, 502, 503, 504}
        for attempt in range(self.collection.max_retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                if response.status_code not in retryable:
                    response.raise_for_status()
                    return response
                if attempt == self.collection.max_retries:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                delay = _retry_delay(retry_after, attempt)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self.collection.max_retries:
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
