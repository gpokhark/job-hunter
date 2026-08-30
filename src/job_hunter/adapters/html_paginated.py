from __future__ import annotations

import html as html_module
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from ..models import JobDetail, JobSummary
from ..normalizer import (
    extract_job_posting_ld,
    fallback_job_id,
    normalize_text,
    parse_display_date,
)
from .base import JobAdapter, SchemaError
from .json_api import _date, _stringify


class HtmlPaginatedAdapter(JobAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        start_url = cfg.get("list_url")
        if not start_url:
            raise SchemaError("list_url is not configured")
        jobs: list[JobSummary] = []
        seen_urls: set[str] = set()
        url: str | None = start_url
        for page in range(int(cfg.get("max_pages", 20))):
            if not url or url in seen_urls:
                break
            seen_urls.add(url)
            response = await self.request("GET", url)
            tree = HTMLParser(response.text)
            cards = tree.css(cfg.get("card_selector", "[data-job-id]"))
            if not cards and not jobs:
                raise SchemaError("no job cards matched configured selector")
            for card in cards:
                link = card.css_first(cfg.get("link_selector", "a"))
                title_node = card.css_first(cfg.get("title_selector", "a"))
                if not link or not title_node or not link.attributes.get("href"):
                    raise SchemaError("job card missing required link/title")
                title = normalize_text(title_node.text())
                # Some client-side routers emit hrefs relative to a shorter base path than
                # the page's own URL (e.g. Google's careers site); detail_base_url overrides
                # what the relative link is resolved against.
                detail_url = urljoin(cfg.get("detail_base_url", url), link.attributes["href"])
                location_node = card.css_first(cfg.get("location_selector", ".location"))
                location = normalize_text(location_node.text()) if location_node else None
                attr = cfg.get("id_attribute", "data-job-id")
                job_id = card.attributes.get(attr) or fallback_job_id(
                    self.company.company, title, location, detail_url
                )
                posted_at = None
                if cfg.get("posted_at_selector"):
                    posted_node = card.css_first(cfg["posted_at_selector"])
                    if posted_node:
                        posted_at = parse_display_date(posted_node.text())
                jobs.append(
                    JobSummary(
                        source_key=self.source_key,
                        source_platform=self.company.platform or "html",
                        company=self.company.company,
                        job_id=job_id,
                        title=title,
                        url=detail_url,
                        location_raw=location,
                        posted_at=posted_at,
                    )
                )
            next_node = tree.css_first(cfg.get("next_selector", "a[rel=next]"))
            next_href = next_node.attributes.get("href") if next_node else None
            if next_href:
                # Some sites emit a "next" href with a bare "&key=value" and no leading "?"
                # (client-side JS is expected to fix it up before navigating); requesting it
                # literally 404s or redirects to the unpaginated page, so normalize it first.
                if "?" not in next_href and "&" in next_href:
                    next_href = next_href.replace("&", "?", 1)
                url = urljoin(url, next_href)
            elif cfg.get("page_parameter") and len(cards) >= int(cfg.get("page_size", 25)):
                url = _page_url(
                    start_url,
                    cfg["page_parameter"],
                    (page + 1) * int(cfg.get("page_size", 25)),
                )
            elif cfg.get("page_number_parameter") and len(cards) >= int(cfg.get("page_size", 25)):
                # Distinct from page_parameter: some sites paginate by 1-indexed page
                # number (?page=2, ?page=3, ...) rather than a row offset.
                url = _page_url(start_url, cfg["page_number_parameter"], page + 2)
            else:
                url = None
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        return self._parse_detail_html(response.text)

    def _parse_detail_html(self, text: str) -> JobDetail:
        tree = HTMLParser(text)
        # A comma-separated selector matches every listed section (e.g. a posting split
        # across separate Summary/Description/Qualifications blocks with no single
        # wrapping container), not just the first one found in document order.
        nodes = tree.css(
            self.company.config.get(
                "description_selector", "[data-job-description], .job-description"
            )
        )
        description = "\n\n".join(normalize_text(node.text()) for node in nodes) if nodes else None
        # A schema.org JobPosting JSON-LD block (a common SEO convention, unrelated to any
        # one platform) can supply posted_at even when the configured description_selector
        # doesn't cover it — and, if description_selector found nothing, its own
        # description too.
        posting = extract_job_posting_ld(text)
        posted_at = _date(posting.get("datePosted")) if posting else None
        if not description and posting and posting.get("description"):
            description = normalize_text(html_module.unescape(posting["description"]))
        return JobDetail(
            description=description or None,
            posted_at=posted_at,
            employment_type=_stringify(posting.get("employmentType")) if posting else None,
        )


def _page_url(url: str, parameter: str, value: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[parameter] = str(value)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )
