from __future__ import annotations

import json

from ..models import JobDetail, JobSummary
from ..normalizer import normalize_text, parse_flexible_date, stringify
from .base import JobAdapter, SchemaError

_DETAIL_MARKER = "CandidateOpportunityDetail("


def _search_body(top: int, skip: int) -> dict:
    return {
        "opportunitySearch": {
            "Top": top,
            "Skip": skip,
            "QueryString": "",
            "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
            "Filters": [],
        },
        "matchCriteria": {"PreferredJobs": [], "Criteria": [], "SearchCriteria": [], "MatchedJobs": []},
    }


def _location_label(location: dict) -> str | None:
    """Prefer the structured "City, ST" over `LocalizedName`: a tenant can name a site
    something that isn't a place at all (Blue Bird: "Blue Bird South", whose address is
    Fort Valley, GA), while "Remote" has no city/state and only `LocalizedName` says so."""
    address = location.get("Address") or {}
    city = stringify(address.get("City"))
    state = address.get("State")
    state_code = stringify(state.get("Code") if isinstance(state, dict) else state)
    if city and state_code:
        return f"{city}, {state_code}"
    return stringify(location.get("LocalizedName")) or city or state_code


class UltiProAdapter(JobAdapter):
    """UKG Pro Recruiting (formerly UltiPro), a shared ATS hosting many employers at
    `recruiting.ultipro.com/<tenant>/JobBoard/<board-guid>` — configured per company via
    `board_url` (that prefix, no trailing slash). The visible job board is a client-side
    widget, but it just calls a public, unauthenticated `POST <board>/JobBoardView/
    LoadSearchResults` (no cookie/token; confirmed with a bare httpx POST) taking
    `opportunitySearch.Top`/`Skip`. Both are real: Top=10/Skip=10 returns a different page
    than Skip=0, Skip past the end returns an empty list, and `totalCount` carries the
    full count. The list is sorted by `PostedDate` when `OrderBy` says so (verified
    monotonic on one tenant), but no early-stop is used — the catalogs are small.

    The listing carries only a short `BriefDescription`. The full description lives on
    the human-facing detail page (`<board>/OpportunityDetail?opportunityId=<id>`) inside an
    inline `new US.Opportunity.CandidateOpportunityDetail({...})` JS call whose argument is
    plain JSON — no separate detail API was found or needed. That page is also the URL a
    person can open, so it doubles as `url`. Never overrides `posted_at` from the detail
    (it agrees with the listing to the millisecond, but the listing already has it)."""

    def _board_url(self) -> str:
        board = self.company.config.get("board_url")
        if not board:
            raise SchemaError("board_url is not configured")
        return str(board).rstrip("/")

    async def fetch_summaries(self) -> list[JobSummary]:
        board = self._board_url()
        page_size = int(self.company.config.get("page_size", 100))
        endpoint = f"{board}/JobBoardView/LoadSearchResults"
        summaries: list[JobSummary] = []
        skip = 0
        total: int | None = None
        for _ in range(int(self.company.config.get("max_pages", 50))):
            response = await self.request("POST", endpoint, json=_search_body(page_size, skip))
            payload = response.json()
            items = payload.get("opportunities")
            if not isinstance(items, list):
                raise SchemaError("UltiPro search response missing 'opportunities' list")
            if total is None:
                total = int(payload.get("totalCount") or 0)
            if not items:
                break
            for item in items:
                summaries.append(self._to_summary(board, item))
            skip += len(items)
            if skip >= total:
                break
        return summaries

    def _to_summary(self, board: str, item: dict) -> JobSummary:
        job_id = stringify(item.get("Id"))
        title = normalize_text(item.get("Title"))
        if not job_id or not title:
            raise SchemaError("required Id/Title missing from an UltiPro listing entry")
        locations = [loc for loc in (item.get("Locations") or []) if isinstance(loc, dict)]
        labels: list[str] = []
        for loc in locations:
            label = _location_label(loc)
            if label and label not in labels:
                labels.append(label)
        first_address = (locations[0].get("Address") or {}) if locations else {}
        state = first_address.get("State")
        country = first_address.get("Country")
        return JobSummary(
            source_key=self.source_key,
            source_platform=self.company.platform or "ultipro",
            company=self.company.company,
            job_id=job_id,
            title=title,
            url=f"{board}/OpportunityDetail?opportunityId={job_id}",
            location_raw="; ".join(labels) or None,
            city=stringify(first_address.get("City")) if len(labels) == 1 else None,
            state=(stringify(state.get("Code") if isinstance(state, dict) else state))
            if len(labels) == 1
            else None,
            country=stringify(country.get("Code") if isinstance(country, dict) else country),
            department=stringify(item.get("JobCategoryName")),
            employment_type=(
                "Full-time" if item.get("FullTime") is True else "Part-time" if item.get("FullTime") is False else None
            ),
            posted_at=parse_flexible_date(item.get("PostedDate")),
            raw=item,
        )

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        response = await self.request("GET", summary.url)
        text = response.text
        start = text.find(_DETAIL_MARKER)
        if start < 0:
            raise SchemaError("UltiPro detail page missing CandidateOpportunityDetail data")
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start + len(_DETAIL_MARKER) :])
        except json.JSONDecodeError as exc:
            raise SchemaError("UltiPro CandidateOpportunityDetail is not valid JSON") from exc
        description = data.get("Description") if isinstance(data, dict) else None
        return JobDetail(description=description or None)
