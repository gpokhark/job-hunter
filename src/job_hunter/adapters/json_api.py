from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from ..models import JobDetail, JobSummary
from ..normalizer import fallback_job_id
from .base import JobAdapter, SchemaError, nested


class ConfigurableJsonAdapter(JobAdapter):
    """Small JSON adapter kernel; ATS subclasses supply sensible configuration defaults."""

    async def fetch_summaries(self) -> list[JobSummary]:
        cfg = self.company.config
        url = cfg.get("list_url")
        if not url:
            raise SchemaError("list_url is not configured")
        method = cfg.get("method", "GET").upper()
        response = await self.request(
            method,
            url,
            json=cfg.get("payload") if method != "GET" else None,
            params=cfg.get("params"),
        )
        payload = response.json()
        items = nested(payload, cfg.get("items_path", ""), payload)
        if not isinstance(items, list):
            raise SchemaError(f"items_path did not resolve to a list: {cfg.get('items_path', '')}")
        return self._items_to_jobs(items, url)

    def _items_to_jobs(self, items: list, url: str) -> list[JobSummary]:
        cfg = self.company.config
        jobs: list[JobSummary] = []
        fields = cfg.get("fields", {})
        public_url_template = cfg.get("public_url_template")
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(nested(item, fields.get("title", "title"), "")).strip()
            raw_url = str(nested(item, fields.get("url", "url"), "")).strip()
            location = _stringify(nested(item, fields.get("location", "location")))
            if not title or not raw_url:
                raise SchemaError("required title/url disappeared from listing response")
            api_url = urljoin(cfg.get("detail_base_url", url), raw_url)
            native_id = str(nested(item, fields.get("id", "id"), "")).strip()
            # public_url_template is opt-in: some platforms' REST detail endpoint (used to
            # fetch a description) returns raw JSON, not a page a human should be handed —
            # e.g. Ford's Oracle HCM API vs. its actual candidate-facing job page. When set,
            # it builds the *displayed* url separately; fetch_detail below still hits the
            # real API endpoint, reconstructed the same way, independent of this override.
            display_url = public_url_template.format(id=native_id) if public_url_template else api_url
            jobs.append(
                JobSummary(
                    source_key=self.source_key,
                    source_platform=self.company.platform or self.company.adapter,
                    company=self.company.company,
                    job_id=native_id
                    or fallback_job_id(self.company.company, title, location, api_url),
                    title=title,
                    url=display_url,
                    location_raw=location,
                    city=_stringify(nested(item, fields.get("city", "city"))),
                    state=_stringify(nested(item, fields.get("state", "state"))),
                    country=_stringify(nested(item, fields.get("country", "country"))),
                    department=_stringify(nested(item, fields.get("department", "department"))),
                    employment_type=_stringify(
                        nested(item, fields.get("employment_type", "employmentType"))
                    ),
                    posted_at=_date(nested(item, fields.get("posted_at", "postedAt"))),
                    raw=item,
                )
            )
        return jobs

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        cfg = self.company.config
        description_path = cfg.get("detail_description_path")
        if not description_path:
            listing_description = nested(
                summary.raw, cfg.get("listing_description_path", "description")
            )
            return JobDetail(description=_stringify(listing_description))
        api_url = summary.url
        if cfg.get("public_url_template"):
            # summary.url is the display page, not the API — rebuild the real detail
            # endpoint the same way _items_to_jobs did, from the untouched raw item.
            fields = cfg.get("fields", {})
            raw_url = str(nested(summary.raw, fields.get("url", "url"), "")).strip()
            api_url = urljoin(cfg.get("detail_base_url", summary.url), raw_url)
        response = await self.request("GET", api_url)
        payload = response.json()
        # detail_description_path may be a single dot-path (most platforms put the full
        # posting in one field) or a list of them — needed because some Oracle HCM
        # tenants (confirmed: Ford) split a posting across three separate fields
        # (ExternalDescriptionStr/ExternalResponsibilitiesStr/ExternalQualificationsStr);
        # reading only the first one silently dropped Qualifications entirely, including
        # each job's visa-sponsorship statement. Concatenating is safe even for a tenant
        # that puts everything in the first field alone (DENSO): the rest are just empty.
        paths = description_path if isinstance(description_path, list) else [description_path]
        parts = [text for path in paths if (text := _stringify(nested(payload, path)))]
        return JobDetail(description="\n\n".join(parts) or None)


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return " / ".join(filter(None, (_stringify(item) for item in value))) or None
    if isinstance(value, dict):
        return ", ".join(str(v) for v in value.values() if v is not None) or None
    return str(value).strip() or None


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.isdigit() and len(text) in (10, 13):
        # Unix epoch seconds or milliseconds (e.g. Lever's createdAt).
        try:
            return datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Date-only strings (e.g. "2026-08-26") parse as naive; treat them as UTC for
    # consistent comparison against other, tz-aware posted_at values.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
