#!/usr/bin/env python3
"""Render a `job-hunter search` output (the default profile-driven run, or a
--keyword-scoped one) plus `data/assessments.json`'s verdicts into a single-page HTML
report — grouped and tagged exactly as the job-hunter skill's step 9 describes: Strong
matches (score >= 75) and For review (score 50-74) as two separate groups. The score
number itself carries a five-step color gradient across the whole 50-100 range (50s
through 90s+) rather than a separate text tag — Strong and For review both get it, since
the gradient is about the score's own value, not which group it landed in; below 50
stays uncolored. A [New] tag on anything posted within the last --new-days
(default 10) days. A job with no discoverable posted_at at all falls back to
first_seen_at (when job-hunter's own collector first observed it) for the same [New] tag, display-only and
using a separate window (settings.yaml's search.undated_new_days, default 15) — and gets a
"Long-standing" tag instead once past search.undated_stale_days (default 45). Never a filter:
every candidate the local model scored still appears in its normal section regardless of
either tag. A third group lists every candidate scored below 50 — every job the
local model actually evaluated appears somewhere on the page. A fourth group, "Not LLM
Reviewed", lists every candidate the review step hasn't gotten to at all (an LM Studio
error skipped it, `--limit` capped the run, or it's a job newly surfaced by a refilter that
hasn't been scored yet) — same row layout as every other section (an "NR" placeholder
sits where the score would go, uncolored, so the whole page reads as one consistent
grid rather than a visually different fallback), title/company/location/salary/date/tags,
just no score/matches/gaps since there's no verdict to show; feedback buttons still work
on these rows.

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
from datetime import datetime
from pathlib import Path
from typing import Any

from job_hunter.atomic import atomic_write_text
from job_hunter.config import load_settings
from job_hunter.rootutil import add_project_argument, chdir_to_project_root
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


def _fmt_first_seen(iso: str | None) -> str | None:
    """Display-only fallback for a job with no posted_at: "First seen {date}", using
    first_seen_at (when job-hunter's own collector first observed the job) — clearly labeled
    so it's never mistaken for the job's actual posting date. Unlike `_fmt_date` (posted_at is
    often a date-only value already normalized to UTC midnight, so converting it to local time
    would shift the displayed day backward), first_seen_at is a genuine instant and is
    converted to local time here for the same reason every other real timestamp in this
    project is at display time."""
    if not iso:
        return None
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    return f"First seen {moment.strftime('%b %-d, %Y')}"


def _undated_tags(
    posted_at: str | None, first_seen_at: str | None, *, now: datetime, new_days: int,
    undated_new_days: int, undated_stale_days: int,
) -> tuple[bool, bool]:
    """(is_new, is_long_standing) for the [New]/"Long-standing" tags. A job with a real
    posted_at only ever gets [New] (existing behavior, unchanged). A job with none falls back
    to first_seen_at as a display-only age proxy — never a filter, see config.py's
    SearchConfig.undated_new_days/undated_stale_days for why this can be wrong (a freshly
    onboarded source's jobs all look "new" regardless of true age; an evergreen undated
    listing eventually looks "long-standing" even if still genuinely open) and is only ever a
    hint for a reviewer, never something that removes a job from the report."""
    if posted_at:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        return (now - posted).days <= new_days, False
    if first_seen_at:
        first_seen = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        age_days = (now - first_seen).days
        return age_days <= undated_new_days, age_days > undated_stale_days
    return False, False


def _tier(score: int) -> str:
    """A five-step color gradient for every score from 50 up (50s/60s/70s/80s/90s+) —
    below 50 stays "plain" (muted/uncolored), matching those rows' own already-lower
    priority (state the count, don't list them individually, per the job-radar skill).
    Below 50 was deliberately not given its own sub-gradient: those postings are
    excluded from the chat-facing summary entirely, so a reader has no reason to be
    comparing shades of "not it" the way they do across the 50-100 range that's
    actually worth their attention."""
    if score >= 90:
        return "exceptional"
    if score >= 80:
        return "strong"
    if score >= 70:
        return "promising"
    if score >= 60:
        return "moderate"
    if score >= 50:
        return "fair"
    return "plain"


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


def _filter_data_attrs(*, sponsorship: str | None, arrangement: str | None, is_new: bool, is_long_standing: bool) -> str:
    """data-* attributes the client-side filter toolbar reads directly off each row — kept as
    their own explicit attributes rather than having the toolbar's JS re-derive them from
    which `.tag-*` spans happen to be present, so a future tag-rendering change can't
    silently break filtering. Deliberately doesn't include company/title: the toolbar's JS
    reads those straight from the already-rendered `.job-company`/`.job-title` text instead of
    duplicating them into attributes, which would otherwise sit earlier in the markup than the
    visible text and confuse any lookup-by-title-text (a real bug caught by this project's own
    tests, which locate a row by searching for its title)."""
    return (
        f'data-sponsorship="{_attr(sponsorship or "")}" data-arrangement="{_attr(arrangement or "")}" '
        f'data-new="{1 if is_new else 0}" data-long-standing="{1 if is_long_standing else 0}"'
    )


def _job_meta_html(location: str | None, salary_evidence: str | None) -> str:
    """Location and salary, shown directly in the always-visible summary row rather
    than only inside the click-to-expand detail — a report reader shouldn't have to
    open every single row just to see where a job is or what it pays. `·`-joined onto
    one line (not two separate tags) since both are free-text and can run long, left to
    the same single-line ellipsis truncation as job-title/job-company rather than
    wrapping, so every row keeps a consistent height. Salary gets its own accent color,
    distinct from location's muted default — a reader scanning down the list should be
    able to spot which rows even mention pay without having to read every word. Returns
    ready-to-embed HTML (each piece escaped individually), not plain text — the caller
    must not re-escape it."""
    parts = []
    if location:
        parts.append(f'<span class="job-location">{_e(location)}</span>')
    if salary_evidence:
        parts.append(f'<span class="job-salary">{_e(salary_evidence)}</span>')
    return " · ".join(parts)


def _row_html(row: dict[str, Any]) -> str:
    tier = _tier(row["score"])
    # "New" is the one signal worth interrupting the title for — it sits right before
    # the title text itself (still inside .job, so the job column's own start position
    # never moves), while the rest are secondary and sit in their own column to the
    # right of the title instead, between it and the date/feedback stack. Neither
    # placement reintroduces the original bug: a variable-width column only misaligns
    # whatever comes *after* it, and nothing variable-width sits before .job any more.
    new_badge = '<span class="tag tag-new">New</span>' if row["new"] else ""
    other_tags = ""
    if row.get("long_standing"):
        other_tags += '<span class="tag tag-long-standing">Long-standing</span>'
    other_tags += _sponsorship_tag(row.get("visa_sponsorship"))
    other_tags += _arrangement_tag(row.get("work_arrangement"))
    filter_attrs = _filter_data_attrs(
        sponsorship=row.get("visa_sponsorship"), arrangement=row.get("work_arrangement"),
        is_new=row["new"], is_long_standing=row.get("long_standing", False),
    )
    date_display = _fmt_date(row["posted_at"]) or _fmt_first_seen(row.get("first_seen_at")) or "Date unknown"
    matches_html = "".join(f"<li>{_e(m)}</li>" for m in row["matches"])
    gaps_html = "".join(f"<li>{_e(g)}</li>" for g in row["gaps"])
    sponsorship_note = (
        f'<p class="sponsor-note">Sponsorship: {_e(row["sponsorship_evidence"])}</p>'
        if row.get("sponsorship_evidence")
        else ""
    )
    meta_html = _job_meta_html(row.get("location"), row.get("salary_evidence"))
    job_meta = f'<span class="job-meta">{meta_html}</span>' if meta_html else ""
    # Always emit .tags as its own grid child, even empty — summary's grid tracks are
    # positional (auto-placement fills them in DOM order), so omitting this element
    # entirely on a row with no secondary tags would shift .row-end into .tags' own
    # track instead of its intended one, misaligning the date/feedback column exactly
    # the way the title column used to be misaligned.
    tags_col = f'<span class="tags">{other_tags}</span>'
    feedback_buttons = f'''<span class="feedback-buttons"
          data-source-key="{_attr(row["source_key"])}" data-job-id="{_attr(row["job_id"])}"
          data-company="{_attr(row["company"])}" data-title="{_attr(row["title"])}"
          data-department="{_attr(row.get("department"))}" data-score="{row["score"]}">
          <button type="button" class="fb-btn fb-relevant" data-label="relevant" title="Relevant">&#128077;</button>
          <button type="button" class="fb-btn fb-okay" data-label="okay" title="Okay">&#128994;</button>
          <button type="button" class="fb-btn fb-irrelevant" data-label="irrelevant" title="Irrelevant">&#128078;</button>
        </span>'''
    return f'''
    <details class="row tier-{tier}" {filter_attrs}>
      <summary>
        <span class="score">{row["score"]}</span>
        <span class="job">
          <span class="job-title-line">
            {new_badge}
            <span class="job-title">{_e(row["title"])}</span>
          </span>
          <span class="job-company">{_e(row["company"])}</span>
          {job_meta}
        </span>
        {tags_col}
        <span class="row-end">
          <span class="job-date">{date_display}</span>
          {feedback_buttons}
          <a class="apply-link" href="{html.escape(row["url"], quote=True)}" target="_blank" rel="noopener">View posting &#8599;</a>
        </span>
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
        {f'<div class="detail-meta">{sponsorship_note}</div>' if sponsorship_note else ""}
      </div>
    </details>'''


def _rows_html(rows: list[dict[str, Any]], *, empty_message: str) -> str:
    if not rows:
        return f'<p class="empty-state">{_e(empty_message)}</p>'
    return "".join(_row_html(row) for row in rows)


def _never_reviewed_row_html(
    candidate: dict[str, Any], *, now: datetime, new_days: int, undated_new_days: int, undated_stale_days: int
) -> str:
    """A candidate with no assessment at all — no matches/gaps to expand into, so this
    stays a plain (non-expandable) row rather than a <details> with nothing to reveal —
    but otherwise mirrors _row_html's layout exactly (an uncolored "NR" placeholder
    where the score goes, the New badge before the title, other tags in their own
    column, location/salary/date/feedback all shown the same way) so this section reads
    as the same report, not a visually different fallback. The group's own heading and
    note already say what "NR" means, so it isn't repeated on every row."""
    posted_at = candidate.get("posted_at")
    first_seen_at = candidate.get("first_seen_at")
    is_new, is_long_standing = _undated_tags(
        posted_at, first_seen_at, now=now, new_days=new_days,
        undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
    )
    new_badge = '<span class="tag tag-new">New</span>' if is_new else ""
    other_tags = ""
    if is_long_standing:
        other_tags += '<span class="tag tag-long-standing">Long-standing</span>'
    other_tags += _sponsorship_tag(candidate.get("visa_sponsorship"))
    other_tags += _arrangement_tag(candidate.get("work_arrangement"))
    filter_attrs = _filter_data_attrs(
        sponsorship=candidate.get("visa_sponsorship"), arrangement=candidate.get("work_arrangement"),
        is_new=is_new, is_long_standing=is_long_standing,
    )
    date_display = _fmt_date(posted_at) or _fmt_first_seen(first_seen_at) or "Date unknown"
    meta_html = _job_meta_html(candidate.get("location_raw"), candidate.get("salary_evidence"))
    job_meta = f'<span class="job-meta">{meta_html}</span>' if meta_html else ""
    feedback_buttons = f'''<span class="feedback-buttons"
          data-source-key="{_attr(candidate["source_key"])}" data-job-id="{_attr(candidate["job_id"])}"
          data-company="{_attr(candidate.get("company"))}" data-title="{_attr(candidate.get("title"))}"
          data-department="{_attr(candidate.get("department"))}" data-score="">
          <button type="button" class="fb-btn fb-relevant" data-label="relevant" title="Relevant">&#128077;</button>
          <button type="button" class="fb-btn fb-okay" data-label="okay" title="Okay">&#128994;</button>
          <button type="button" class="fb-btn fb-irrelevant" data-label="irrelevant" title="Irrelevant">&#128078;</button>
        </span>'''
    return f'''
    <div class="plain-row" {filter_attrs}>
      <div class="plain-row-top">
        <span class="score score-nr" title="Not yet reviewed by the local model">NR</span>
        <span class="job">
          <span class="job-title-line">
            {new_badge}
            <span class="job-title">{_e(candidate.get("title"))}</span>
          </span>
          <span class="job-company">{_e(candidate.get("company"))}</span>
          {job_meta}
        </span>
        <span class="tags">{other_tags}</span>
        <span class="row-end">
          <span class="job-date">{date_display}</span>
          {feedback_buttons}
          <a class="apply-link" href="{html.escape(candidate.get("url", ""), quote=True)}" target="_blank" rel="noopener">View posting &#8599;</a>
        </span>
      </div>
    </div>'''


def _never_reviewed_rows_html(
    candidates: list[dict[str, Any]], *, now: datetime, new_days: int, undated_new_days: int, undated_stale_days: int
) -> str:
    if not candidates:
        return '<p class="empty-state">Every candidate has been reviewed.</p>'
    return "".join(
        _never_reviewed_row_html(
            c, now=now, new_days=new_days, undated_new_days=undated_new_days, undated_stale_days=undated_stale_days
        )
        for c in candidates
    )


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
    undated_new_days: int = 15,
    undated_stale_days: int = 45,
    now: datetime | None = None,
) -> dict[str, int]:
    search = json.loads(search_path.read_text(encoding="utf-8"))
    candidates = {(c["source_key"], c["job_id"]): c for c in search["candidates"]}
    assessments = json.loads(assessments_path.read_text(encoding="utf-8"))
    assess_map = {(a["source_key"], a["job_id"]): a for a in assessments}

    # `now`, when passed explicitly (tests), is used with whatever tzinfo it already carries;
    # only the no-argument production default resolves the real system-local instant — this
    # is what the "eyebrow" date string below is formatted from, so a late-night run displays
    # the date as it was actually experienced, not tomorrow's UTC date.
    now = now or datetime.now().astimezone()
    rows: list[dict[str, Any]] = []
    never_reviewed_candidates: list[dict[str, Any]] = []
    for key, candidate in candidates.items():
        assessment = assess_map.get(key)
        if not assessment:
            never_reviewed_candidates.append(candidate)  # already has source_key/job_id
            continue
        posted_at = candidate.get("posted_at")
        first_seen_at = candidate.get("first_seen_at")
        is_new, is_long_standing = _undated_tags(
            posted_at, first_seen_at, now=now, new_days=new_days,
            undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
        )
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
                "first_seen_at": first_seen_at,
                "location": candidate.get("location_raw"),
                "new": is_new,
                "long_standing": is_long_standing,
                "matches": assessment["matches"],
                "gaps": assessment["gaps"],
                "visa_sponsorship": candidate.get("visa_sponsorship"),
                "sponsorship_evidence": candidate.get("sponsorship_evidence"),
                "salary_evidence": candidate.get("salary_evidence"),
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

    # "New" here means the same posting-recency flag as every row's own [New] tag
    # (`is_new`, computed above per scored row / via `_undated_tags` below for an
    # unreviewed one) — never `is_new`/`is_changed`'s "not previously seen by this
    # tool" meaning. Counted across every candidate regardless of review status,
    # since posting recency and sponsorship are candidate-level facts that don't
    # require a verdict — an unreviewed candidate can still be new or sponsored.
    never_reviewed_new = 0
    never_reviewed_sponsorship_new = 0
    for candidate in never_reviewed_candidates:
        is_new, _ = _undated_tags(
            candidate.get("posted_at"), candidate.get("first_seen_at"), now=now, new_days=new_days,
            undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
        )
        if is_new:
            never_reviewed_new += 1
            if candidate.get("visa_sponsorship") == "available":
                never_reviewed_sponsorship_new += 1
    strong_new = sum(1 for r in strong if r["new"])
    review_new = sum(1 for r in review if r["new"])
    below_50_new = sum(1 for r in below_50_rows if r["new"])
    total_new = strong_new + review_new + below_50_new + never_reviewed_new
    sponsorship_new = (
        sum(1 for r in rows if r["new"] and r.get("visa_sponsorship") == "available")
        + never_reviewed_sponsorship_new
    )

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
        .replace("__TOTAL_JOBS__", str(len(candidates)))
        .replace("__TOTAL_SCORED__", str(len(rows)))
        .replace("__STRONG_COUNT__", str(len(strong)))
        .replace("__REVIEW_COUNT__", str(len(review)))
        .replace("__BELOW_50__", str(below_50))
        .replace("__SOURCES__", _e(sources))
        .replace("__FAILED_COUNT__", str(failed_count))
        .replace("__SOURCE_ISSUES_COUNT__", str(len(source_issues)))
        .replace("__NEVER_REVIEWED_COUNT__", str(never_reviewed))
        .replace("__TOTAL_NEW__", str(total_new))
        .replace("__STRONG_NEW__", str(strong_new))
        .replace("__REVIEW_NEW__", str(review_new))
        .replace("__BELOW_50_NEW__", str(below_50_new))
        .replace("__SPONSORSHIP_NEW__", str(sponsorship_new))
        .replace(
            "__STRONG_ROWS__",
            _rows_html(strong, empty_message="No candidates scored 75 or above for this search."),
        )
        .replace(
            "__REVIEW_ROWS__",
            _rows_html(review, empty_message="No candidates scored 50-74 for this search."),
        )
        .replace(
            "__BELOW_50_ROWS__",
            _rows_html(below_50_rows, empty_message="No candidates scored below 50 for this search."),
        )
        .replace(
            "__NEVER_REVIEWED_ROWS__",
            _never_reviewed_rows_html(
                never_reviewed_candidates, now=now, new_days=new_days,
                undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
            ),
        )
        .replace("__SOURCE_ISSUES_ROWS__", _source_issue_rows_html(source_issues))
    )
    atomic_write_text(output_path, out)
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
    parser.add_argument(
        "--undated-new-days", type=int, default=None,
        help="for jobs with no posted_at, first-seen-age window for the [New] tag (default: settings.yaml's search.undated_new_days)",
    )
    parser.add_argument(
        "--undated-stale-days", type=int, default=None,
        help='for jobs with no posted_at, first-seen-age past which they\'re tagged "Long-standing" (default: settings.yaml\'s search.undated_stale_days)',
    )
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    args.search = resolve_search_path(search=args.search, keyword=args.keyword)

    output_path = args.output or Path("data/radar") / f"{args.search.stem}.html"
    title = args.title or _default_title(args.keyword)

    settings = load_settings()
    undated_new_days = args.undated_new_days if args.undated_new_days is not None else settings.search.undated_new_days
    undated_stale_days = (
        args.undated_stale_days if args.undated_stale_days is not None else settings.search.undated_stale_days
    )

    stats = build(
        search_path=args.search,
        assessments_path=args.assessments,
        output_path=output_path,
        title=title,
        keyword_label=args.keyword,
        new_days=args.new_days,
        undated_new_days=undated_new_days,
        undated_stale_days=undated_stale_days,
    )
    print(
        f"Wrote {output_path} | strong={stats['strong']} review={stats['review']} "
        f"below_50={stats['below_50']} never_reviewed={stats['never_reviewed']} "
        f"source_issues={stats['source_issues']} (failed={stats['failed']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
