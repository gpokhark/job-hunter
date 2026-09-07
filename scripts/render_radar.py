#!/usr/bin/env python3
"""Render a `job-hunter search` output (the default profile-driven run, or a
--keyword-scoped one) plus `data/assessments.json`'s verdicts into a single-page HTML
report — grouped and tagged exactly as the job-hunter skill's step 9 describes: Strong
matches (score >= 75) and For review (score 50-74) as two separate groups, [90+]/[80+]
tags within Strong, and a [New] tag on anything posted within the last --new-days
(default 10) days. A third group lists every candidate scored below 50 — every job the
local model actually evaluated appears somewhere on the page. A fourth group, "Not LLM
Reviewed", lists every candidate the review step hasn't gotten to at all (an LM Studio
error skipped it, `--limit` capped the run, or it's a job newly surfaced by a refilter that
hasn't been scored yet) — title/company/link/date/tags only, no score/matches/gaps since
there's no verdict to show; feedback buttons still work on these rows.

This is pure presentation: it never re-derives, adjusts, or overrides a score — every
number here is exactly what's already in data/assessments.json.

Usage:
    uv run python scripts/render_radar.py                              # newest archive, any keyword
    uv run python scripts/render_radar.py --keyword "product manager"  # newest archive for that keyword
    uv run python scripts/render_radar.py --search data/searches/product-manager_2026-08-31.json --keyword "product manager"
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_hunter.search_archive import resolve_search_path

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "radar_template.html"


def _e(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _attr(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _fmt_date(iso: str | None) -> str | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %Y")


def _tier(score: int) -> str:
    if score >= 90:
        return "exceptional"
    if score >= 80:
        return "strong"
    return "plain"


def _tier_tag(score: int) -> str:
    if score >= 90:
        return '<span class="tag tag-exceptional">90+</span>'
    if score >= 80:
        return '<span class="tag tag-strong">80+</span>'
    return ""


def _sponsorship_tag(status: str | None) -> str:
    if status == "not_available":
        return '<span class="tag tag-sponsor-no">No Sponsorship</span>'
    if status == "available":
        return '<span class="tag tag-sponsor-yes">Sponsorship OK</span>'
    return ""  # "unmentioned" (or missing, for an older archive) carries no tag — a
    # blanket "Not Stated" label on the majority of rows was tried and found to add no
    # value; only the two explicit, actionable states are worth a tag.


def _arrangement_tag(arrangement: str | None) -> str:
    if arrangement == "remote":
        return '<span class="tag tag-remote">Remote</span>'
    if arrangement == "hybrid":
        return '<span class="tag tag-hybrid">Hybrid</span>'
    return ""  # "onsite"/"unknown" carry no tag, same reasoning as sponsorship's
    # "unmentioned" — onsite is the unremarkable default and unknown says nothing.


def _row_html(row: dict[str, Any], *, show_tier_tag: bool) -> str:
    tier = _tier(row["score"]) if show_tier_tag else "plain"
    tags = _tier_tag(row["score"]) if show_tier_tag else ""
    if row["new"]:
        tags += '<span class="tag tag-new">New</span>'
    tags += _sponsorship_tag(row.get("visa_sponsorship"))
    tags += _arrangement_tag(row.get("work_arrangement"))
    date_display = _fmt_date(row["posted_at"]) or "Date unknown"
    matches_html = "".join(f"<li>{_e(m)}</li>" for m in row["matches"])
    gaps_html = "".join(f"<li>{_e(g)}</li>" for g in row["gaps"])
    sponsorship_note = (
        f'<p class="sponsor-note">Sponsorship: {_e(row["sponsorship_evidence"])}</p>'
        if row.get("sponsorship_evidence")
        else ""
    )
    feedback_buttons = f'''<span class="feedback-buttons"
          data-source-key="{_attr(row["source_key"])}" data-job-id="{_attr(row["job_id"])}"
          data-company="{_attr(row["company"])}" data-title="{_attr(row["title"])}"
          data-department="{_attr(row.get("department"))}" data-score="{row["score"]}">
          <button type="button" class="fb-btn fb-relevant" data-label="relevant" title="Relevant">&#128077;</button>
          <button type="button" class="fb-btn fb-okay" data-label="okay" title="Okay">&#128994;</button>
          <button type="button" class="fb-btn fb-irrelevant" data-label="irrelevant" title="Irrelevant">&#128078;</button>
        </span>'''
    return f'''
    <details class="row tier-{tier}">
      <summary>
        <span class="score">{row["score"]}</span>
        <span class="tags">{tags}</span>
        <span class="job">
          <span class="job-title">{_e(row["title"])}</span>
          <span class="job-company">{_e(row["company"])}</span>
        </span>
        <span class="job-date">{date_display}</span>
        {feedback_buttons}
      </summary>
      <div class="row-detail">
        <div class="detail-col">
          <h3>Matches</h3>
          <ul>{matches_html}</ul>
        </div>
        <div class="detail-col">
          <h3>Gaps</h3>
          <ul>{gaps_html}</ul>
        </div>
        <div class="detail-meta">
          <div>
            <p class="loc">{_e(row.get("location"))}</p>
            {sponsorship_note}
          </div>
          <a class="apply-link" href="{html.escape(row["url"], quote=True)}" target="_blank" rel="noopener">View posting &#8599;</a>
        </div>
      </div>
    </details>'''


def _rows_html(rows: list[dict[str, Any]], *, show_tier_tag: bool, empty_message: str) -> str:
    if not rows:
        return f'<p class="empty-state">{_e(empty_message)}</p>'
    return "".join(_row_html(row, show_tier_tag=show_tier_tag) for row in rows)


def _never_reviewed_row_html(candidate: dict[str, Any], *, now: datetime, new_days: int) -> str:
    """A candidate with no assessment at all — no score, so no <details>/matches/gaps/tier
    tag, just the same at-a-glance signal (link/date/[New]/sponsorship/arrangement tags) every
    other report already shows, plus feedback buttons so it can still be tagged before review."""
    posted_at = candidate.get("posted_at")
    is_new = False
    if posted_at:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        is_new = (now - posted).days <= new_days
    tags = '<span class="tag tag-new">New</span>' if is_new else ""
    tags += _sponsorship_tag(candidate.get("visa_sponsorship"))
    tags += _arrangement_tag(candidate.get("work_arrangement"))
    date_display = _fmt_date(posted_at) or "Date unknown"
    feedback_buttons = f'''<span class="feedback-buttons"
          data-source-key="{_attr(candidate["source_key"])}" data-job-id="{_attr(candidate["job_id"])}"
          data-company="{_attr(candidate.get("company"))}" data-title="{_attr(candidate.get("title"))}"
          data-department="{_attr(candidate.get("department"))}" data-score="">
          <button type="button" class="fb-btn fb-relevant" data-label="relevant" title="Relevant">&#128077;</button>
          <button type="button" class="fb-btn fb-okay" data-label="okay" title="Okay">&#128994;</button>
          <button type="button" class="fb-btn fb-irrelevant" data-label="irrelevant" title="Irrelevant">&#128078;</button>
        </span>'''
    return f'''
    <div class="plain-row">
      <div class="plain-row-top">
        <span class="tags">{tags}</span>
        <span class="job">
          <span class="job-title">{_e(candidate.get("title"))}</span>
          <span class="job-company">{_e(candidate.get("company"))}</span>
        </span>
        <span class="job-date">{date_display}</span>
        <a class="apply-link" href="{html.escape(candidate.get("url", ""), quote=True)}" target="_blank" rel="noopener">View posting &#8599;</a>
      </div>
      <div class="row-meta">Not yet reviewed by the local model.</div>
      {feedback_buttons}
    </div>'''


def _never_reviewed_rows_html(candidates: list[dict[str, Any]], *, now: datetime, new_days: int) -> str:
    if not candidates:
        return '<p class="empty-state">Every candidate has been reviewed.</p>'
    return "".join(_never_reviewed_row_html(c, now=now, new_days=new_days) for c in candidates)


# Ordering/label/CSS-class for each non-OK SourceHealth status this run's collector.py can
# actually emit (see models.py's HealthStatus) — failed first (this run's own transient
# problem, the most actionable), then warning (collected, but health.py's count-anomaly
# check flagged a suspicious drop), then unsupported last (a permanent, already-disclosed
# config.py state — config_reason lives in companies.yaml, not something new this run).
_SOURCE_ISSUE_ORDER = {"failed": 0, "warning": 1, "unsupported": 2}
_SOURCE_ISSUE_LABEL = {"failed": "Failed", "warning": "Warning", "unsupported": "Unsupported"}


def _source_issue_row_html(health: dict[str, Any]) -> str:
    status = health.get("status", "failed")
    label = _SOURCE_ISSUE_LABEL.get(status, status.title())
    message = health.get("message") or "No error message recorded."
    return f'''
    <div class="source-issue source-issue-{_attr(status)}">
      <span class="tag tag-source-{_attr(status)}">{_e(label)}</span>
      <span class="source-issue-company">{_e(health.get("company") or health.get("source_key"))}</span>
      <span class="source-issue-message">{_e(message)}</span>
    </div>'''


def _source_issue_rows_html(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return '<p class="empty-state">Every attempted source collected successfully this run.</p>'
    return "".join(_source_issue_row_html(h) for h in entries)


def build(
    *,
    search_path: Path,
    assessments_path: Path,
    output_path: Path,
    title: str,
    keyword_label: str | None,
    new_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    search = json.loads(search_path.read_text(encoding="utf-8"))
    candidates = {(c["source_key"], c["job_id"]): c for c in search["candidates"]}
    assessments = json.loads(assessments_path.read_text(encoding="utf-8"))
    assess_map = {(a["source_key"], a["job_id"]): a for a in assessments}

    now = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    never_reviewed_candidates: list[dict[str, Any]] = []
    for key, candidate in candidates.items():
        assessment = assess_map.get(key)
        if not assessment:
            never_reviewed_candidates.append(candidate)  # already has source_key/job_id
            continue
        posted_at = candidate.get("posted_at")
        is_new = False
        if posted_at:
            posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
            is_new = (now - posted).days <= new_days
        rows.append(
            {
                # Judgment fields (score/recommended/matches/gaps) come from the
                # assessment — that's its whole purpose, and its validity is exactly
                # what content_hash matching already guarantees. Everything else here
                # is factual/descriptive metadata about the listing, not a judgment, so
                # it must come from the *current* candidate record, not the assessment's
                # snapshot frozen at whatever moment it was reviewed — otherwise an
                # adapter fix (e.g. a corrected URL) silently doesn't show up in any
                # report until the job's description happens to change and forces a
                # fresh review. Confirmed as a real bug: Ford's URL fix didn't appear
                # here because these jobs' cached assessments predated it.
                "source_key": key[0],
                "job_id": key[1],
                "department": candidate.get("department"),
                "score": assessment["score"],
                "company": candidate.get("company", assessment["company"]),
                "title": candidate.get("title", assessment["title"]),
                "url": candidate.get("url", assessment["url"]),
                "posted_at": posted_at,
                "location": candidate.get("location_raw"),
                "new": is_new,
                "matches": assessment["matches"],
                "gaps": assessment["gaps"],
                "visa_sponsorship": candidate.get("visa_sponsorship"),
                "sponsorship_evidence": candidate.get("sponsorship_evidence"),
                "work_arrangement": candidate.get("work_arrangement"),
            }
        )

    strong = sorted((r for r in rows if r["score"] >= 75), key=lambda r: -r["score"])
    review = sorted((r for r in rows if 50 <= r["score"] < 75), key=lambda r: -r["score"])
    below_50_rows = sorted((r for r in rows if r["score"] < 50), key=lambda r: -r["score"])
    below_50 = len(below_50_rows)
    never_reviewed_candidates.sort(
        key=lambda c: c.get("posted_at") or "", reverse=True
    )
    never_reviewed = len(never_reviewed_candidates)

    source_issues = sorted(
        (h for h in search.get("source_health", []) if h.get("status") != "ok"),
        key=lambda h: (_SOURCE_ISSUE_ORDER.get(h.get("status"), 99), h.get("company") or h.get("source_key") or ""),
    )
    failed_count = sum(1 for h in source_issues if h.get("status") == "failed")

    summary = search.get("summary", {})
    sources = f"{summary.get('sources_succeeded', '?')}/{summary.get('sources_attempted', '?')}"
    date_str = now.strftime("%Y-%m-%d")
    eyebrow = (
        f'Job-Hunter Keyword Search · “{keyword_label}” · {date_str}'
        if keyword_label
        else f"Job-Hunter Search Run · {date_str}"
    )
    scope = f'matching “{keyword_label}” in title/department' if keyword_label else "matching your candidate profile"
    subhead = (
        f"{len(rows)} U.S.-eligible postings {scope}, scored one at a time by a local model "
        "against the resume on file. No cap, nothing discarded before review."
    )

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    out = (
        template.replace("__TITLE__", _e(title))
        .replace("__SEARCH_STEM__", _e(search_path.stem))
        .replace("__H1__", _e(title))
        .replace("__EYEBROW__", _e(eyebrow))
        .replace("__SUBHEAD__", _e(subhead))
        .replace("__TOTAL_SCORED__", str(len(rows)))
        .replace("__STRONG_COUNT__", str(len(strong)))
        .replace("__REVIEW_COUNT__", str(len(review)))
        .replace("__BELOW_50__", str(below_50))
        .replace("__SOURCES__", _e(sources))
        .replace("__FAILED_COUNT__", str(failed_count))
        .replace("__SOURCE_ISSUES_COUNT__", str(len(source_issues)))
        .replace("__NEVER_REVIEWED_COUNT__", str(never_reviewed))
        .replace(
            "__STRONG_ROWS__",
            _rows_html(strong, show_tier_tag=True, empty_message="No candidates scored 75 or above for this search."),
        )
        .replace(
            "__REVIEW_ROWS__",
            _rows_html(review, show_tier_tag=False, empty_message="No candidates scored 50-74 for this search."),
        )
        .replace(
            "__BELOW_50_ROWS__",
            _rows_html(
                below_50_rows, show_tier_tag=False, empty_message="No candidates scored below 50 for this search."
            ),
        )
        .replace(
            "__NEVER_REVIEWED_ROWS__",
            _never_reviewed_rows_html(never_reviewed_candidates, now=now, new_days=new_days),
        )
        .replace("__SOURCE_ISSUES_ROWS__", _source_issue_rows_html(source_issues))
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(out, encoding="utf-8")
    return {
        "strong": len(strong),
        "review": len(review),
        "below_50": below_50,
        "never_reviewed": never_reviewed,
        "source_issues": len(source_issues),
        "failed": failed_count,
    }


def _default_title(keyword: str | None) -> str:
    if not keyword:
        return "Candidate Radar"
    parts = []
    for part in keyword.split(","):
        part = part.strip()
        if not part:
            continue
        # Preserve an already-uppercase acronym (e.g. "ADAS") as-is instead of
        # .title()-casing it into something like "Adas".
        parts.append(part if part.isupper() else part.title())
    return f"{' & '.join(parts)} Radar"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--search", type=Path, default=None,
        help=(
            "path to a job-hunter search --json/--archive output; if omitted, resolved via "
            "--keyword (newest archive for that keyword's slug) or, with neither given, the "
            "newest archive overall — see docs/skill-split-plan.md section 4"
        ),
    )
    parser.add_argument(
        "--assessments", type=Path, default=Path("data/assessments.json"), help="assessments JSON to read"
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="HTML path (default: data/radar/<search filename>.html)",
    )
    parser.add_argument("--title", default=None, help="page title (default derived from --keyword)")
    parser.add_argument(
        "--keyword", default=None,
        help="the --keyword string used for this search, if any (drives the subhead/eyebrow/default title; omit for a default profile-driven search)",
    )
    parser.add_argument("--new-days", type=int, default=10, help="posting-age window for the [New] tag (default 10)")
    args = parser.parse_args()
    args.search = resolve_search_path(search=args.search, keyword=args.keyword)

    output_path = args.output or Path("data/radar") / f"{args.search.stem}.html"
    title = args.title or _default_title(args.keyword)

    stats = build(
        search_path=args.search,
        assessments_path=args.assessments,
        output_path=output_path,
        title=title,
        keyword_label=args.keyword,
        new_days=args.new_days,
    )
    print(
        f"Wrote {output_path} | strong={stats['strong']} review={stats['review']} "
        f"below_50={stats['below_50']} never_reviewed={stats['never_reviewed']} "
        f"source_issues={stats['source_issues']} (failed={stats['failed']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
