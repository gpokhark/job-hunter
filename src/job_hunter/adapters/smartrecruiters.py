from __future__ import annotations

from ..models import JobSummary
from .base import SchemaError, nested
from .json_api import ConfigurableJsonAdapter


class SmartRecruitersAdapter(ConfigurableJsonAdapter):
    """SmartRecruiters' public Job Board API (api.smartrecruiters.com/v1/companies/<id>/
    postings) — found behind a company's branded career site (e.g. careers.intuitive.com,
    itself Akamai/bot-protected, confirmed 403 on a plain request) via the same
    "front end is a skin over a public backend" pattern as GM/Stellantis/Apple. Unlike
    those, the *branded* front end doesn't even carry the real listing/detail data
    anywhere for a JSON-LD/hydration fallback to find — this is a true API discovery,
    made by testing SmartRecruiters' own documented public endpoint shape directly, not
    by rendering the front end. Confirmed zero-auth, live: returns the same job (by
    `id`, matching the URL path segment on the branded site) with matching title/location.

    Ordinary `offset`/`limit` query-param pagination, capped at 100/page server-side
    regardless of a larger requested `limit` (confirmed: requesting limit=1000 still
    returns 100) — `config: {paginate: true}` loops it, reading `totalFound` to know when
    to stop. This is a plain-query-param version of the same pagination gap
    `oracle_hcm.py` fills for Oracle's embedded-value offset; kept separate since the two
    platforms' offset mechanics don't share code cleanly (Oracle's lives inside one query
    value, this one is an ordinary param dict already supported by
    `ConfigurableJsonAdapter.fetch_summaries`, so only the looping is bespoke here).

    Each listing item's own `ref` field is already the absolute per-job API detail URL
    (`.../postings/<id>`, confirmed present in every item) — mapped via `fields.url` so
    `_items_to_jobs`/`fetch_detail` need no `detail_base_url` guessing. `ref` is a raw
    JSON endpoint, not a page a human should be handed (opening it shows a JSON dump, the
    same trap GM's Workday CXS host and Ford's Oracle REST endpoint both had) — SmartRecruiters'
    own public candidate-facing host, `jobs.smartrecruiters.com/<company>/<id>`, renders a
    real job page (confirmed 200, correct title, no slug required) and is used instead via
    `public_url_template`. The full description lives on the detail response under
    `jobAd.sections.<name>.text` (`jobDescription`/`qualifications`/`additionalInformation`;
    `companyDescription` is generic boilerplate, deliberately excluded — same reasoning as
    `prefilter.py`'s title+department-only positive-term scope, though this is the
    free-text description so it's about not diluting an LLM reviewer's read of the role,
    not a filtering-correctness issue)."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        if not cfg.get("paginate"):
            return await super().fetch_summaries()
        url = cfg.get("list_url")
        if not url:
            raise SchemaError("list_url is not configured")
        items_path = cfg.get("items_path", "content")
        total_path = cfg.get("total_path", "totalFound")
        page_size = int(cfg.get("page_size", 100))
        jobs: list[JobSummary] = []
        offset = 0
        total = None
        for _ in range(int(cfg.get("max_pages", 50))):
            params = {**(cfg.get("params") or {}), "offset": offset, "limit": page_size}
            response = await self.request("GET", url, params=params)
            payload = response.json()
            items = nested(payload, items_path, payload)
            if not isinstance(items, list):
                raise SchemaError(f"items_path did not resolve to a list: {items_path}")
            if total is None:
                total = int(nested(payload, total_path, 0) or 0)
            if not items:
                break
            jobs.extend(self._items_to_jobs(items, url))
            offset += len(items)
            if offset >= total:
                break
        return jobs
