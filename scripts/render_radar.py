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

One deliberate, disclosed exception to "pure presentation": this module also implements the
stale-source-collection fallback (`docs/pipeline-refilter-stale-source-plan.md` section 4.3) — a
source whose live collection genuinely `failed` this run (not `warning`, which already produced
real live data this run just fewer jobs than expected, and not `unsupported`, which never has
cached data to fall back to) still has its last-known-good jobs sitting in SQLite untouched by
that failure. `build()` merges that source's current active/US-eligible/prefilter-passing/
recency-passing jobs (via `job_hunter.active_pool.source_jobs()`, deduped against whatever's
already in `candidates`) straight into the same candidate pool everything else in this module
already renders, and extends that source's Collection Issues row with a note naming how many
jobs came from the fallback and when they were last actually collected. This is why `build()`
now takes a `database_path` for the first time — the one new dependency this module didn't have
before — but it still never touches a *score*: a merged job is scored (or shown as "NR") exactly
like any other candidate, using whatever's already in `data/assessments.json`, and the archive
file on disk is never rewritten by this — only the rendered HTML changes, so re-running this
script against the same archive stays idempotent. `--no-collection-fallback` (default: fallback
on) disables this and restores today's plain "no jobs, just the note" behavior, mirroring the
escape-hatch style of `cleanup.py`'s `--no-vacuum`/`--no-export` flags.

Usage:
    uv run python scripts/render_radar.py                              # newest archive, any keyword
    uv run python scripts/render_radar.py --keyword "product manager"  # newest archive for that keyword
    uv run python scripts/render_radar.py --search data/searches/product-manager_2026-08-31.json --keyword "product manager"
"""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from job_hunter.active_pool import source_jobs as _pool_source_jobs
from job_hunter.atomic import atomic_write_text
from job_hunter.config import CandidateProfile, load_profile, load_settings
from job_hunter.rootutil import add_project_argument, chdir_to_project_root, nonneg_int
from job_hunter.search_archive import resolve_search_path
from job_hunter.storage import Storage

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "radar_template.html"
_TEMPLATE_DIR = _TEMPLATE_PATH.parent
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class LiveState:
    """Everything a live render needs that isn't in the archive/assessments: the current
    feedback rows (keyed "source_key|job_id"), the state versions the client polls against, and
    the archive filename shown in the footer. Passed in explicitly — the renderer never opens
    its own DB connection."""

    feedback: dict[str, dict[str, Any]]
    versions: dict[str, str | None]
    archive_name: str


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


def _fmt_local_date(iso: str | None) -> str | None:
    """Same "Sep 10, 2026"-style day formatting as `_fmt_first_seen`, for
    `source_health.last_success_at` — a genuine instant (not a date-only value the way
    `posted_at` often is), so converting it to the device's local timezone before display is
    correct here for the same reason it's correct for first_seen_at."""
    if not iso:
        return None
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    return moment.strftime("%b %-d, %Y")


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


def _live_row_attrs(
    *, source_key: str, job_id: str, score: int | None, posted_at: str | None,
    state: str | None, country: str | None, salary_evidence: str | None,
) -> str:
    """Identity + filter attributes for a live row root. Deliberately no company/title (see
    `_filter_data_attrs`'s docstring); the live script reads those from the visible text."""
    posted = (posted_at or "")[:10]
    if not _ISO_DAY.fullmatch(posted):
        posted = ""
    return (
        f' data-source-key="{_attr(source_key)}" data-job-id="{_attr(job_id)}"'
        f' data-score="{"" if score is None else score}" data-posted="{posted}"'
        f' data-state="{_attr(state)}" data-country="{_attr(country)}"'
        f' data-has-salary="{1 if salary_evidence else 0}"'
    )


def _feedback_buttons_html(
    *, source_key: str, job_id: str, company: str | None, title: str | None,
    department: str | None, score_attr: str, label: str | None = None,
) -> str:
    """The three buttons. With `label=None` (static mode, and any unlabelled live row)
    this is byte-for-byte the markup the two row builders used to inline."""

    def active(name: str) -> str:
        return " fb-active" if label == name else ""

    return f'''<span class="feedback-buttons"
          data-source-key="{_attr(source_key)}" data-job-id="{_attr(job_id)}"
          data-company="{_attr(company)}" data-title="{_attr(title)}"
          data-department="{_attr(department)}" data-score="{score_attr}">
          <button type="button" class="fb-btn fb-relevant{active("relevant")}" data-label="relevant" title="Relevant">&#128077;</button>
          <button type="button" class="fb-btn fb-okay{active("okay")}" data-label="okay" title="Okay">&#128994;</button>
          <button type="button" class="fb-btn fb-irrelevant{active("irrelevant")}" data-label="irrelevant" title="Irrelevant">&#128078;</button>
        </span>'''


def _live_label(feedback: dict[str, dict[str, Any]] | None, source_key: str, job_id: str) -> str | None:
    if not feedback:
        return None
    entry = feedback.get(f"{source_key}|{job_id}")
    return entry["label"] if entry else None


def _row_html(row: dict[str, Any], *, live: bool = False, feedback: dict[str, dict[str, Any]] | None = None) -> str:
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
    label = _live_label(feedback, row["source_key"], row["job_id"]) if live else None
    feedback_buttons = _feedback_buttons_html(
        source_key=row["source_key"], job_id=row["job_id"], company=row["company"],
        title=row["title"], department=row.get("department"), score_attr=str(row["score"]),
        label=label,
    )
    live_attrs = (
        _live_row_attrs(
            source_key=row["source_key"], job_id=row["job_id"], score=row["score"],
            posted_at=row["posted_at"], state=row.get("state"), country=row.get("country"),
            salary_evidence=row.get("salary_evidence"),
        )
        if live
        else ""
    )
    tagged = " fb-tagged" if label else ""
    return f'''
    <details class="row tier-{tier}{tagged}" {filter_attrs}{live_attrs}>
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


def _rows_html(
    rows: list[dict[str, Any]], *, empty_message: str, live: bool = False,
    feedback: dict[str, dict[str, Any]] | None = None,
) -> str:
    if not rows:
        return f'<p class="empty-state">{_e(empty_message)}</p>'
    return "".join(_row_html(row, live=live, feedback=feedback) for row in rows)


def _never_reviewed_row_html(
    candidate: dict[str, Any], *, now: datetime, new_days: int, undated_new_days: int, undated_stale_days: int,
    live: bool = False, feedback: dict[str, dict[str, Any]] | None = None,
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
    label = _live_label(feedback, candidate["source_key"], candidate["job_id"]) if live else None
    feedback_buttons = _feedback_buttons_html(
        source_key=candidate["source_key"], job_id=candidate["job_id"], company=candidate.get("company"),
        title=candidate.get("title"), department=candidate.get("department"), score_attr="",
        label=label,
    )
    live_attrs = (
        _live_row_attrs(
            source_key=candidate["source_key"], job_id=candidate["job_id"], score=None,
            posted_at=posted_at, state=candidate.get("state"), country=candidate.get("country"),
            salary_evidence=candidate.get("salary_evidence"),
        )
        if live
        else ""
    )
    tagged = " fb-tagged" if label else ""
    return f'''
    <div class="plain-row{tagged}" {filter_attrs}{live_attrs}>
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
    candidates: list[dict[str, Any]], *, now: datetime, new_days: int, undated_new_days: int, undated_stale_days: int,
    live: bool = False, feedback: dict[str, dict[str, Any]] | None = None,
) -> str:
    if not candidates:
        return '<p class="empty-state">Every candidate has been reviewed.</p>'
    return "".join(
        _never_reviewed_row_html(
            c, now=now, new_days=new_days, undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
            live=live, feedback=feedback,
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


def _apply_collection_fallback(
    *,
    source_issues: list[dict[str, Any]],
    candidates: dict[tuple[str, str], dict[str, Any]],
    database_path: Path,
    profile: CandidateProfile,
    max_age_days: int,
    keywords: list[str] | None,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ability 3 from docs/pipeline-refilter-stale-source-plan.md section 4.3: a source whose
    live collection genuinely `failed` this run still has its last-known-good jobs sitting in
    SQLite, completely untouched by that failure — this is what actually surfaces them in the
    rendered report (with a note explaining why they're there) instead of a `failed` source
    silently showing zero jobs even though real, recently-collected data for it exists on disk.

    Deliberately scoped to `status == "failed"` only, never `warning` or `unsupported` — see the
    plan's section 4.3 for the reasoning: a `warning` source already produced real live data this
    run (health.py's count-anomaly check just flagged the count as suspiciously low), so mixing
    in old jobs on top of a partial-but-real result would blur what actually happened this run
    rather than clarify it; `unsupported` is a permanent, already-disclosed config.py state that
    never fetches jobs at all, so it never has cached data to fall back to regardless.

    Returns `(updated_source_issues, fallback_provenance)`: `updated_source_issues` is the same
    shape as before — every `failed` entry's own `message` extended with the fallback note,
    everything else passed through unchanged — and merges any qualifying, not-already-present job
    straight into the `candidates` dict *in place*, so the caller's own scoring/tiering loop over
    `candidates` picks them up exactly as if they were part of this run's live search output. The
    archive file on disk is never touched by this — only the in-memory `candidates` dict this one
    render pass builds its HTML from, which is what keeps re-running this script against the same
    archive idempotent. `fallback_provenance` is new (docs/agent-runtime-audit.md's "provenance
    fields for stale-source fallback" finding): one `{"source_key", "merged_count",
    "last_success_at"}` record per `failed` source, structured rather than folded only into the
    human-readable `message` prose — a caller reading `--result-json` (`job-hunter pipeline`,
    an agent) can tell which sources in a given report came from the live run vs. this fallback,
    and exactly when that fallback data was last actually collected, without parsing HTML.

    Opens exactly one `Storage` connection for the whole call (not one per failed source) and
    reads `health_rows()` once into a dict keyed by `source_key` — `source_health` is a small,
    one-row-per-configured-company table, so this was never the expensive part the way the
    `jobs` table scan `source_jobs()` used to be, but there's no reason a run with several
    concurrently-failing sources (this project's own docs note `stealth_html` sources like
    astemo/google failing together as a real, recurring case) should reopen the connection and
    re-scan that table once per failure either."""
    updated: list[dict[str, Any]] = []
    fallback_provenance: list[dict[str, Any]] = []
    failed_keys = [h.get("source_key") for h in source_issues if h.get("status") == "failed"]
    last_success_by_key: dict[str, str | None] = {}
    if failed_keys:
        with Storage(database_path) as storage:
            health_by_key = {row["source_key"]: row for row in storage.health_rows()}
        last_success_by_key = {
            key: (health_by_key[key]["last_success_at"] if key in health_by_key else None)
            for key in failed_keys
        }
    for health in source_issues:
        if health.get("status") != "failed":
            updated.append(health)
            continue
        health = dict(health)
        source_key = health.get("source_key")
        last_success_at = last_success_by_key.get(source_key)
        base_message = health.get("message") or "No error message recorded."
        merged = 0
        if not last_success_at:
            note = "Failed to scrape — no prior successful data available for this source."
        else:
            fallback_jobs = _pool_source_jobs(
                database_path, source_key, profile, max_age_days, keywords=keywords, now=now
            )
            for job in fallback_jobs:
                key = (job.source_key, job.job_id)
                if key in candidates:
                    continue
                candidates[key] = json.loads(job.model_dump_json())
                merged += 1
            note = (
                f"Failed to scrape today — showing {merged} job(s) from the last successful "
                f"scrape on {_fmt_local_date(last_success_at)}."
            )
        health["message"] = f"{base_message} {note}"
        updated.append(health)
        fallback_provenance.append(
            {
                "source_key": source_key,
                "merged_count": merged,
                "last_success_at": last_success_at,
            }
        )
    return updated, fallback_provenance


_LIVE_STATIC_BLOCKS = (
    ('<div class="feedback-export">', "</div>"),
    ('<script>\n(function () {\n  // Per-job relevance feedback', "</script>"),
    ('<script>\n(function () {\n  // Quick-glance client-side filtering', "</script>"),
)

_LIVE_STYLE = """
  .live-toolbar-extra { display: contents; }
  .toolbar-field { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-soft); }
  .toolbar-num, .toolbar-select { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: inherit; }
  .toolbar-num { width: 64px; }
  .live-bar { position: fixed; bottom: 24px; right: 24px; z-index: 10; display: flex; flex-direction: column; align-items: flex-end; gap: 6px; font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  .live-pill { padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line); background: var(--surface); box-shadow: var(--shadow); font-weight: 600; }
  .live-pill[data-state="live"] { color: var(--accent); }
  .live-pill[data-state="saving"] { color: var(--ink-soft); }
  .live-pill[data-state="offline"] { background: var(--accent); color: var(--surface); }
  .live-notice, .live-reload, .live-archive { padding: 4px 10px; border-radius: 8px; background: var(--surface); border: 1px solid var(--line); }
  .live-notice:empty { display: none; }
  .live-archive { color: var(--muted); }
"""

_LIVE_TOOLBAR = """<span class="live-toolbar-extra">
      <label class="toolbar-field">Min score <input type="number" id="live-min-score" class="toolbar-num" min="0" max="100" step="5"></label>
      <label class="toolbar-field">Posted <select id="live-posted-days" class="toolbar-select"><option value="">any time</option><option value="7">last 7 days</option><option value="14">last 14 days</option><option value="30">last 30 days</option></select></label>
      <label class="toolbar-field">Company <select id="live-company" class="toolbar-select"><option value="">all</option></select></label>
      <label class="toolbar-field">Location <input type="search" id="live-location" class="toolbar-select" placeholder="state or city" autocomplete="off"></label>
      <label class="toolbar-field"><input type="checkbox" id="live-has-salary"> Has salary</label>
      <label class="toolbar-field">Feedback <select id="live-feedback" class="toolbar-select"><option value="">any</option><option value="untagged">untagged</option><option value="relevant">relevant</option><option value="okay">okay</option><option value="irrelevant">irrelevant</option></select></label>
      <label class="toolbar-field">Sort <select id="live-sort" class="toolbar-select"><option value="default">default</option><option value="score">score</option><option value="newest">newest</option><option value="company">company</option></select></label>
    </span>"""


def _json_for_script(value: Any) -> str:
    """JSON safe to embed inside a <script> element: `<`, `>`, `&` and the two JS line
    separators are \\u-escaped so hostile text can never close the tag or break the literal."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def _replace_block(text: str, start: str, end: str, replacement: str) -> str:
    """Replace the first `start` ... `end` span. Raises (rather than silently no-ops) if the
    template no longer contains it, so a template edit that breaks live mode fails loudly."""
    try:
        first = text.index(start)
        last = text.index(end, first) + len(end)
    except ValueError as exc:
        raise ValueError(f"radar template no longer contains the block starting {start!r}") from exc
    return text[:first] + replacement + text[last:]


def _live_bar_html(live_state: LiveState, sources: str) -> str:
    rendered = datetime.now().astimezone().strftime("%H:%M:%S")
    return (
        '<div class="live-bar">'
        '<span id="live-status" class="live-pill" role="status" data-state="live">Live</span>'
        '<span id="live-notice" class="live-notice" role="alert"></span>'
        '<span id="live-reload" class="live-reload" hidden>New results &mdash; '
        '<a href="#" id="live-reload-link">Reload</a></span>'
        f'<span class="live-archive">{_e(live_state.archive_name)} &middot; {_e(sources)} sources '
        f"&middot; rendered {rendered}</span></div>"
    )


def _live_script_html(live_state: LiveState, stem: str) -> str:
    boot = {
        "stem": stem,
        "feedback": {k: {"label": v["label"]} for k, v in live_state.feedback.items()},
        "versions": live_state.versions,
    }
    parts = [f"<script>window.__RADAR_LIVE__ = {_json_for_script(boot)};</script>"]
    for name in ("radar_live_core.js", "radar_live_ui.js"):
        source = (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
        if "</script" in source.lower():
            raise ValueError(f"{name} must not contain a script terminator")
        parts.append(f"<script>\n{source}\n</script>")
    return "\n".join(parts)


def render(
    *,
    search_path: Path,
    assessments_path: Path,
    title: str,
    keyword_label: str | None,
    new_days: int,
    undated_new_days: int = 15,
    undated_stale_days: int = 45,
    now: datetime | None = None,
    database_path: Path | None = None,
    profile: CandidateProfile | None = None,
    max_age_days: int | None = None,
    keywords: list[str] | None = None,
    collection_fallback: bool = True,
    live: bool = False,
    live_state: LiveState | None = None,
) -> tuple[str, dict[str, Any]]:
    if live and live_state is None:
        raise ValueError("live=True requires a LiveState")
    search = json.loads(search_path.read_text(encoding="utf-8"))
    candidates = {(c["source_key"], c["job_id"]): c for c in search["candidates"]}
    assessments = json.loads(assessments_path.read_text(encoding="utf-8"))
    assess_map = {(a["source_key"], a["job_id"]): a for a in assessments}

    # `now`, when passed explicitly (tests), is used with whatever tzinfo it already carries;
    # only the no-argument production default resolves the real system-local instant — this
    # is what the "eyebrow" date string below is formatted from, so a late-night run displays
    # the date as it was actually experienced, not tomorrow's UTC date.
    now = now or datetime.now().astimezone()

    # Ability 3's stale-source-collection fallback (see this module's docstring and
    # docs/pipeline-refilter-stale-source-plan.md section 4.3) — deliberately run *before* the
    # scoring loop below, not after, so a merged-in job is scored/tiered exactly like any other
    # candidate rather than needing a second, separate pass over just the merged ones. Callers
    # that never pass `database_path` (every existing test, and any other embedder of this
    # function) get exactly today's behavior — this whole block is a no-op without it, by
    # design, not merely by accident of argument defaults.
    source_issues_raw = [h for h in search.get("source_health", []) if h.get("status") != "ok"]
    fallback_provenance: list[dict[str, Any]] = []
    if collection_fallback and database_path is not None and profile is not None and max_age_days is not None:
        source_issues_raw, fallback_provenance = _apply_collection_fallback(
            source_issues=source_issues_raw, candidates=candidates, database_path=database_path,
            profile=profile, max_age_days=max_age_days, keywords=keywords, now=now,
        )

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
                "state": candidate.get("state"),
                "country": candidate.get("country"),
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
        source_issues_raw,
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
    if live:
        for start, end in _LIVE_STATIC_BLOCKS[1:]:
            template = _replace_block(template, start, end, "")
        template = _replace_block(
            template, *_LIVE_STATIC_BLOCKS[0], _live_bar_html(live_state, sources)
        )
        template = (
            template.replace("__LIVE_STYLE__", _LIVE_STYLE)
            .replace("__LIVE_TOOLBAR__", _LIVE_TOOLBAR)
            .replace("__LIVE_SCRIPT__", _live_script_html(live_state, search_path.stem))
        )
    else:
        template = (
            template.replace("__LIVE_STYLE__", "")
            .replace("__LIVE_TOOLBAR__", "")
            .replace("__LIVE_SCRIPT__", "")
        )
    feedback = live_state.feedback if live_state else None
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
            _rows_html(
                strong, empty_message="No candidates scored 75 or above for this search.",
                live=live, feedback=feedback,
            ),
        )
        .replace(
            "__REVIEW_ROWS__",
            _rows_html(
                review, empty_message="No candidates scored 50-74 for this search.",
                live=live, feedback=feedback,
            ),
        )
        .replace(
            "__BELOW_50_ROWS__",
            _rows_html(
                below_50_rows, empty_message="No candidates scored below 50 for this search.",
                live=live, feedback=feedback,
            ),
        )
        .replace(
            "__NEVER_REVIEWED_ROWS__",
            _never_reviewed_rows_html(
                never_reviewed_candidates, now=now, new_days=new_days,
                undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
                live=live, feedback=feedback,
            ),
        )
        .replace("__SOURCE_ISSUES_ROWS__", _source_issue_rows_html(source_issues))
    )
    return out, {
        "strong": len(strong),
        "review": len(review),
        "below_50": below_50,
        "never_reviewed": never_reviewed,
        "source_issues": len(source_issues),
        "failed": failed_count,
        # Structured stale-source-fallback provenance (docs/agent-runtime-audit.md's "provenance
        # fields" finding) -- one entry per `failed` source, machine-readable rather than only
        # ever folded into the HTML report's prose note. Empty whenever collection_fallback=False
        # or no source failed this run.
        "stale_source_fallback": fallback_provenance,
    }


def build(*, output_path: Path, **render_kwargs: Any) -> dict[str, Any]:
    """Render the static (file://) report and write it atomically. Signature and return value
    are unchanged for every existing caller/test; live rendering goes through `render()`."""
    out, stats = render(live=False, **render_kwargs)
    atomic_write_text(output_path, out)
    return stats


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


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags that choose *which* report to build (archive, scope, windows) — shared by this
    script's CLI and `serve_radar.py` so the two can never drift apart."""
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
    parser.add_argument("--title", default=None, help="page title (default derived from --keyword)")
    parser.add_argument(
        "--keyword", default=None,
        help="the --keyword string used for this search, if any (drives the subhead/eyebrow/default title; omit for a default profile-driven search)",
    )
    parser.add_argument(
        "--companies", default=None,
        help=(
            "disambiguate --keyword resolution among archives sharing that keyword by "
            "--companies scope (same value originally passed to job-hunter search/pipeline "
            "--companies) — omit to resolve only among unscoped archives; ignored if --search "
            "is given"
        ),
    )
    parser.add_argument(
        "--new-days", type=nonneg_int, default=10, help="posting-age window for the [New] tag (default 10)"
    )
    parser.add_argument(
        "--undated-new-days", type=nonneg_int, default=None,
        help="for jobs with no posted_at, first-seen-age window for the [New] tag (default: settings.yaml's search.undated_new_days)",
    )
    parser.add_argument(
        "--undated-stale-days", type=nonneg_int, default=None,
        help='for jobs with no posted_at, first-seen-age past which they\'re tagged "Long-standing" (default: settings.yaml\'s search.undated_stale_days)',
    )
    parser.add_argument(
        "--no-collection-fallback", action="store_true",
        help=(
            "disable the failed-source stale-job fallback merge (section 4.3/4.5 of "
            "docs/pipeline-refilter-stale-source-plan.md) — falls back to today's plain "
            "note-with-no-jobs behavior for a source that failed to scrape this run"
        ),
    )


def selection_render_kwargs(
    args: argparse.Namespace, settings: Any, profile: CandidateProfile | None
) -> dict[str, Any]:
    keywords = [t.strip() for t in args.keyword.split(",") if t.strip()] if args.keyword else None
    return dict(
        title=args.title or _default_title(args.keyword),
        keyword_label=args.keyword,
        new_days=args.new_days,
        undated_new_days=(
            args.undated_new_days if args.undated_new_days is not None else settings.search.undated_new_days
        ),
        undated_stale_days=(
            args.undated_stale_days if args.undated_stale_days is not None else settings.search.undated_stale_days
        ),
        database_path=settings.database_path,
        profile=profile,
        max_age_days=settings.search.max_posting_age_days,
        keywords=keywords,
        collection_fallback=not args.no_collection_fallback,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_selection_arguments(parser)
    parser.add_argument(
        "--output", type=Path, default=None,
        help="HTML path (default: data/radar/<search filename>.html)",
    )
    parser.add_argument(
        "--result-json", type=Path, default=None,
        help=(
            "also write {\"report_path\": \"...\", \"stale_source_fallback\": "
            "[{\"source_key\", \"merged_count\", \"last_success_at\"}, ...]} to this path on "
            "success — a structured result for a caller (job-hunter pipeline) to read instead "
            "of parsing this script's own human-readable stdout, and to know which sources in "
            "this report came from the live run vs. the stale-source fallback (empty list when "
            "--no-collection-fallback or no source failed). Purely additive: stdout is unchanged."
        ),
    )
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    args.search = resolve_search_path(search=args.search, keyword=args.keyword, companies=args.companies)


    output_path = args.output or Path("data/radar") / f"{args.search.stem}.html"

    settings = load_settings()
    stats = build(
        search_path=args.search,
        assessments_path=args.assessments,
        output_path=output_path,
        **selection_render_kwargs(args, settings, load_profile()),
    )
    print(
        f"Wrote {output_path} | strong={stats['strong']} review={stats['review']} "
        f"below_50={stats['below_50']} never_reviewed={stats['never_reviewed']} "
        f"source_issues={stats['source_issues']} (failed={stats['failed']})"
    )
    if args.result_json is not None:
        atomic_write_text(
            args.result_json,
            json.dumps(
                {
                    "report_path": str(output_path),
                    "stale_source_fallback": stats["stale_source_fallback"],
                }
            )
            + "\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
