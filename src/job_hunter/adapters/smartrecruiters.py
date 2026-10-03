from __future__ import annotations

import re

from ..models import JobSummary
from .base import SchemaError, nested
from .json_api import ConfigurableJsonAdapter

_PAY_LABEL = re.compile(r"^(min|max)\.?\s+salary\s+(.+)$", re.I)
_PAY_VALUE = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s+([A-Z]{3})\s*$")


def pay_text_from_custom_fields(payload: dict) -> str | None:
    """Intuitive posts pay only as structured `customField` entries ("Min. Salary Region 1":
    "170500 USD", "Max. Salary Region 1": ...), never in the description, so a text mine of
    the description finds nothing. Turns each region's min/max pair into a plain sentence
    the shared `salary.evaluate_salary` reads like any other posting. Which region applies
    to which office isn't stated, so when there are several the leading range spans all of
    them and each region is listed after it; a lone region is just its own range. Only USD
    pairs with min <= max are used, never a guess."""
    regions: dict[str, dict[str, float]] = {}
    for field in payload.get("customField") or []:
        label = _PAY_LABEL.match(str(field.get("fieldLabel") or "").strip())
        value = _PAY_VALUE.match(str(field.get("valueLabel") or ""))
        if not label or not value or value.group(2) != "USD":
            continue
        regions.setdefault(label.group(2).strip(), {})[label.group(1).lower()] = float(
            value.group(1).replace(",", "")
        )
    pairs = {r: v for r, v in regions.items() if "min" in v and "max" in v and v["min"] <= v["max"]}
    if not pairs:
        return None

    def money(low: float, high: float) -> str:
        return f"${low:,.0f} - ${high:,.0f} USD"

    if len(pairs) == 1:
        (low, high), = [(v["min"], v["max"]) for v in pairs.values()]
        return f"Pay range: {money(low, high)}."
    span = money(min(v["min"] for v in pairs.values()), max(v["max"] for v in pairs.values()))
    per_region = "; ".join(f"{r}: {money(v['min'], v['max'])}" for r, v in sorted(pairs.items()))
    return f"Pay range across pay regions: {span} ({per_region})."


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
        jobs = self.begin_listing()
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

    def augment_description(self, payload: dict, description: str | None) -> str | None:
        pay = pay_text_from_custom_fields(payload)
        if not pay:
            return description
        return f"{description}\n\n{pay}" if description else pay
