from __future__ import annotations

import re

from ..models import JobSummary
from .base import SchemaError, nested
from .json_api import ConfigurableJsonAdapter

_OFFSET = re.compile(r"offset=\d+")


class OracleHcmAdapter(ConfigurableJsonAdapter):
    """Oracle Fusion HCM's recruitingCEJobRequisitions finder embeds `offset`/`limit`
    inside one semicolon-delimited query value rather than as ordinary query params, and
    silently caps `limit` well below some sites' full job count (e.g. Ford: 824 jobs,
    200/page max) — so a single request only ever returns the first page. When
    `config: {paginate: true}`, this loops by rewriting `offset=N` in place and following
    `total_path` (dot-path to the response's total-count field) until exhausted."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        if not cfg.get("paginate"):
            return await super().fetch_summaries()
        base_url = cfg.get("list_url")
        if not base_url:
            raise SchemaError("list_url is not configured")
        if not _OFFSET.search(base_url):
            raise SchemaError("paginate:true requires an 'offset=N' segment in list_url")
        items_path = cfg.get("items_path", "")
        total_path = cfg.get("total_path")
        if not total_path:
            raise SchemaError("paginate:true requires total_path")
        jobs: list[JobSummary] = []
        offset = 0
        total = None
        for _ in range(int(cfg.get("max_pages", 20))):
            url = _OFFSET.sub(f"offset={offset}", base_url)
            response = await self.request("GET", url)
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
