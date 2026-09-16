from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..models import JobDetail, JobSummary
from ..normalizer import normalize_text, parse_flexible_date, stringify
from .base import JobAdapter, SchemaError

_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});", re.S)


def _extract_page_data(text: str) -> dict:
    """Paylocity Recruiting's public job-board listing
    (`recruiting.paylocity.com/recruiting/jobs/All/<guid>/<slug>`) server-renders its
    entire job list into one `window.pageData = {...};` JS object literal that a
    client-side React widget then hydrates from — confirmed live: every job (JobId,
    JobTitle, a structured JobLocation with City/State/Zip/Country, PublishedDate, a
    ~120-char truncated Description) is already present in the plain HTML response, no
    separate listing API call needed or found. The non-greedy `\\{.*?\\}` still finds
    the correct outer boundary despite nested objects (JobLocation) because every nested
    close is followed by "," while only the true outer close is followed by ";" —
    confirmed by round-tripping the real payload, not assumed."""
    match = _PAGE_DATA.search(text)
    if not match:
        raise SchemaError("Paylocity listing page missing window.pageData")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SchemaError("Paylocity pageData is not valid JSON") from exc


def _sibling_content_html(tree: HTMLParser, label: str) -> str | None:
    """The detail page's Job Type/Description/Requirements sections are just three
    plain sibling `<div class="job-listing-header">` + content-`<div>` pairs with no
    id/class distinguishing one field's content from another — same trap as BMW's
    `.rtltextaligneligible` (successfactors_rmk_v2.py): match by the header's own text,
    not by position, since a tenant missing one section (e.g. no "Job Type") would
    otherwise silently shift every later field's match."""
    for header in tree.css("div.job-listing-header"):
        if normalize_text(header.text()) != label:
            continue
        node = header.next
        while node is not None and node.tag == "-text":
            node = node.next
        return node.html if node is not None else None
    return None


class PaylocityAdapter(JobAdapter):
    """Paylocity Recruiting ("Citrus HR"), a shared ATS platform hosting many unrelated
    employers' career sites at recruiting.paylocity.com under a per-tenant module id —
    configured per company via `list_url` (each tenant's own `.../Jobs/All/<guid>/<slug>`
    URL). No pagination endpoint exists or was needed: `window.pageData` embeds every
    open job for the tenant in one response regardless of count (confirmed against a
    12-job tenant; revisit if a much larger Paylocity tenant is onboarded later and its
    full list isn't actually present).

    The detail page's own schema.org JobPosting JSON-LD block is NOT used here despite
    being present on some job pages — confirmed live it's silently absent on others
    (e.g. a job whose listing-level JobLocation carries no city/state, only a country),
    so it can't be relied on as a general mechanism the way `html_paginated.py` relies
    on it elsewhere. The Job Type/Description/Requirements `<div>` pairs below are
    present on every sampled detail page instead, JSON-LD or not, so that's the
    universal path. Likewise `fetch_detail` never overrides `posted_at`: the JSON-LD
    `datePosted` it does carry when present was confirmed, live, to run exactly ~5 hours
    after the listing's own per-job `PublishedDate` for the same job on repeated fetches
    (stable, not live-drifting like Honda's — but a fixed offset, not the true original
    post time) — `PublishedDate` (already captured in `fetch_summaries`) is the
    trustworthy field."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        list_url = cfg.get("list_url")
        if not list_url:
            raise SchemaError("list_url is not configured")
        response = await self.request("GET", list_url)
        data = _extract_page_data(response.text)
        jobs_raw = data.get("Jobs")
        if not isinstance(jobs_raw, list):
            raise SchemaError("Paylocity pageData missing Jobs list")
        detail_base = cfg.get(
            "detail_base_url", "https://recruiting.paylocity.com/Recruiting/Jobs/Details/"
        )
        summaries: list[JobSummary] = []
        for item in jobs_raw:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("JobId") or "").strip()
            title = normalize_text(item.get("JobTitle"))
            if not job_id or not title:
                raise SchemaError("required JobId/JobTitle missing from a Paylocity listing entry")
            location = item.get("JobLocation") or {}
            summaries.append(
                JobSummary(
                    source_key=self.source_key,
                    source_platform=self.company.platform or "paylocity",
                    company=self.company.company,
                    job_id=job_id,
                    title=title,
                    url=urljoin(detail_base, job_id),
                    location_raw=stringify(item.get("LocationName")),
                    city=stringify(location.get("City")),
                    state=stringify(location.get("State")),
                    country=stringify(location.get("Country")),
                    department=stringify(item.get("HiringDepartment")),
                    posted_at=parse_flexible_date(item.get("PublishedDate")),
                    raw=item,
                )
            )
        return summaries

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        tree = HTMLParser(response.text)
        description_html = _sibling_content_html(tree, "Description")
        requirements_html = _sibling_content_html(tree, "Requirements")
        description = "".join(filter(None, [description_html, requirements_html])) or None
        employment_type = None
        job_type_html = _sibling_content_html(tree, "Job Type")
        if job_type_html:
            employment_type = normalize_text(HTMLParser(job_type_html).text()) or None
        return JobDetail(description=description, employment_type=employment_type)
