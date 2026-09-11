import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from job_hunter.adapters.adp_recruiting import AdpRecruitingAdapter
from job_hunter.adapters.apple import AppleAdapter
from job_hunter.adapters.ashby import AshbyAdapter
from job_hunter.adapters.base import SchemaError
from job_hunter.adapters.bosch import BoschAdapter
from job_hunter.adapters.eightfold import EightfoldAdapter
from job_hunter.adapters.greenhouse import GreenhouseAdapter
from job_hunter.adapters.html_multi_index import HtmlMultiIndexAdapter
from job_hunter.adapters.html_paginated import HtmlPaginatedAdapter
from job_hunter.adapters.lever import LeverAdapter
from job_hunter.adapters.oracle_hcm import OracleHcmAdapter
from job_hunter.adapters.paycom import PaycomAdapter
from job_hunter.adapters.phenom import PhenomAdapter
from job_hunter.adapters.smartrecruiters import SmartRecruitersAdapter
from job_hunter.adapters.successfactors_rmk_v2 import SuccessFactorsRmkV2Adapter
from job_hunter.adapters.workday import WorkdayAdapter
from job_hunter.config import CollectionConfig, CompanyConfig
from job_hunter.models import JobSummary, WorkArrangement
from job_hunter.normalizer import parse_flexible_date

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@respx.mock
async def test_lever_fixture():
    url = "https://api.example/jobs"
    payload = json.loads((FIXTURES / "lever/jobs.json").read_text())
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))
    company = CompanyConfig(
        key="tri",
        company="TRI",
        adapter="lever",
        config={
            "list_url": url,
            "items_path": "",
            "detail_base_url": "https://jobs.example",
            "fields": {
                "id": "id",
                "title": "text",
                "url": "hostedUrl",
                "location": "categories.location",
                "department": "categories.team",
                "employment_type": "categories.commitment",
                "posted_at": "createdAt",
            },
            "listing_description_path": "descriptionPlain",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = LeverAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert jobs[0].job_id == "tri-1"
    assert jobs[0].location_raw == "Los Altos, CA"
    assert "autonomous" in detail.description


@pytest.mark.asyncio
@respx.mock
async def test_ashby_reads_structured_location_and_work_arrangement():
    """Confirmed live against api.ashbyhq.com/posting-api/job-board/openai: a single
    zero-auth request returns every job with clean (non-double-encoded) descriptionHtml
    inline and a structured address.postalAddress — no per-job detail fetch needed."""
    url = "https://api.ashbyhq.com/posting-api/job-board/example"
    payload = {
        "jobs": [
            {
                "id": "abc-123",
                "title": "Software Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/example/abc-123",
                "location": "San Francisco",
                "department": "Engineering",
                "employmentType": "FullTime",
                "publishedAt": "2026-03-12T16:38:15.322+00:00",
                "workplaceType": "Hybrid",
                "descriptionHtml": "<p>Build things.</p>",
                "address": {
                    "postalAddress": {
                        "addressCountry": "United States",
                        "addressRegion": "California",
                        "addressLocality": "San Francisco",
                    }
                },
            }
        ]
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))
    company = CompanyConfig(
        key="example",
        company="Example",
        adapter="ashby",
        config={
            "list_url": url,
            "items_path": "jobs",
            "listing_description_path": "descriptionHtml",
            "fields": {
                "id": "id",
                "title": "title",
                "url": "jobUrl",
                "location": "location",
                "department": "department",
                "employment_type": "employmentType",
                "posted_at": "publishedAt",
                "work_arrangement": "workplaceType",
                "country": "address.postalAddress.addressCountry",
                "state": "address.postalAddress.addressRegion",
                "city": "address.postalAddress.addressLocality",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = AshbyAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert jobs[0].job_id == "abc-123"
    assert jobs[0].url == "https://jobs.ashbyhq.com/example/abc-123"
    assert jobs[0].country == "United States"
    assert jobs[0].state == "California"
    assert jobs[0].city == "San Francisco"
    assert jobs[0].work_arrangement == WorkArrangement.HYBRID
    assert detail.description == "<p>Build things.</p>"


@pytest.mark.asyncio
@respx.mock
async def test_ashby_work_arrangement_unknown_when_not_configured():
    """work_arrangement is opt-in — a company config that doesn't map it must not crash
    or guess, same as any other unconfigured field."""
    url = "https://api.ashbyhq.com/posting-api/job-board/example"
    payload = {"jobs": [{"id": "1", "title": "Engineer", "url": "https://example.com/1"}]}
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))
    company = CompanyConfig(
        key="example", company="Example", adapter="ashby",
        config={"list_url": url, "items_path": "jobs"},
    )
    async with httpx.AsyncClient() as client:
        jobs = await AshbyAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert jobs[0].work_arrangement == WorkArrangement.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_ashby_folds_secondary_locations_into_location_raw():
    """The exact bug career-ops' own ashby.mjs independently hit and fixed: a posting
    whose PRIMARY label is a non-US city but that's also open to a U.S. secondaryLocation
    must carry that U.S. evidence in location_raw — otherwise evaluate_location's
    structured non-US rejection has no location text to defer to and wrongly excludes a
    genuinely US-eligible multi-location posting."""
    url = "https://api.ashbyhq.com/posting-api/job-board/example"
    payload = {
        "jobs": [
            {
                "id": "1",
                "title": "Engineer",
                "url": "https://example.com/1",
                "location": "Toronto",
                "secondaryLocations": [
                    {
                        "location": "United States",
                        "address": {
                            "postalAddress": {
                                "addressLocality": "San Francisco",
                                "addressCountry": "United States",
                            }
                        },
                    }
                ],
            }
        ]
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))
    company = CompanyConfig(
        key="example", company="Example", adapter="ashby",
        config={"list_url": url, "items_path": "jobs", "fields": {"location": "location"}},
    )
    async with httpx.AsyncClient() as client:
        jobs = await AshbyAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert jobs[0].location_raw == "Toronto · United States · San Francisco"


@pytest.mark.asyncio
@respx.mock
async def test_ashby_appends_remote_tag_when_workplace_type_remote():
    url = "https://api.ashbyhq.com/posting-api/job-board/example"
    payload = {
        "jobs": [
            {
                "id": "1", "title": "Engineer", "url": "https://example.com/1",
                "location": "San Francisco", "workplaceType": "Remote",
            }
        ]
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))
    company = CompanyConfig(
        key="example", company="Example", adapter="ashby",
        config={"list_url": url, "items_path": "jobs", "fields": {"location": "location"}},
    )
    async with httpx.AsyncClient() as client:
        jobs = await AshbyAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert jobs[0].location_raw == "San Francisco · Remote"


@pytest.mark.asyncio
@respx.mock
async def test_greenhouse_unescapes_double_encoded_content():
    """Confirmed live against boards-api.greenhouse.io/v1/boards/anthropic/jobs: the
    `content` field is HTML-entity-double-encoded (literally "&lt;div&gt;", not "<div>").
    GreenhouseAdapter must unescape it once so the description is real, renderable HTML."""
    url = "https://boards-api.greenhouse.io/v1/boards/example/jobs"
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Software Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1",
                "location": {"name": "New York City, NY"},
                "departments": [{"name": "Engineering"}],
                "first_published": "2024-12-20T13:53:38-05:00",
                "content": "&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;We do sponsor visas!&lt;/p&gt;&lt;/div&gt;",
            }
        ]
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))
    company = CompanyConfig(
        key="example",
        company="Example",
        adapter="greenhouse",
        config={
            "list_url": url,
            "items_path": "jobs",
            "listing_description_path": "content",
            "fields": {
                "id": "id",
                "title": "title",
                "url": "absolute_url",
                "location": "location.name",
                "department": "departments.0.name",
                "posted_at": "first_published",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = GreenhouseAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert jobs[0].url == "https://job-boards.greenhouse.io/example/jobs/1"
    assert jobs[0].department == "Engineering"
    assert detail.description == '<div class="content-intro"><p>We do sponsor visas!</p></div>'
    assert "&lt;" not in detail.description


def _greenhouse_company(url: str, **extra_fields):
    fields = {"id": "id", "title": "title", "url": "absolute_url", "location": "location.name"}
    fields.update(extra_fields)
    return CompanyConfig(
        key="example", company="Example", adapter="greenhouse",
        config={"list_url": url, "items_path": "jobs", "fields": fields},
    )


@pytest.mark.asyncio
@respx.mock
async def test_greenhouse_enriches_work_model_only_location_from_offices():
    """The bug career-ops' greenhouse.mjs independently hit: some boards put ONLY a
    work-model string ("Hybrid") in location.name with no city at all — the real city
    lives in a separate /offices endpoint the /jobs list never returns. Without
    enrichment, evaluate_location has no geography to work with and a genuinely
    US-eligible job would be wrongly excluded."""
    jobs_url = "https://boards-api.greenhouse.io/v1/boards/example/jobs"
    offices_url = "https://boards-api.greenhouse.io/v1/boards/example/offices"
    respx.get(jobs_url).mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "location": {"name": "Hybrid"}}]},
        )
    )
    respx.get(offices_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "offices": [
                    {
                        "name": "San Francisco, CA",
                        "departments": [{"jobs": [{"id": 1}]}],
                        "children": [],
                    }
                ]
            },
        )
    )
    company = _greenhouse_company(jobs_url)
    async with httpx.AsyncClient() as client:
        jobs = await GreenhouseAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert jobs[0].location_raw == "Hybrid · San Francisco, CA"


@pytest.mark.asyncio
@respx.mock
async def test_greenhouse_skips_offices_fetch_when_no_job_needs_it():
    """Enrichment is conditional — a board where every job already has a real city
    must never pay for the (potentially large) /offices request. respx has no mock for
    the offices URL here; if the adapter fetched it anyway, this test would fail with
    an unmocked-request error, which is exactly the assertion."""
    jobs_url = "https://boards-api.greenhouse.io/v1/boards/example/jobs"
    respx.get(jobs_url).mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "location": {"name": "New York City, NY"}}]},
        )
    )
    company = _greenhouse_company(jobs_url)
    async with httpx.AsyncClient() as client:
        jobs = await GreenhouseAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert jobs[0].location_raw == "New York City, NY"


@pytest.mark.asyncio
@respx.mock
async def test_greenhouse_offices_fetch_failure_falls_back_gracefully():
    """Best-effort enrichment: a board with no /offices (or a failed fetch) must not
    break the whole listing — just keep the bare work-model string."""
    jobs_url = "https://boards-api.greenhouse.io/v1/boards/example/jobs"
    offices_url = "https://boards-api.greenhouse.io/v1/boards/example/offices"
    respx.get(jobs_url).mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "location": {"name": "Hybrid"}}]},
        )
    )
    respx.get(offices_url).mock(return_value=httpx.Response(404))
    company = _greenhouse_company(jobs_url)
    async with httpx.AsyncClient() as client:
        jobs = await GreenhouseAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert jobs[0].location_raw == "Hybrid"


@pytest.mark.asyncio
@respx.mock
async def test_phenom_fixture():
    search_url = "https://careers.example/us/en/search-results"
    respx.get(search_url).mock(
        return_value=httpx.Response(200, text=(FIXTURES / "phenom/search.html").read_text())
    )
    detail_url = "https://careers.example/us/en/job/123/adas-test-engineer"
    respx.get(detail_url).mock(
        return_value=httpx.Response(200, text=(FIXTURES / "phenom/detail.html").read_text())
    )
    company = CompanyConfig(
        key="honda",
        company="Honda",
        adapter="phenom",
        config={"list_url": search_url},
    )
    async with httpx.AsyncClient() as client:
        adapter = PhenomAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        assert jobs[0].url == detail_url
        detail = await adapter.fetch_detail(jobs[0])
    assert jobs[0].job_id == "123"
    assert jobs[0].location_raw == "Marysville, Ohio, United States"
    assert jobs[0].posted_at is not None
    assert "Validate ADAS features" in detail.description
    assert "&lt;" not in detail.description
    assert detail.employment_type == "FULL_TIME"


def _oracle_page(job_ids: list[str], total: int) -> dict:
    return {
        "items": [
            {
                "TotalJobsCount": total,
                "requisitionList": [
                    {
                        "Id": job_id,
                        "Title": f"Role {job_id}",
                        "PrimaryLocation": "Dearborn, Michigan, United States",
                        "PostedDate": "2026-08-20",
                    }
                    for job_id in job_ids
                ],
            }
        ]
    }


@pytest.mark.asyncio
@respx.mock
async def test_oracle_hcm_pagination():
    base = "https://jobs.example/reqs?onlyData=true&finder=findReqs;offset=0"

    def _respond(request: httpx.Request) -> httpx.Response:
        if "offset=0" in str(request.url):
            return httpx.Response(200, json=_oracle_page(["1", "2"], total=3))
        return httpx.Response(200, json=_oracle_page(["3"], total=3))

    respx.get(url__regex=r".*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="ford",
        company="Ford",
        adapter="oracle_hcm",
        config={
            "paginate": True,
            "list_url": base,
            "items_path": "items.0.requisitionList",
            "total_path": "items.0.TotalJobsCount",
            "fields": {
                "id": "Id",
                "title": "Title",
                "url": "Id",
                "location": "PrimaryLocation",
                "posted_at": "PostedDate",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await OracleHcmAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert [job.job_id for job in jobs] == ["1", "2", "3"]
    assert jobs[0].posted_at is not None


@pytest.mark.asyncio
@respx.mock
async def test_oracle_hcm_public_url_template_used_for_display_not_detail_fetch():
    """The REST detail endpoint (detail_base_url) returns raw JSON — a human clicking the
    job's url should land on a real page instead. public_url_template must be what's
    shown, while fetch_detail must still hit the REST API (not the display page) to get
    a description. Regression test for a real bug: Ford/DENSO/GM/Toyota/Valeo/Nissan job
    links were opening a JSON dump instead of the posting."""
    base = "https://jobs.example/reqs?onlyData=true&finder=findReqs;offset=0"
    respx.get(base).mock(return_value=httpx.Response(200, json=_oracle_page(["42"], total=1)))
    detail_url = "https://jobs.example/details/42"
    respx.get(detail_url).mock(
        return_value=httpx.Response(200, json={"ExternalDescriptionStr": "Build ADAS features."})
    )
    company = CompanyConfig(
        key="ford",
        company="Ford",
        adapter="oracle_hcm",
        config={
            "paginate": True,
            "list_url": base,
            "items_path": "items.0.requisitionList",
            "total_path": "items.0.TotalJobsCount",
            "detail_base_url": "https://jobs.example/details/",
            "detail_description_path": "ExternalDescriptionStr",
            "public_url_template": "https://jobs.example/candidate/job/{id}",
            "fields": {
                "id": "Id",
                "title": "Title",
                "url": "Id",
                "location": "PrimaryLocation",
                "posted_at": "PostedDate",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = OracleHcmAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        assert jobs[0].url == "https://jobs.example/candidate/job/42"
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.description == "Build ADAS features."


@pytest.mark.asyncio
@respx.mock
async def test_oracle_hcm_concatenates_multiple_description_fields():
    """Regression for a real bug: Ford's Oracle tenant splits a posting across three
    separate fields (ExternalDescriptionStr/ExternalResponsibilitiesStr/
    ExternalQualificationsStr) instead of putting everything in one — a single-path
    detail_description_path silently dropped Qualifications entirely, including each
    job's visa-sponsorship statement. A list of paths must concatenate all of them, and
    must not choke on an empty field (DENSO's tenant, confirmed live, leaves
    Responsibilities/Qualifications empty and puts everything in the first field alone)."""
    base = "https://jobs.example/reqs?onlyData=true&finder=findReqs;offset=0"
    respx.get(base).mock(return_value=httpx.Response(200, json=_oracle_page(["42"], total=1)))
    respx.get("https://jobs.example/details/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "ExternalDescriptionStr": "Build ADAS features.",
                "ExternalResponsibilitiesStr": "",
                "ExternalQualificationsStr": "Visa sponsorship is not available for this position.",
            },
        )
    )
    company = CompanyConfig(
        key="ford",
        company="Ford",
        adapter="oracle_hcm",
        config={
            "paginate": True,
            "list_url": base,
            "items_path": "items.0.requisitionList",
            "total_path": "items.0.TotalJobsCount",
            "detail_base_url": "https://jobs.example/details/",
            "detail_description_path": [
                "ExternalDescriptionStr",
                "ExternalResponsibilitiesStr",
                "ExternalQualificationsStr",
            ],
            "fields": {"id": "Id", "title": "Title", "url": "Id", "location": "PrimaryLocation"},
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = OracleHcmAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert "Build ADAS features." in detail.description
    assert "Visa sponsorship is not available" in detail.description


def _smartrecruiters_page(job_ids: list[str], total: int) -> dict:
    return {
        "totalFound": total,
        "content": [
            {
                "id": job_id,
                "name": f"Role {job_id}",
                "ref": f"https://api.example/v1/companies/Acme/postings/{job_id}",
                "location": {"fullLocation": "Sunnyvale, CA, United States", "city": "Sunnyvale", "region": "CA", "country": "us"},
                "releasedDate": "2026-09-04T21:32:25.944Z",
            }
            for job_id in job_ids
        ],
    }


@pytest.mark.asyncio
@respx.mock
async def test_smartrecruiters_pagination():
    """Server caps limit at 100/page regardless of what's requested (confirmed live
    against Intuitive's board) — paginate:true must keep incrementing offset by however
    many items actually came back until totalFound is reached, not assume a fixed page
    size matches what was requested."""
    list_url = "https://api.example/v1/companies/Acme/postings"

    def _respond(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("offset") == "0":
            return httpx.Response(200, json=_smartrecruiters_page(["1", "2"], total=3))
        return httpx.Response(200, json=_smartrecruiters_page(["3"], total=3))

    respx.get(url__regex=r".*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="acme",
        company="Acme",
        adapter="smartrecruiters",
        config={
            "paginate": True,
            "list_url": list_url,
            "items_path": "content",
            "total_path": "totalFound",
            "fields": {
                "id": "id",
                "title": "name",
                "url": "ref",
                "location": "location.fullLocation",
                "posted_at": "releasedDate",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await SmartRecruitersAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert [job.job_id for job in jobs] == ["1", "2", "3"]
    assert jobs[0].posted_at is not None


@pytest.mark.asyncio
@respx.mock
async def test_smartrecruiters_public_url_template_and_description_sections():
    """The listing item's own `ref` field is the raw API detail endpoint (a JSON dump, not
    a page a human should open) — public_url_template must be what's shown, and
    fetch_detail must still hit the real API endpoint via that same `ref` for a
    description, concatenating just the job-specific sections (not the generic
    companyDescription boilerplate)."""
    list_url = "https://api.example/v1/companies/Acme/postings"
    respx.get(list_url).mock(return_value=httpx.Response(200, json=_smartrecruiters_page(["42"], total=1)))
    respx.get("https://api.example/v1/companies/Acme/postings/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobAd": {
                    "sections": {
                        "companyDescription": {"text": "About Acme."},
                        "jobDescription": {"text": "Build robots."},
                        "qualifications": {"text": "5 years experience."},
                    }
                }
            },
        )
    )
    company = CompanyConfig(
        key="acme",
        company="Acme",
        adapter="smartrecruiters",
        config={
            "paginate": True,
            "list_url": list_url,
            "items_path": "content",
            "total_path": "totalFound",
            "public_url_template": "https://jobs.smartrecruiters.com/Acme/{id}",
            "detail_description_path": ["jobAd.sections.jobDescription.text", "jobAd.sections.qualifications.text"],
            "fields": {"id": "id", "title": "name", "url": "ref", "location": "location.fullLocation"},
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = SmartRecruitersAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        assert jobs[0].url == "https://jobs.smartrecruiters.com/Acme/42"
        detail = await adapter.fetch_detail(jobs[0])
    assert "Build robots." in detail.description
    assert "5 years experience." in detail.description
    assert "About Acme." not in detail.description


def _paycom_page_html(token: str) -> str:
    return f'<html><script>var configsFromHost = {{"sessionJWT":"{token}"}};</script></html>'


@pytest.mark.asyncio
@respx.mock
async def test_paycom_token_scrape_and_search():
    """The career-page widget has no job data in its own plain HTML — every page load
    embeds a short-lived anonymous bearer token that must be scraped and replayed on the
    real search API. Regression: the token must come from the page text, never guessed
    or hardcoded, since a stale token would silently 401."""
    career_page_url = "https://jobs.example/portal/ABC/career-page"
    search_url = "https://jobs.example/api/search"
    respx.get(career_page_url).mock(return_value=httpx.Response(200, text=_paycom_page_html("tok-123")))

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok-123"
        return httpx.Response(
            200,
            json={
                "jobPostingPreviewsCount": 1,
                "jobPostingPreviews": [
                    {"jobId": 42, "jobTitle": "Widget Engineer", "locations": "Plymouth, MI"}
                ],
            },
        )

    respx.post(search_url).mock(side_effect=_respond)
    company = CompanyConfig(
        key="isuzu",
        company="Isuzu",
        adapter="paycom",
        config={
            "career_page_url": career_page_url,
            "search_url": search_url,
            "detail_api_url": "https://jobs.example/api/job-postings/{id}",
            "public_base_url": "https://jobs.example/portal/ABC",
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await PaycomAdapter(company, client, CollectionConfig(max_retries=0)).fetch_summaries()
    assert len(jobs) == 1
    assert jobs[0].job_id == "42"
    assert jobs[0].title == "Widget Engineer"
    assert jobs[0].url == "https://jobs.example/portal/ABC/jobs/42"


@pytest.mark.asyncio
@respx.mock
async def test_paycom_detail_parses_google_job_json_date():
    """The listing preview's description is truncated and postedOn is always empty on
    this platform — fetch_detail must re-mint its own token (a job detail page embeds
    one too, independent of the listing's) and read the full description plus the
    embedded googleJobJson *string* (needs its own json.loads) for a real datePosted."""
    job_url = "https://jobs.example/portal/ABC/jobs/42"
    detail_url = "https://jobs.example/api/job-postings/42"
    respx.get(job_url).mock(return_value=httpx.Response(200, text=_paycom_page_html("tok-456")))

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok-456"
        return httpx.Response(
            200,
            json={
                "jobPosting": {
                    "description": "Build widgets.",
                    "qualifications": "5 years experience.",
                    "googleJobJson": '{"datePosted": "2026-07-29"}',
                }
            },
        )

    respx.get(detail_url).mock(side_effect=_respond)
    company = CompanyConfig(
        key="isuzu",
        company="Isuzu",
        adapter="paycom",
        config={
            "career_page_url": "https://jobs.example/portal/ABC/career-page",
            "search_url": "https://jobs.example/api/search",
            "detail_api_url": "https://jobs.example/api/job-postings/{id}",
            "public_base_url": "https://jobs.example/portal/ABC",
        },
    )
    summary = JobSummary(
        source_key="isuzu",
        source_platform="paycom",
        company="Isuzu",
        job_id="42",
        title="Widget Engineer",
        url=job_url,
    )
    async with httpx.AsyncClient() as client:
        detail = await PaycomAdapter(company, client, CollectionConfig(max_retries=0)).fetch_detail(summary)
    assert "Build widgets." in detail.description
    assert "5 years experience." in detail.description
    assert detail.posted_at == datetime(2026, 7, 29, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_workday_native_public_base_url_used_for_display_not_detail_fetch():
    """Same regression as the Oracle HCM case: the CXS API host (list_url) returns raw
    JSON when opened directly — public_base_url (a real Workday-hosted page) must be
    what's shown, while fetch_detail must still hit the CXS API for a description."""
    list_url = "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/jobs"
    respx.post(list_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "ADAS Engineer",
                        "externalPath": "/job/Some-City/ADAS-Engineer_JR-1",
                        "jobId": "JR-1",
                        "postedOn": "Posted Today",
                    }
                ],
            },
        )
    )
    detail_api_url = "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/job/Some-City/ADAS-Engineer_JR-1"
    respx.get(detail_api_url).mock(
        return_value=httpx.Response(
            200, json={"jobPostingInfo": {"jobDescription": "Build ADAS features."}}
        )
    )
    company = CompanyConfig(
        key="tenant",
        company="Tenant",
        adapter="workday",
        config={
            "workday_native": True,
            "list_url": list_url,
            "public_base_url": "https://tenant.wd1.myworkdayjobs.com/en-US/site/",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = WorkdayAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        assert jobs[0].url == "https://tenant.wd1.myworkdayjobs.com/en-US/site/job/Some-City/ADAS-Engineer_JR-1"
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.description == "Build ADAS features."


@pytest.mark.asyncio
@respx.mock
async def test_workday_native_skips_malformed_rows_instead_of_failing_source():
    """Confirmed live 2026-09-08 (Valeo: 1 of 1016, NVIDIA: 32 of the 2000-cap): Workday
    tenants return individual listing entries with every content field null except
    bulletFields (a bare "JR2018101"-style identifier) for postings in a withdrawn-ish
    state. One such entry must be skipped with a warning, not fail the entire 1,000+-job
    source as the old code did — but a page of *only* malformed entries (a genuine
    response-shape change) must still raise SchemaError loudly."""
    list_url = "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/jobs"
    respx.post(list_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 3,
                "jobPostings": [
                    {
                        "title": "ADAS Engineer",
                        "externalPath": "/job/Some-City/ADAS-Engineer_JR-1",
                        "jobId": "JR-1",
                        "postedOn": "Posted Today",
                    },
                    # The malformed shape seen live: only a bare identifier in bulletFields.
                    {"bulletFields": ["JR2018101"]},
                    {
                        "title": "Test Engineer",
                        "externalPath": "/job/Some-City/Test-Engineer_JR-2",
                        "jobId": "JR-2",
                        "postedOn": "Posted Today",
                    },
                ],
            },
        )
    )
    company = CompanyConfig(
        key="tenant",
        company="Tenant",
        adapter="workday",
        config={
            "workday_native": True,
            "list_url": list_url,
            "public_base_url": "https://tenant.wd1.myworkdayjobs.com/en-US/site/",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = WorkdayAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
    assert [j.job_id for j in jobs] == ["JR-1", "JR-2"]

    # A whole page of malformed entries is a structural change, not a few withdrawn
    # postings — must fail loudly, not silently return zero jobs.
    respx.post(list_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [
                    {"bulletFields": ["JR0000001"]},
                    {"bulletFields": ["JR0000002"]},
                ],
            },
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = WorkdayAdapter(company, client, CollectionConfig(max_retries=0))
        with pytest.raises(SchemaError, match="only malformed"):
            await adapter.fetch_summaries()


@pytest.mark.asyncio
@respx.mock
async def test_workday_native_reads_remote_type_from_listing_and_detail():
    """Workday's own "remoteType" facet ("Hybrid", "Onsite", "Remote", "Remote/Hybrid") is
    real structured signal the adapter previously never read at all — work_arrangement fell
    back to text-sniffing location_raw, which says nothing about remote/hybrid for most
    postings (e.g. a plain "Sunnyvale, California, United States of America"), silently
    misclassifying real hybrid/remote jobs as unknown. Confirmed live: a GM posting with
    jobPostingInfo.remoteType == "Hybrid" and no "hybrid"/"remote" text anywhere in
    location_raw or the description."""
    list_url = "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/jobs"
    respx.post(list_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [
                    {
                        "title": "Hybrid Role",
                        "externalPath": "/job/Some-City/Hybrid-Role_JR-1",
                        "jobId": "JR-1",
                        "postedOn": "Posted Today",
                        "remoteType": "Remote/Hybrid",
                    },
                    {
                        "title": "Onsite Role",
                        "externalPath": "/job/Some-City/Onsite-Role_JR-2",
                        "jobId": "JR-2",
                        "postedOn": "Posted Today",
                        "remoteType": "Onsite",
                    },
                ],
            },
        )
    )
    respx.get(
        "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/job/Some-City/Hybrid-Role_JR-1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"jobPostingInfo": {"jobDescription": "Build ADAS features.", "remoteType": "Hybrid"}},
        )
    )
    company = CompanyConfig(
        key="tenant",
        company="Tenant",
        adapter="workday",
        config={
            "workday_native": True,
            "list_url": list_url,
            "public_base_url": "https://tenant.wd1.myworkdayjobs.com/en-US/site/",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = WorkdayAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        # "Remote/Hybrid" resolves to HYBRID via the same hybrid-beats-remote text
        # precedence used everywhere else (detect_arrangement), not a bespoke mapping.
        assert jobs[0].work_arrangement == WorkArrangement.HYBRID
        assert jobs[1].work_arrangement == WorkArrangement.ONSITE
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.work_arrangement == WorkArrangement.HYBRID


@pytest.mark.asyncio
@respx.mock
async def test_workday_native_detail_arrangement_none_when_remote_type_absent():
    """A detail response with no remoteType field must yield work_arrangement=None, not
    UNKNOWN — None is what tells evaluate_location to fall back to parsing location_raw
    text, the same fallback behavior that existed before remoteType was read at all."""
    list_url = "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/jobs"
    respx.post(list_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "ADAS Engineer",
                        "externalPath": "/job/Some-City/ADAS-Engineer_JR-1",
                        "jobId": "JR-1",
                        "postedOn": "Posted Today",
                    }
                ],
            },
        )
    )
    respx.get(
        "https://tenant.wd1.myworkdayjobs.com/wday/cxs/tenant/site/job/Some-City/ADAS-Engineer_JR-1"
    ).mock(return_value=httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "Build ADAS features."}}))
    company = CompanyConfig(
        key="tenant",
        company="Tenant",
        adapter="workday",
        config={
            "workday_native": True,
            "list_url": list_url,
            "public_base_url": "https://tenant.wd1.myworkdayjobs.com/en-US/site/",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = WorkdayAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        assert jobs[0].work_arrangement == WorkArrangement.UNKNOWN
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.work_arrangement is None


@pytest.mark.asyncio
@respx.mock
async def test_html_fixture():
    url = "https://jobs.example/search"
    respx.get(url).mock(
        return_value=httpx.Response(200, text=(FIXTURES / "html/jobs.html").read_text())
    )
    company = CompanyConfig(
        key="ford",
        company="Ford",
        adapter="html_paginated",
        config={
            "list_url": url,
            "card_selector": ".job",
            "title_selector": ".title",
            "link_selector": ".title",
            "location_selector": ".location",
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await HtmlPaginatedAdapter(
            company, client, CollectionConfig(max_retries=0)
        ).fetch_summaries()
    assert jobs[0].job_id == "H1" and jobs[0].location_raw == "Dearborn, MI"


@pytest.mark.asyncio
@respx.mock
async def test_html_detail_json_ld_fallback():
    """A schema.org JobPosting JSON-LD block (id attribute before type=, matching
    Astemo's exact tag shape) should backfill posted_at/employment_type without
    overriding a description that description_selector already found, and should supply
    the description too when description_selector finds nothing."""
    list_url = "https://jobs.example/search"
    respx.get(list_url).mock(
        return_value=httpx.Response(200, text=(FIXTURES / "html/jobs.html").read_text())
    )
    detail_url = "https://jobs.example/job/H1"
    ld_json = (
        '{"@context":"https://schema.org","@type":"JobPosting","datePosted":"2026-05-29",'
        '"employmentType":"FULL_TIME","description":"Fallback description"}'
    )

    company_with_selector = CompanyConfig(
        key="astemo",
        company="Astemo",
        adapter="html_paginated",
        config={
            "list_url": list_url,
            "card_selector": ".job",
            "title_selector": ".title",
            "link_selector": ".title",
            "location_selector": ".location",
            "description_selector": ".real-description",
        },
    )
    html_with_selector = (
        '<div class="real-description">Real description</div>'
        f'<script id="js-job-posting" type="application/ld+json">{ld_json}</script>'
    )
    respx.get(detail_url).mock(return_value=httpx.Response(200, text=html_with_selector))
    async with httpx.AsyncClient() as client:
        adapter = HtmlPaginatedAdapter(company_with_selector, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.description == "Real description"
    assert detail.posted_at == parse_flexible_date("2026-05-29")
    assert detail.employment_type == "FULL_TIME"

    company_without_selector = CompanyConfig(
        key="astemo2",
        company="Astemo",
        adapter="html_paginated",
        config={
            "list_url": list_url,
            "card_selector": ".job",
            "title_selector": ".title",
            "link_selector": ".title",
            "location_selector": ".location",
        },
    )
    html_without_selector = f'<script id="js-job-posting" type="application/ld+json">{ld_json}</script>'
    respx.get(detail_url).mock(return_value=httpx.Response(200, text=html_without_selector))
    async with httpx.AsyncClient() as client:
        adapter = HtmlPaginatedAdapter(company_without_selector, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.description == "Fallback description"
    assert detail.posted_at == parse_flexible_date("2026-05-29")


@pytest.mark.asyncio
@respx.mock
async def test_html_posted_at_selector():
    url = "https://jobs.example/search"
    html = (
        "<table><tr class='job data-row' data-job-id='H1'>"
        "<td class='title'><a class='title' href='/job/H1'>Senior Systems Engineer</a></td>"
        "<td class='location'><span>Dearborn, MI</span></td>"
        "<td class='colDate'><span class='jobDate'>Aug 10, 2026</span></td>"
        "</tr></table>"
    )
    respx.get(url).mock(return_value=httpx.Response(200, text=html))
    company = CompanyConfig(
        key="paccar",
        company="PACCAR",
        adapter="html_paginated",
        config={
            "list_url": url,
            "card_selector": "tr.job",
            "title_selector": "a.title",
            "link_selector": "a.title",
            "location_selector": "td.location span",
            "posted_at_selector": "td.colDate span.jobDate",
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await HtmlPaginatedAdapter(
            company, client, CollectionConfig(max_retries=0)
        ).fetch_summaries()
    assert jobs[0].posted_at is not None and jobs[0].posted_at.year == 2026
    assert jobs[0].posted_at.month == 8 and jobs[0].posted_at.day == 10


def _card(job_id: str) -> str:
    return (
        f'<article class="job" data-job-id="{job_id}">'
        f'<a class="title" href="/job/{job_id}">Role {job_id}</a>'
        f'<span class="location">Detroit, MI</span></article>'
    )


@pytest.mark.asyncio
@respx.mock
async def test_html_multi_index_liferay_publication_date():
    """HRI's Liferay DDM detail pages carry no JSON-LD; posted_at should instead be
    backfilled from the inline `JobOfferData.publicationDate` JS-object literal used to
    populate the application-confirmation email."""
    index_url = "https://usa.honda-ri.com/associate-positions"
    respx.get(index_url).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<div class="b-job-item">'
                '<a class="b-button" href="https://usa.honda-ri.com/-/flight-test-team-lead">Apply</a>'
                '<div class="b-job-item__title"><h3>Flight Test Team Lead</h3></div>'
                '<div class="b-job-item__location">Los Angeles County, CA</div>'
                "</div>"
            ),
        )
    )
    detail_url = "https://usa.honda-ri.com/-/flight-test-team-lead"
    respx.get(detail_url).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<article class="journal-content-article">Real description</article>'
                "<script>var JobOfferData = {id: \"P25F15\", name: \"Flight Test Team "
                'Lead", publicationDate: "Jun 24, 2026 6:42:50 AM"};</script>'
            ),
        )
    )
    company = CompanyConfig(
        key="hri",
        company="Honda Research Institute USA",
        adapter="html_multi_index",
        config={
            "index_urls": [index_url],
            "card_selector": ".b-job-item",
            "link_selector": "a.b-button",
            "title_selector": ".b-job-item__title h3",
            "location_selector": ".b-job-item__location",
            "description_selector": ".journal-content-article",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = HtmlMultiIndexAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert detail.description == "Real description"
    assert detail.posted_at is not None
    assert detail.posted_at.date().isoformat() == "2026-06-24"


@pytest.mark.asyncio
@respx.mock
async def test_html_multi_index_fetches_urls_without_mutating_shared_config():
    """Regression test: fetch_summaries used to fetch each index_url by temporarily
    overwriting company.config["list_url"] in place and restoring it afterward — if a
    later index raised mid-loop, the shared CompanyConfig was left mutated to whichever
    URL was in flight, since the restore line was never reached. It now passes each
    index_url through as a call argument instead of mutating shared config, so a failing
    index can never corrupt it, with nothing to restore."""
    first_url = "https://careers.example/index-a"
    second_url = "https://careers.example/index-b"
    original_list_url = "https://careers.example/original"
    respx.get(first_url).mock(
        return_value=httpx.Response(
            200,
            text=(
                '<div class="job"><a href="https://careers.example/job/1">Engineer</a></div>'
            ),
        )
    )
    respx.get(second_url).mock(return_value=httpx.Response(500))

    company = CompanyConfig(
        key="multi",
        company="Multi",
        adapter="html_multi_index",
        config={
            "list_url": original_list_url,
            "index_urls": [first_url, second_url],
            "card_selector": ".job",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = HtmlMultiIndexAdapter(company, client, CollectionConfig(max_retries=0))
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.fetch_summaries()
    assert company.config["list_url"] == original_list_url


def _adp_job(req_id: str) -> dict:
    return {
        "reqId": req_id,
        "jobTitle": f"Role {req_id}",
        "publishedJobTitle": f"Role {req_id}",
        "jobDescription": "<p>Build cars.</p>",
        "jobQualifications": "<p>5 years experience.</p>",
        "postingDate": "2026-08-18T17:50:33Z",
        "requisitionLocations": [
            {
                "address": {
                    "cityName": "Auburn Hills",
                    "countrySubdivisionLevel1": {"longName": "Michigan"},
                    "country": {"longName": "United States"},
                }
            }
        ],
    }


@pytest.mark.asyncio
@respx.mock
async def test_adp_recruiting_token_handshake_and_pagination():
    """The Angular front end never renders content server-side; it fetches a public,
    unauthenticated myJobsToken from the career-site endpoint and replays it as a
    myjobstoken header on the paginated job-requisitions listing call."""
    domain = "teststellantis"
    respx.get(f"https://myjobs.adp.com/public/staffing/v1/career-site/{domain}").mock(
        return_value=httpx.Response(200, json={"myJobsToken": "test-token-123"})
    )

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("myjobstoken") == "test-token-123"
        if "%24skip=0" in str(request.url) or "$skip=0" in str(request.url):
            return httpx.Response(
                200, json={"count": 3, "jobRequisitions": [_adp_job("1"), _adp_job("2")]}
            )
        return httpx.Response(200, json={"count": 3, "jobRequisitions": [_adp_job("3")]})

    respx.get(url__regex=r"https://my\.adp\.com/.*apply-custom-filters.*").mock(
        side_effect=_respond
    )
    company = CompanyConfig(
        key="stellantis",
        company="Stellantis",
        adapter="adp_recruiting",
        config={"career_site_domain": domain, "page_size": 2},
    )
    async with httpx.AsyncClient() as client:
        adapter = AdpRecruitingAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert [job.job_id for job in jobs] == ["1", "2", "3"]
    assert jobs[0].url == "https://myjobs.adp.com/teststellantis/cx/job-details?reqId=1"
    assert jobs[0].location_raw == "Auburn Hills, Michigan, United States"
    assert jobs[0].posted_at is not None
    assert "Build cars." in detail.description
    assert "5 years experience." in detail.description


@pytest.mark.asyncio
@respx.mock
async def test_adp_recruiting_stops_pagination_once_stale():
    """This listing is confirmed sorted newest-first; once a page's oldest item is
    already past the recency cutoff, every later page is guaranteed older still — the
    adapter must stop there instead of walking the rest of the catalog."""
    domain = "teststellantis"
    respx.get(f"https://myjobs.adp.com/public/staffing/v1/career-site/{domain}").mock(
        return_value=httpx.Response(200, json={"myJobsToken": "test-token-123"})
    )
    now = datetime.now(UTC)
    recent = _adp_job("recent-1")
    recent["postingDate"] = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    stale = _adp_job("stale-1")
    stale["postingDate"] = (now - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    requested_skips: list[str] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        skip = request.url.params.get("$skip")
        requested_skips.append(skip)
        if skip == "0":
            return httpx.Response(200, json={"count": 100, "jobRequisitions": [recent]})
        if skip == "1":
            return httpx.Response(200, json={"count": 100, "jobRequisitions": [stale]})
        raise AssertionError(f"should not have paginated past the stale page (skip={skip})")

    respx.get(url__regex=r"https://my\.adp\.com/.*apply-custom-filters.*").mock(
        side_effect=_respond
    )
    company = CompanyConfig(
        key="stellantis",
        company="Stellantis",
        adapter="adp_recruiting",
        config={"career_site_domain": domain, "page_size": 1},
    )
    async with httpx.AsyncClient() as client:
        adapter = AdpRecruitingAdapter(
            company, client, CollectionConfig(max_retries=0), max_posting_age_days=30
        )
        jobs = await adapter.fetch_summaries()
    assert [job.job_id for job in jobs] == ["recent-1", "stale-1"]
    assert requested_skips == ["0", "1"]


def _hydration_html(data: dict) -> str:
    return (
        f'<html><script>window.__staticRouterHydrationData = '
        f"JSON.parse({json.dumps(json.dumps(data))});</script></html>"
    )


@pytest.mark.asyncio
@respx.mock
async def test_apple_hydration_json_pagination_and_query_merge():
    """jobs.apple.com's list_url already carries its own "location=..." query string;
    httpx's params= replaces rather than merges a URL's existing query, so the adapter
    must merge "page" into it manually or silently lose the location filter."""
    search_data = {
        "loaderData": {
            "search": {
                "totalRecords": 1,
                "searchResults": [
                    {
                        "reqId": "200672640-3760",
                        "postingTitle": "Robotics Prototyping Engineer",
                        "transformedPostingTitle": "robotics-prototyping-engineer",
                        "postDateInGMT": "2026-07-17T16:51:18.167Z",
                        "jobSummary": "Short summary.",
                        "locations": [
                            {"name": "Santa Clara", "stateProvince": "", "countryName": "United States of America"}
                        ],
                    }
                ],
            }
        }
    }
    list_url = "https://jobs.apple.com/en-us/search?location=united-states-USA"

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("location") == "united-states-USA"
        assert request.url.params.get("page") == "1"
        return httpx.Response(200, text=_hydration_html(search_data))

    respx.get(url__regex=r"https://jobs\.apple\.com/en-us/search.*").mock(side_effect=_respond)
    detail_data = {
        "loaderData": {
            "jobDetails": {
                "jobsData": {
                    "description": "Full description.",
                    "postDateInGMT": "2026-07-17T16:51:18.167+00:00",
                    "locations": [
                        {"city": "Santa Clara", "stateProvince": "California", "countryName": "United States"}
                    ],
                }
            }
        }
    }
    detail_url = "https://jobs.apple.com/en-us/details/200672640-3760/robotics-prototyping-engineer"
    respx.get(detail_url).mock(return_value=httpx.Response(200, text=_hydration_html(detail_data)))

    company = CompanyConfig(
        key="apple",
        company="Apple",
        adapter="apple",
        config={"list_url": list_url, "page_size": 20},
    )
    async with httpx.AsyncClient() as client:
        adapter = AppleAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert jobs[0].job_id == "200672640-3760"
    assert jobs[0].posted_at.date().isoformat() == "2026-07-17"
    assert jobs[0].url == detail_url
    assert detail.description == "Full description."
    assert detail.state == "California"


def _apple_result(req_id: str, posted_at: datetime) -> dict:
    return {
        "reqId": req_id,
        "postingTitle": f"Role {req_id}",
        "transformedPostingTitle": f"role-{req_id}",
        "postDateInGMT": posted_at.isoformat().replace("+00:00", "Z"),
        "jobSummary": "Short summary.",
        "locations": [{"name": "Cupertino", "stateProvince": "", "countryName": "United States"}],
    }


@pytest.mark.asyncio
@respx.mock
async def test_apple_stops_pagination_once_stale():
    """Apple's default listing order is confirmed sorted newest-first; once a batch's
    oldest item is already past the recency cutoff, later pages are guaranteed older
    still — the adapter must stop fetching further batches rather than walking the
    whole ~230-page catalog."""
    now = datetime.now(UTC)
    pages = {
        1: {"totalRecords": 60, "searchResults": [_apple_result("p1", now - timedelta(days=2))]},
        2: {"totalRecords": 60, "searchResults": [_apple_result("p2", now - timedelta(days=45))]},
    }
    requested_pages: list[str] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        requested_pages.append(page)
        if int(page) not in pages:
            raise AssertionError(f"should not have paginated past the stale page (page={page})")
        return httpx.Response(
            200,
            text=_hydration_html({"loaderData": {"search": pages[int(page)]}}),
        )

    respx.get(url__regex=r"https://jobs\.apple\.com/en-us/search.*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="apple",
        company="Apple",
        adapter="apple",
        config={
            "list_url": "https://jobs.apple.com/en-us/search?location=united-states-USA",
            "page_size": 20,
            "max_concurrent_pages": 1,
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = AppleAdapter(
            company, client, CollectionConfig(max_retries=0), max_posting_age_days=30
        )
        jobs = await adapter.fetch_summaries()
    assert [job.job_id for job in jobs] == ["p1", "p2"]
    assert requested_pages == ["1", "2"]


@pytest.mark.asyncio
@respx.mock
async def test_page_number_parameter_pagination():
    base = "https://jobs.example/search"
    page1 = f"<main>{''.join(_card(f'P{i}') for i in range(2))}</main>"
    page2 = f"<main>{_card('P2')}</main>"

    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, text=page2)
        return httpx.Response(200, text=page1)

    respx.get(url__regex=r".*").mock(side_effect=_respond)
    company = CompanyConfig(
        key="test",
        company="Test",
        adapter="html_paginated",
        config={
            "list_url": base,
            "card_selector": ".job",
            "title_selector": ".title",
            "link_selector": ".title",
            "location_selector": ".location",
            "page_number_parameter": "page",
            "page_size": 2,
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await HtmlPaginatedAdapter(
            company, client, CollectionConfig(max_retries=0)
        ).fetch_summaries()
    assert [job.job_id for job in jobs] == ["P0", "P1", "P2"]


@pytest.mark.asyncio
@respx.mock
async def test_eightfold_fixed_page_size_pagination_and_detail_fetch():
    """Eightfold's pcsx API ignores every page-size override tried (num/limit/size/
    pageSize/per_page) and always returns a server-fixed page size, so fetch_summaries
    must paginate by whatever length it actually got back, not a configured page_size,
    stopping once `start` reaches `data.count`."""
    list_url = "https://careers.example/api/pcsx/search"
    detail_url = "https://careers.example/api/pcsx/position_details"

    def _respond(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start")
        if start == "2":
            return httpx.Response(
                200,
                json={"data": {"count": 3, "positions": [
                    {
                        "id": "3",
                        "name": "Staff Engineer",
                        "positionUrl": "/careers/job/3",
                        "standardizedLocations": ["Austin, TX, US"],
                        "department": "Engineering",
                        "postedTs": 1786121028,
                    }
                ]}},
            )
        return httpx.Response(
            200,
            json={"data": {"count": 3, "positions": [
                {
                    "id": "1",
                    "name": "ADAS Engineer",
                    "positionUrl": "/careers/job/1",
                    "standardizedLocations": ["Moline, IL, US", "Waterloo, IA, US"],
                    "department": "Engineering",
                    "postedTs": 1786121028,
                },
                {
                    "id": "2",
                    "name": "Robotics Engineer",
                    "positionUrl": "/careers/job/2",
                    "standardizedLocations": ["Ames, IA, US"],
                    "department": "Engineering",
                    "postedTs": 1786121028,
                },
            ]}},
        )

    respx.get(url__regex=r".*pcsx/search.*").mock(side_effect=_respond)
    respx.get(url__regex=r".*pcsx/position_details.*").mock(
        return_value=httpx.Response(
            200, json={"data": {"jobDescription": "Build autonomous systems."}}
        )
    )
    company = CompanyConfig(
        key="deere",
        company="Deere & Company",
        adapter="eightfold",
        config={
            "list_url": list_url,
            "detail_url": detail_url,
            "public_base_url": "https://careers.example",
            "params": {"domain": "example.com", "location": "united states"},
            "detail_params": {"domain": "example.com"},
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = EightfoldAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert [job.job_id for job in jobs] == ["1", "2", "3"]
    assert jobs[0].url == "https://careers.example/careers/job/1"
    assert jobs[0].location_raw == "Moline, IL, US / Waterloo, IA, US"
    assert jobs[0].posted_at.year == 2026
    assert detail.description == "Build autonomous systems."


@pytest.mark.asyncio
@respx.mock
async def test_bosch_pagination_and_refnumber_detail_lookup():
    """Confirmed live against bosch-i3-caas-api.e-spirit.cloud: get_jobs paginates by a
    1-indexed `page` param and reports its total under
    _embedded.rh:result[0].meta[0].count; the listing payload carries no description, so
    fetch_detail re-queries the same collection filtered by refNumber and must
    concatenate all four jobAd.sections fields, not just the first."""
    list_url = "https://caas.example/get_jobs"
    detail_url = "https://caas.example/jobs.content"

    def _page(items: list[dict], total: int) -> dict:
        return {
            "_embedded": {"rh:result": [{"meta": [{"count": total}], "data": items}]}
        }

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-key"
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(
                200,
                json=_page(
                    [
                        {
                            "_id": "REF1",
                            "refNumber": "REF1",
                            "name": "Autonomous Systems Engineer",
                            "jobUrl": "REF1-autonomous-systems-engineer",
                            "releasedDate": "2026-08-01T00:00:00.000Z",
                            "location": {"city": "Sunnyvale", "workLocation": "Sunnyvale, CA"},
                            "country": {"valueLabel": "United States"},
                            "function": {"label": "Engineering"},
                            "working_hours": {"valueLabel": "Full-time"},
                            "work_mode": "on-site",
                        }
                    ],
                    total=2,
                ),
            )
        return httpx.Response(
            200,
            json=_page(
                [
                    {
                        "_id": "REF2",
                        "refNumber": "REF2",
                        "name": "Quality Engineer",
                        "jobUrl": "REF2-quality-engineer",
                        "releasedDate": "2026-08-02T00:00:00.000Z",
                        "location": {"city": "Fort Lauderdale", "workLocation": "Fort Lauderdale, FL"},
                        "country": {"valueLabel": "United States"},
                    }
                ],
                total=2,
            ),
        )

    respx.get(url__regex=r"https://caas\.example/get_jobs.*").mock(side_effect=_respond)
    respx.get(url__regex=r"https://caas\.example/jobs\.content.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "jobAd": {
                        "sections": {
                            "jobDescription": {"text": "Build ADAS."},
                            "qualifications": {"text": "5+ years."},
                        }
                    }
                }
            ],
        )
    )
    company = CompanyConfig(
        key="bosch",
        company="Bosch",
        adapter="bosch",
        config={
            "list_url": list_url,
            "detail_url": detail_url,
            "detail_base_url": "https://jobs.bosch.example/en/job/",
            "api_key": "test-key",
            "pagesize": 1,
            "fields": {
                "id": "refNumber",
                "title": "name",
                "url": "jobUrl",
                "location": "location.workLocation",
                "city": "location.city",
                "country": "country.valueLabel",
                "department": "function.label",
                "employment_type": "working_hours.valueLabel",
                "posted_at": "releasedDate",
                "work_arrangement": "work_mode",
            },
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = BoschAdapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert [job.job_id for job in jobs] == ["REF1", "REF2"]
    assert jobs[0].url == "https://jobs.bosch.example/en/job/REF1-autonomous-systems-engineer"
    assert jobs[0].work_arrangement == WorkArrangement.ONSITE
    assert detail.description == "Build ADAS.\n\n5+ years."


@pytest.mark.asyncio
@respx.mock
async def test_successfactors_rmk_v2_pagination_and_label_matched_detail():
    """Confirmed live against jobs.bmwgroup.com: the search widget POSTs a JSON body
    paginated by a 0-indexed pageNumber field, and the plain-HTML detail page repeats the
    same value CSS class (.rtltextaligneligible) for title/date/location/description
    alike — fetch_detail must match by the adjacent .joblayouttoken-label text
    ("Job Description:"), not position or a bare class selector."""
    list_url = "https://rmk.example/services/recruiting/v1/jobs"

    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["pageNumber"] == 0:
            return httpx.Response(
                200,
                json={
                    "totalJobs": 2,
                    "jobSearchResult": [
                        {
                            "response": {
                                "id": "111",
                                "unifiedStandardTitle": "Manufacturing Engineer",
                                "urlTitle": "Manufacturing-Engineer",
                                "jobLocationShort": ["Spartanburg, SC, USA, "],
                                "unifiedStandardStart": "7/31/26",
                            }
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "totalJobs": 2,
                "jobSearchResult": [
                    {
                        "response": {
                            "id": "222",
                            "unifiedStandardTitle": "Data Analyst",
                            "urlTitle": "Data-Analyst",
                            "jobLocationShort": ["Spartanburg, SC, USA, "],
                            "unifiedStandardStart": "8/1/26",
                        }
                    }
                ],
            },
        )

    respx.post(list_url).mock(side_effect=_respond)
    detail_html = """
    <div class="joblayouttoken"><span class="joblayouttoken-label">Job Title: </span>
      <span class="rtltextaligneligible">Manufacturing Engineer</span></div>
    <div class="joblayouttoken"><span class="joblayouttoken-label">Posting Start Date: </span>
      <span class="rtltextaligneligible">7/31/26</span></div>
    <div class="joblayouttoken"><span class="joblayouttoken-label">Job Description: </span>
      <span class="rtltextaligneligible">Build vehicles.</span></div>
    """
    respx.get(url__regex=r"https://rmk\.example/job/.*").mock(
        return_value=httpx.Response(200, text=detail_html)
    )
    company = CompanyConfig(
        key="bmw",
        company="BMW Group",
        adapter="successfactors_rmk_v2",
        config={
            "list_url": list_url,
            "detail_base_url": "https://rmk.example/job/",
            "locale": "en_US",
            "location_filter": "United States",
        },
    )
    async with httpx.AsyncClient() as client:
        adapter = SuccessFactorsRmkV2Adapter(company, client, CollectionConfig(max_retries=0))
        jobs = await adapter.fetch_summaries()
        detail = await adapter.fetch_detail(jobs[0])
    assert [job.job_id for job in jobs] == ["111", "222"]
    assert jobs[0].url == "https://rmk.example/job/Manufacturing-Engineer/111-en_US"
    assert jobs[0].posted_at.strftime("%Y-%m-%d") == "2026-07-31"
    assert detail.description == "Build vehicles."
