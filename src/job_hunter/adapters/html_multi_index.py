from ..models import JobDetail, JobSummary
from ..normalizer import parse_liferay_publication_date
from .html_paginated import HtmlPaginatedAdapter


class HtmlMultiIndexAdapter(HtmlPaginatedAdapter):
    async def fetch_summaries(self):
        urls = self.company.config.get("index_urls", [])
        if not urls:
            return await super().fetch_summaries()
        jobs = []
        original = self.company.config.get("list_url")
        for url in urls:
            self.company.config["list_url"] = url
            jobs.extend(await super().fetch_summaries())
        self.company.config["list_url"] = original
        return list({job.job_id: job for job in jobs}.values())

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        detail = self._parse_detail_html(response.text)
        if detail.posted_at:
            return detail
        # Liferay DDM career sites (e.g. Honda Research Institute USA) carry no JSON-LD;
        # the page's own publish date instead lives in an inline `JobOfferData` JS-object
        # literal used to populate the application-confirmation email.
        posted_at = parse_liferay_publication_date(response.text)
        if posted_at:
            detail = detail.model_copy(update={"posted_at": posted_at})
        return detail
