import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from job_hunter.adapters.adp_recruiting import AdpRecruitingAdapter
from job_hunter.adapters.apple import AppleAdapter
from job_hunter.adapters.html_multi_index import HtmlMultiIndexAdapter
from job_hunter.adapters.html_paginated import HtmlPaginatedAdapter
from job_hunter.adapters.json_api import _date
from job_hunter.adapters.lever import LeverAdapter
from job_hunter.adapters.oracle_hcm import OracleHcmAdapter
from job_hunter.adapters.phenom import PhenomAdapter
from job_hunter.adapters.workday import WorkdayAdapter
from job_hunter.config import CollectionConfig, CompanyConfig

FIXTURES = Path(__file__).parent / "fixtures"


def test_date_parses_epoch_millis_and_seconds():
    assert _date("1762389866472").year == 2025
    assert _date("1762389866").year == 2025
    assert _date("2026-08-20T12:00:00Z").year == 2026
    assert _date(None) is None
    assert _date("not a date") is None


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
    assert detail.posted_at == _date("2026-05-29")
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
    assert detail.posted_at == _date("2026-05-29")


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
