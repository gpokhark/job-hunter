from __future__ import annotations

from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from ..models import JobDetail, JobSummary
from ..normalizer import fallback_job_id, normalize_text
from .base import JobAdapter, SchemaError


def _parse_short_date(text: str | None) -> datetime | None:
    """BMW's listing API reports dates as "M/D/YY" (e.g. "7/31/26") — a shape none of
    normalizer.py's existing date parsers cover (its 4-digit-year "%m/%d/%Y" is close but
    not the same format)."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%m/%d/%y").replace(tzinfo=UTC)
    except ValueError:
        return None


class SuccessFactorsRmkV2Adapter(JobAdapter):
    """A second, unrelated-looking template of the same underlying SuccessFactors
    Recruiting Marketing ("Job2Web"/j2w) platform the classic `successfactors_rmk`
    adapter already covers (confirmed: BMW's job detail page pulls its CSS/JS from
    `rmkcdn.successfactors.com` and its static assets live under `/platform/js/j2w/...`,
    the same platform family as the Volkswagen-Group entries) — but configured with a
    client-rendered web-component search UI instead of RMK's classic server-rendered
    `tr.data-row` table, so `html_paginated`'s plain-HTML card scraping finds nothing on
    the listing page. Rendering it once with Scrapling/Playwright surfaced the real
    listing call the widget makes: an unauthenticated `POST .../services/recruiting/v1/
    jobs` with a JSON body (confirmed working identically via plain httpx with no
    cookies/session) that paginates by a 0-indexed `pageNumber` field rather than a URL
    query param, and filters by a free-text `location` field (confirmed: combining it
    with `facetFilters` breaks nothing here, unlike Volkswagen-Group's `q=`+
    `optionsFacetsDD_country=` combination two paragraphs of history below — the two
    templates' filtering quirks are independent of each other).

    The job *detail* page, by contrast, needs no browser at all: it's plain, static,
    server-rendered HTML — just not in a shape any existing selector config covers. The
    posting's fields live in repeated `.joblayouttoken` blocks (one for Job Title,
    Posting Start Date, Job Location, Job Description), each pairing a `.joblayouttoken-
    label` text ("Job Description:") with a value in a same-class `.rtltextaligneligible`
    span — so a plain `.rtltextaligneligible` CSS selector matches all four values
    indiscriminately (title/date/location/description alike) with no way to select only
    the description one; `_parse_job_layout_tokens` below matches by label text instead of
    position, since page order is a template detail, not a contract."""

    def _list_url(self) -> str:
        list_url = self.company.config.get("list_url")
        if not list_url:
            raise SchemaError("list_url is not configured")
        return list_url

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        list_url = self._list_url()
        locale = cfg.get("locale", "en_US")
        location_filter = cfg.get("location_filter", "")
        base = cfg.get("detail_base_url")
        if not base:
            raise SchemaError("detail_base_url is not configured")
        jobs: list[JobSummary] = []
        total = None
        for page in range(int(cfg.get("max_pages", 20))):
            body = {
                "locale": locale,
                "pageNumber": page,
                "sortBy": "",
                "keywords": "",
                "location": location_filter,
                "facetFilters": {},
                "brand": "",
                "skills": [],
                "categoryId": 0,
                "alertId": "",
                "rcmCandidateId": "",
            }
            response = await self.request("POST", list_url, json=body)
            payload = response.json()
            if total is None:
                total = int(payload.get("totalJobs", 0) or 0)
            results = payload.get("jobSearchResult") or []
            if not results:
                break
            for entry in results:
                item = entry.get("response") or {}
                title = str(item.get("unifiedStandardTitle") or "").strip()
                url_title = str(item.get("urlTitle") or "").strip()
                native_id = str(item.get("id") or "").strip()
                if not title or not url_title or not native_id:
                    raise SchemaError("job entry missing required title/urlTitle/id")
                location = " / ".join(
                    loc.strip()
                    for loc in item.get("jobLocationShort") or []
                    if isinstance(loc, str) and loc.strip()
                ) or None
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform=self.company.platform or "successfactors_rmk_v2",
                        company=self.company.company,
                        job_id=native_id
                        or fallback_job_id(self.company.company, title, location, url_title),
                        title=title,
                        url=f"{base}{url_title}/{native_id}-{locale}",
                        location_raw=location,
                        posted_at=_parse_short_date(item.get("unifiedStandardStart")),
                        raw=item,
                    )
                )
            if len(jobs) >= total:
                break
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        tokens = _parse_job_layout_tokens(response.text)
        description = tokens.get("job description")
        if not description:
            # CNH's detail page (same platform, a different customer's template
            # instance) has no `.joblayouttoken-label` for its description at all —
            # the text instead sits in a bare `span[itemprop="description"]` with no
            # adjacent label to key on (confirmed: two such spans per page, one empty,
            # one populated — take the first with real text).
            tree = HTMLParser(response.text)
            for node in tree.css('span[itemprop="description"]'):
                text = normalize_text(node.text())
                if text:
                    description = text
                    break
        return JobDetail(description=description, posted_at=None)


def _parse_job_layout_tokens(html: str) -> dict[str, str]:
    tree = HTMLParser(html)
    tokens: dict[str, str] = {}
    for node in tree.css(".joblayouttoken"):
        label_node = node.css_first(".joblayouttoken-label")
        value_node = node.css_first(".rtltextaligneligible")
        if not label_node or not value_node:
            continue
        label = normalize_text(label_node.text()).rstrip(":").lower()
        tokens[label] = normalize_text(value_node.text())
    return tokens
