from __future__ import annotations

import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from ..models import JobDetail, JobSummary
from .base import SchemaError, nested
from .json_api import ConfigurableJsonAdapter


class BoschAdapter(ConfigurableJsonAdapter):
    """jobs.bosch.com's own visible search page is a JS-rendered shell with no data in
    its plain HTML; the real backend is a FirstSpirit "CaaS" content API
    (bosch-i3-caas-api.e-spirit.cloud) that the page calls with a static `Bearer` API key
    — confirmed embedded in the page's own plain HTML as `window.EXTERNAL_CONFIG.jobsApi`,
    the same "public frontend key, not a session secret" shape as Ashby's posting API, not
    a credential someone had to log in for. The listing endpoint
    (`_aggrs/get_jobs?avars={...}&page=N`) paginates by a 1-indexed `page` query param
    (distinct from html_paginated's row-offset/page-number config) and reports its total
    under `_embedded.rh:result[0].meta[0].count`. The listing payload itself carries no
    description; each job's `refNumber` refeeds a second, differently-shaped request
    (`?filter={"refNumber":...}`) whose `jobAd.sections` splits the posting across four
    named HTML fields (jobDescription/qualifications/additionalInformation/
    companyDescription) — the same "split across several fields" shape Ford/DENSO's
    Oracle HCM tenant has, for an unrelated platform, handled the same way: concatenate
    all of them rather than keep only the first."""

    _DETAIL_SECTIONS = (
        "jobDescription",
        "qualifications",
        "additionalInformation",
        "companyDescription",
    )

    def _headers(self) -> dict[str, str]:
        api_key = self.company.config.get("api_key")
        if not api_key:
            raise SchemaError("api_key is not configured")
        return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        base_url = cfg.get("list_url")
        if not base_url:
            raise SchemaError("list_url is not configured")
        pagesize = int(cfg.get("pagesize", 100))
        headers = self._headers()
        jobs: list[JobSummary] = []
        total = None
        for page in range(1, int(cfg.get("max_pages", 20)) + 1):
            url = _with_query(base_url, {"pagesize": pagesize, "page": page})
            response = await self.request("GET", url, headers=headers)
            payload = response.json()
            result = nested(payload, "_embedded.rh:result.0", {})
            if total is None:
                total = int(nested(result, "meta.0.count", 0) or 0)
            items = result.get("data") if isinstance(result, dict) else None
            if not isinstance(items, list):
                raise SchemaError("get_jobs response did not carry a data list")
            if not items:
                break
            jobs.extend(self._items_to_jobs(items, url))
            if len(jobs) >= total:
                break
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        cfg = self.company.config
        ref_number = summary.raw.get("refNumber") if summary.raw else None
        if not ref_number:
            return JobDetail()
        # Built by literal concatenation, not the query-dict helper below: the site's own
        # frontend sends a bare `np` flag (no `=value`), confirmed live that `np=` (what
        # urlencode would produce) is not the same request shape.
        filter_json = quote(json.dumps({"refNumber": ref_number}))
        detail_url = f"{cfg['detail_url']}?np&rep=pj&filter={filter_json}"
        response = await self.request("GET", detail_url, headers=self._headers())
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return JobDetail()
        sections = nested(payload[0], "jobAd.sections", {}) or {}
        parts = [
            text
            for name in self._DETAIL_SECTIONS
            if (text := nested(sections, f"{name}.text"))
        ]
        return JobDetail(description="\n\n".join(parts) or None)


def _with_query(url: str, params: dict) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items()})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
