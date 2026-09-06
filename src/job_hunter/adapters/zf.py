from __future__ import annotations

import re
from datetime import UTC, datetime

from ..models import JobDetail, JobSummary
from .html_paginated import HtmlPaginatedAdapter

_DATE_POSTED = re.compile(r'itemprop="datePosted"\s+content="([^"]+)"')


def _parse_java_date(text: str | None) -> datetime | None:
    """ZF's detail page reports its schema.org JobPosting data as inline microdata
    (<meta itemprop="datePosted" content="Sat Sep 05 02:00:00 UTC 2026">), not the
    <script type="application/ld+json"> block html_paginated's built-in fallback looks
    for (confirmed: zero ld+json blocks anywhere on the page), and in Java's default
    Date.toString() format, which no existing normalizer.py parser covers."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%a %b %d %H:%M:%S %Z %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


class ZfAdapter(HtmlPaginatedAdapter):
    """jobs.zf.com's listing page (/search/) is the classic server-rendered
    SuccessFactors RMK ("Job2Web") tr.data-row table — identical shape to the existing
    plain successfactors_rmk config (PACCAR/Volkswagen/Valeo), reused here via plain
    html_paginated config. Its detail page, though, uses the *other* RMK template
    (successfactors_rmk_v2.py/BMW's .joblayouttoken/.rtltextaligneligible value-class
    reuse) — but unlike BMW, ZF's description lives in its own dedicated
    `.jobdescription` span (the same selector PACCAR/Volkswagen already use), so plain
    CSS description_selector matching handles it with zero bespoke parsing. The one
    real gap html_paginated's inherited logic can't fill is posted_at: the listing
    table has no date column at all, and the detail page's JobPosting data is
    schema.org microdata (a <meta itemprop="datePosted"> tag), not the
    <script type="application/ld+json"> block html_paginated's own fallback looks for
    — this override adds exactly that one piece back."""

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        detail = self._parse_detail_html(response.text)
        match = _DATE_POSTED.search(response.text)
        posted_at = _parse_java_date(match.group(1)) if match else None
        return detail.model_copy(update={"posted_at": posted_at}) if posted_at else detail
