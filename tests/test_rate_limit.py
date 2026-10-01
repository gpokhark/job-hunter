import httpx
import pytest
import respx
from pydantic import ValidationError

from job_hunter.adapters.base import JobAdapter, ListingTimeout, RateLimitError, _parse_retry_after
from job_hunter.config import CollectionConfig, CompanyConfig
from job_hunter.models import HealthStatus, JobSummary, SourceHealth


class _Adapter(JobAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        return []


def _company() -> CompanyConfig:
    return CompanyConfig(key="acme", company="Acme", adapter="apple", config={})


def _summary(job_id: str) -> JobSummary:
    return JobSummary(
        source_key="acme", source_platform="x", company="Acme", job_id=job_id,
        title=f"Role {job_id}", url=f"https://example.com/{job_id}",
    )


@pytest.mark.asyncio
@respx.mock
async def test_terminal_429_raises_rate_limit_error_with_retry_after():
    respx.get("https://example.com/list").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(_company(), client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError) as info:
            await adapter.request("GET", "https://example.com/list")
    assert info.value.http_status == 429
    assert info.value.retry_after_seconds == 120.0
    assert info.value.url == "https://example.com/list"
    assert "HTTP 429" in str(info.value)


@pytest.mark.asyncio
@respx.mock
async def test_terminal_waf_challenge_raises_rate_limit_error_keeping_its_message():
    respx.get("https://example.com/list").mock(
        return_value=httpx.Response(202, headers={"x-amzn-waf-action": "challenge"})
    )
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(_company(), client, CollectionConfig(max_retries=0))
        with pytest.raises(RateLimitError, match="WAF challenge not cleared") as info:
            await adapter.request("GET", "https://example.com/list")
    assert info.value.http_status == 202


@pytest.mark.asyncio
@respx.mock
async def test_terminal_500_is_still_a_plain_http_status_error():
    respx.get("https://example.com/list").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        adapter = _Adapter(_company(), client, CollectionConfig(max_retries=0))
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.request("GET", "https://example.com/list")


@pytest.mark.parametrize("value", [None, "", "soon", "nan", "inf", "-inf"])
def test_unusable_retry_after_values_report_none_or_zero_never_non_finite(value):
    result = _parse_retry_after(value)
    assert result is None or result == 0.0


def test_retry_after_numeric_and_http_date():
    assert _parse_retry_after("90") == 90.0
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # long in the past


def test_kept_flattens_listings_and_dedupes_by_job_id_first_wins():
    adapter = _Adapter(_company(), None, CollectionConfig())
    first = adapter.begin_listing()
    first.append(_summary("a"))
    first.append(_summary("b"))
    second = adapter.begin_listing()
    second.append(_summary("b"))
    second.append(_summary("c"))
    adapter.keep([_summary("c"), _summary("d")])
    assert [job.job_id for job in adapter.kept] == ["a", "b", "c", "d"]


def test_kept_is_empty_until_something_is_registered():
    adapter = _Adapter(_company(), None, CollectionConfig())
    assert adapter.kept == []


def test_listing_timeout_is_an_adapter_error():
    from job_hunter.adapters.base import AdapterError

    assert issubclass(ListingTimeout, AdapterError)


def test_source_health_failure_fields_default_to_none_and_validate_kind():
    health = SourceHealth(source_key="a", company="A", status=HealthStatus.OK)
    assert (health.failure_kind, health.http_status, health.retry_after_seconds) == (None, None, None)
    limited = SourceHealth(
        source_key="a", company="A", status=HealthStatus.WARNING,
        failure_kind="rate_limited", http_status=429, retry_after_seconds=90.0,
    )
    assert limited.failure_kind == "rate_limited"
    with pytest.raises(ValidationError):
        SourceHealth(source_key="a", company="A", status=HealthStatus.WARNING, failure_kind="bogus")
