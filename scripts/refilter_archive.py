#!/usr/bin/env python3
"""Re-apply the *current* candidate_profile.yaml's prefilter/recency logic to an already-collected
search archive — no network, no adapter/scraper invoked. Useful right after adding, removing, or
loosening a filtering term: see its effect on a report you already have, without waiting for (or
risking rate limits from) a fresh live search.

Rebuilds the archive's `candidates` list from scratch out of SQLite's current active/US-eligible
job pool — scoped to the sources this archive's own `source_health` recorded as having actually
*succeeded* (`ok`/`warning`), not merely attempted — rather than narrowing whatever's already
sitting in `candidates`. That narrowing-in-place design
was tried first and found to be a one-way ratchet: once a `soft_exclude_terms` edit dropped a job
from `candidates`, that job's data was gone from the archive file, so a *later* loosening edit
meant to rescue it (a new `strong_relevance_terms` override, a removed `soft_exclude_terms` entry)
had nothing left in the file to restore — the rescue silently did nothing. Concrete case that
surfaced this: an Apple "Custom Silicon Validation Engineer - Camera Hardware" posting dropped by
adding `silicon` to `soft_exclude_terms`, meant to be rescued by later adding `camera` to
`strong_relevance_terms` — the rescue had zero effect until the posting was manually pulled back
out of SQLite. Every observed job is persisted there regardless of prefilter outcome
(`collector.py`'s `storage.upsert_job()` runs before prefilter is ever applied), so rebuilding from
it is always possible without a live search — this is the same "stored, `us_eligible`,
recency-passing job" universe `scripts/diff_profile.py` already previews against, using the same
`passes_prefilter`/`passes_recency`.

Restricting to the archive's own successful `source_health` source set (rather than every source
currently in `companies.yaml`) matters for a dated, non-default archive: onboarding a new company
later should never cause an old keyword archive to silently gain that company's jobs on a
re-filter — it was never part of what that archive searched. The same reasoning extends to a
source that *was* attempted but failed: a `stealth_html` source failing with "the 'stealth'
dependency group is not installed" contributes zero jobs to this archive either way, but SQLite
can still hold that source's jobs `active` from an earlier, unrelated successful run — counting a
failed attempt as "in scope" pulled those jobs back in on every refilter as spurious "Gained"
entries with no connection to any actual profile change, since this archive never had them to
lose in the first place (confirmed live: `astemo`/`google` failing this way surfaced 10 unrelated
"Gained" jobs alongside an otherwise-clean `soft_exclude_terms` edit). A `--search`/
`--keyword`-resolved archive with no `source_health` at all (an unexpected/older file shape) falls
back to no source restriction rather than raising, since failing shouldn't be the behavior for a
merely-missing optional field — but a `source_health` where every entry failed correctly restricts
to nothing, not the same "unrestricted" fallback, since that's a known scope of zero, not an
unknown one.

One real behavior change from the old narrowing design: because candidates are rebuilt from
SQLite's *current* state, a job's title/description/location here reflects the latest observed
content for it (across any run, not just this archive's original collection), not a frozen
snapshot from when this archive was first written — matching this tool's existing recency
handling, which already recomputes against wall-clock *now* rather than assuming nothing aged
since collection.

The raw "every active/US-eligible job, optionally source-scoped" query this module's `refilter()`
runs against SQLite used to be a private `_active_jobs()` right here; it now lives in
`job_hunter.active_pool.raw_active_jobs()` instead (see `docs/pipeline-refilter-stale-source-plan.md`
section 4.4), moved into the installed package once `scripts/render_radar.py`'s stale-source
fallback needed the same query for one source at a time (`active_pool.source_jobs()`). This
module is a behavior-preserving refactor around that move, not a behavior change — every test in
`tests/test_refilter_archive.py` still exercises the identical `refilter()` output.

Usage:
    uv run python scripts/refilter_archive.py
    uv run python scripts/refilter_archive.py --keyword ADAS
    uv run python scripts/refilter_archive.py --search data/searches/default_2026-09-05.json --output /tmp/refiltered.json
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Reused rather than reimplemented — same [New]/sponsorship/hybrid-remote tag rules and date
# formatting as the other two reports, so a job is never tagged differently across all three.
from diff_profile import (  # noqa: E402
    _e,
    _fmt_posted_date,
    _is_new,
    _job_meta_html,
    _job_tags,
    _local,
    _report_timestamp,
)
from render_radar import _tier  # noqa: E402

from job_hunter.active_pool import raw_active_jobs
from job_hunter.atomic import atomic_write_text
from job_hunter.config import load_profile, load_settings
from job_hunter.models import Job
from job_hunter.prefilter import passes_prefilter, passes_recency
from job_hunter.rootutil import add_project_argument, chdir_to_project_root
from job_hunter.search_archive import resolve_search_path

_FAILED_STATUSES = {"failed", "unsupported"}


def _successful_source_scope(data: dict[str, Any]) -> set[str] | None:
    """Source keys this archive's own `source_health` did *not* record as having failed
    (excludes `failed`/`unsupported` — the complement of the success set `cli.py`'s
    `_source_test` uses), not merely attempted. Deliberately opt-out (exclude known-bad) rather
    than opt-in (require known-good): a `source_health` row with no `status` field at all (an
    unexpected/degraded shape, distinct from `source_health` being absent entirely — see below)
    is kept in scope rather than dropped, since there's no positive evidence it failed — matching
    this project's general preference for false negatives over false positives in any filtering
    mechanism.

    Excluding a `failed`/`unsupported` entry here matters for the same reason restricting to
    `source_health` at all does (see `refilter()`'s docstring: an old archive shouldn't silently
    gain a company's jobs just because that company was onboarded later) — a source can also be
    "not really part of what this archive searched" by having been attempted and failed, not just
    by being absent entirely. Confirmed live: a `stealth_html` source (e.g. `astemo`/`google`)
    that failed with "the 'stealth' dependency group is not installed" during this archive's own
    collection run contributes zero jobs to `candidates` either way, but SQLite can still hold
    that source's jobs `active` from an earlier, successful run — including a failed source's key
    here pulled those jobs back in on every refilter, surfacing as spurious "Gained" entries with
    no connection to any actual profile change, since the archive never had them to lose in the
    first place. `unsupported` is excluded for the same reason it's harmless to exclude: that
    adapter never fetches any jobs, so it never has SQLite rows to backfill from anyway."""
    statuses = {
        row["source_key"]: row.get("status")
        for row in data.get("source_health", [])
        if row.get("source_key")
    }
    if not statuses:
        # No source_health at all (an unexpected/older archive shape) — genuinely unknown scope,
        # so fall back to no restriction rather than guessing. Distinct from the case below: a
        # real source_health where every entry is a known failure correctly restricts to nothing,
        # not the same "unrestricted" fallback, since that's a known scope of zero, not an
        # unknown one.
        return None
    return {key for key, status in statuses.items() if status not in _FAILED_STATUSES}


def refilter(
    data: dict[str, Any],
    *,
    keywords: list[str] | None = None,
    now: datetime | None = None,
    database_path: Path | str | None = None,
) -> dict[str, Any]:
    """Returns a new archive dict — `candidates` rebuilt from SQLite's current active/eligible
    job pool (scoped to the sources this archive's own `source_health` recorded as having
    succeeded, not merely attempted — see `_successful_source_scope`), filtered by the current
    on-disk profile, summary counts updated to match. Does not mutate the input dict."""
    now = now or datetime.now(UTC)
    profile = load_profile()
    settings = load_settings()
    max_age_days = settings.search.max_posting_age_days
    resolved_db_path = Path(database_path) if database_path is not None else settings.database_path

    source_scope = _successful_source_scope(data)
    # Deliberately the *unfiltered* multi-source pool (job_hunter.active_pool.raw_active_jobs,
    # not its sibling source_jobs()) — this loop still needs to distinguish a prefilter failure
    # from a recency failure itself (stale_excluded counts only the latter), which a
    # pre-filtered single-source list can no longer tell apart once the excluded jobs are
    # already gone from it. See active_pool.py's module docstring for the full reasoning.
    jobs = raw_active_jobs(resolved_db_path, source_scope)

    kept: list[Job] = []
    stale_excluded = 0
    for job in jobs:
        if not passes_prefilter(job, profile, keywords=keywords):
            continue
        if not passes_recency(job, max_age_days, now=now):
            stale_excluded += 1
            continue
        kept.append(job)
    kept.sort(key=lambda job: (-(job.posted_at.timestamp() if job.posted_at else 0), job.title))

    new_data = dict(data)
    new_data["candidates"] = [json.loads(job.model_dump_json()) for job in kept]
    new_summary = dict(data.get("summary", {}))
    new_summary["prefilter_candidates"] = len(kept)
    new_summary["stale_excluded"] = stale_excluded
    new_data["summary"] = new_summary
    return new_data


_HTML_TEMPLATE = """<!DOCTYPE html>
<meta charset="utf-8">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #F3F6F7;
    --ink: #14191F;
    --ink-soft: #4B5560;
    --surface: #FFFFFF;
    --line: #DCE3E7;
    --accent: #0C7F91;
    --accent-soft: #E4F1F3;
    /* Score tiers — identical values to radar_template.html's/diff_profile.py's. */
    --score-fair: #5C7A99;
    --score-moderate: #0C7F91;
    --score-promising: #A3760E;
    --score-strong: #D2691E;
    --score-exceptional: #1D9A66;
    /* Status color: reserved, fixed meaning ("good") — mirrors diff_profile.py's
       and radar_template.html's identical --status-good token. */
    --status-good: #1D9A66;
    --status-good-soft: #E4F5EC;
    --danger: #C1443A;
    --danger-soft: #FBEAE8;
    --arrangement-remote: #2D6FB0;
    --arrangement-remote-soft: #E4EEF8;
    --arrangement-hybrid: #7B5CAE;
    --arrangement-hybrid-soft: #EFE8F7;
    --muted: #6B7480;
    --shadow: 0 1px 2px rgba(20, 25, 31, 0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper: #10151A; --ink: #E9EDEF; --ink-soft: #A6B0B8; --surface: #171E24; --line: #2A333A;
      --accent: #3FC1D4; --accent-soft: #17323A;
      --score-fair: #7FA3C4; --score-moderate: #3FC1D4; --score-promising: #E3B24A; --score-strong: #E8875A; --score-exceptional: #3FCC8C;
      --status-good: #3FCC8C; --status-good-soft: #163829;
      --danger: #E2695E; --danger-soft: #3A1F1C; --arrangement-remote: #6FB1EE; --arrangement-remote-soft: #17293A;
      --arrangement-hybrid: #C0A3EA; --arrangement-hybrid-soft: #2A2038; --muted: #8A95A0; --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
    }
  }
  :root[data-theme="dark"] {
    --paper: #10151A; --ink: #E9EDEF; --ink-soft: #A6B0B8; --surface: #171E24; --line: #2A333A;
    --accent: #3FC1D4; --accent-soft: #17323A;
    --score-fair: #7FA3C4; --score-moderate: #3FC1D4; --score-promising: #E3B24A; --score-strong: #E8875A; --score-exceptional: #3FCC8C;
    --status-good: #3FCC8C; --status-good-soft: #163829;
    --danger: #E2695E; --danger-soft: #3A1F1C; --arrangement-remote: #6FB1EE; --arrangement-remote-soft: #17293A;
    --arrangement-hybrid: #C0A3EA; --arrangement-hybrid-soft: #2A2038; --muted: #8A95A0; --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
  }
  * { box-sizing: border-box; }
  body { font-family: "IBM Plex Sans", system-ui, sans-serif; background: var(--paper); color: var(--ink); margin: 0; line-height: 1.5; }
  main { max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
  h1 { font-family: "Big Shoulders Display", system-ui, sans-serif; font-weight: 700; font-size: 32px; letter-spacing: -0.005em; margin: 0 0 4px; }
  h2 { font-family: "Big Shoulders Display", system-ui, sans-serif; font-weight: 700; font-size: 24px; margin: 0 0 6px; }
  main > p { color: var(--muted); font-size: 13px; }
  .stats { display: flex; gap: 12px; margin: 20px 0; flex-wrap: wrap; }
  .stat { background: var(--surface); border: 1px solid var(--line); border-radius: 4px; padding: 12px 18px; box-shadow: var(--shadow); }
  .stat-value { font-family: "IBM Plex Mono", monospace; font-size: 22px; font-weight: 700; display: block; font-variant-numeric: tabular-nums; }
  .stat-label { font-size: 11px; letter-spacing: 0.04em; color: var(--muted); text-transform: uppercase; }
  section { margin: 32px 0; }
  .rows { display: flex; flex-direction: column; gap: 8px; }
  .row { background: var(--surface); border: 1px solid var(--line); border-radius: 3px; box-shadow: var(--shadow); padding: 12px 16px; }
  .row-top { display: grid; grid-template-columns: 44px 1fr auto auto; align-items: center; gap: 14px; }
  .score { font-family: "IBM Plex Mono", monospace; font-weight: 600; font-size: 20px; font-variant-numeric: tabular-nums; text-align: right; color: var(--ink-soft); }
  .score-nr { color: var(--muted); font-size: 13px; letter-spacing: 0.02em; }
  .row.tier-exceptional .score { color: var(--score-exceptional); }
  .row.tier-strong .score { color: var(--score-strong); }
  .row.tier-promising .score { color: var(--score-promising); }
  .row.tier-moderate .score { color: var(--score-moderate); }
  .row.tier-fair .score { color: var(--score-fair); }
  .job { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .job-title-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .job-title { font-weight: 600; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .job-title-line .tag-new { flex: none; }
  .job-company { font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .job-meta { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .job-salary { color: var(--accent); font-weight: 600; }
  .job-date { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted); white-space: nowrap; }
  .row-end { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
  .tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; max-width: 220px; }
  .tag { font-family: "IBM Plex Mono", monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.03em; padding: 3px 7px; border-radius: 2px; white-space: nowrap; }
  .tag-sponsor-yes { background: var(--status-good-soft); color: var(--status-good); }
  .tag-sponsor-no { background: var(--danger-soft); color: var(--danger); }
  .tag-long-standing { background: var(--line); color: var(--muted); }
  .tag-remote { background: var(--arrangement-remote-soft); color: var(--arrangement-remote); }
  .tag-hybrid { background: var(--arrangement-hybrid-soft); color: var(--arrangement-hybrid); }
  .tag-new { display: inline-block; background: var(--accent); color: var(--surface); font-weight: 700; }
  @media (prefers-reduced-motion: no-preference) { .tag-new { animation: pulse-new 1.4s ease-in-out infinite; } }
  @keyframes pulse-new { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.12); opacity: 0.72; } }
  .apply-link { font-family: "IBM Plex Mono", monospace; font-size: 12.5px; font-weight: 500; color: var(--accent); text-decoration: none; border-bottom: 1px solid transparent; white-space: nowrap; }
  .apply-link:hover, .apply-link:focus-visible { border-bottom-color: var(--accent); }
  .row-meta { font-size: 12.5px; color: var(--muted); margin-top: 8px; }
  @media (max-width: 560px) {
    .row-top { grid-template-columns: 36px 1fr auto auto; }
    .tags { max-width: 90px; }
    .job-date { display: none; }
  }
  .empty { color: var(--muted); font-style: italic; }
  .feedback-buttons { display: flex; gap: 4px; align-items: center; }
  .fb-btn { font-size: 13px; line-height: 1; padding: 4px 6px; border-radius: 3px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; }
  .fb-btn:hover { border-color: var(--accent); }
  .fb-btn.fb-active { border-color: var(--accent); background: var(--accent-soft); }
  .fb-btn.fb-irrelevant.fb-active { border-color: var(--danger); background: var(--danger-soft); }
  .row.fb-tagged { border-left: 3px solid var(--accent); }
  .feedback-export { position: fixed; bottom: 24px; right: 24px; z-index: 10; }
  #feedback-export-btn {
    font-family: "IBM Plex Mono", monospace; font-size: 13px; font-weight: 600; padding: 12px 20px;
    border-radius: 999px; border: 1px solid var(--line); background: var(--accent); color: var(--surface);
    cursor: pointer; box-shadow: 0 2px 10px rgba(20, 25, 31, 0.2);
  }
  #feedback-export-btn:disabled { background: var(--surface); color: var(--muted); cursor: not-allowed; box-shadow: var(--shadow); opacity: 0.7; }
</style>
<main>
  <h1>__TITLE__</h1>
  <p>Evaluated __EVALUATED_AT__ against __ACTIVE_POOL__ currently-active stored jobs (recency cutoff
    __MAX_AGE_DAYS__ days) &middot; compares this archive's previously-saved candidates to what the
    same profile/pool produces right now &mdash; this is the total effect of everything that's
    changed since this archive was last written, not only a profile-term edit.</p>
  <div class="stats">
    <div class="stat"><span class="stat-value">__BEFORE_COUNT__</span><span class="stat-label">Before</span></div>
    <div class="stat"><span class="stat-value">__AFTER_COUNT__</span><span class="stat-label">After</span></div>
    <div class="stat"><span class="stat-value">__RETAINED__</span><span class="stat-label">Retained</span></div>
    <div class="stat"><span class="stat-value">__GAINED_COUNT__</span><span class="stat-label">Gained</span></div>
    <div class="stat"><span class="stat-value">__LOST_COUNT__</span><span class="stat-label">Lost</span></div>
  </div>
  <section>
    <h2>Gained</h2>
    <div class="rows">__GAINED_ROWS__</div>
  </section>
  <section>
    <h2>Lost</h2>
    <div class="rows">__LOST_ROWS__</div>
  </section>
</main>

<div class="feedback-export">
  <button type="button" id="feedback-export-btn" disabled>Export Feedback (0)</button>
</div>

<script>
(function () {
  // Same click-to-tag / export-on-demand mechanism as the other two reports (job-radar,
  // profile-diff) — identical radar-feedback-*.json export shape, ingested by
  // apply_radar_feedback.py with no changes regardless of which report a label came from.
  var STORAGE_KEY = 'job-hunter-feedback:__DIFF_STEM__';
  var feedback = {};

  function loadPersisted() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) {
      return {};
    }
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(feedback));
    } catch (e) {}
  }

  function exportButton() {
    return document.getElementById('feedback-export-btn');
  }

  function refreshExportButton() {
    var count = Object.keys(feedback).length;
    var btn = exportButton();
    btn.textContent = 'Export Feedback (' + count + ')';
    btn.disabled = count === 0;
  }

  feedback = loadPersisted();

  document.querySelectorAll('.feedback-buttons').forEach(function (group) {
    var buttons = group.querySelectorAll('.fb-btn');
    var key = group.dataset.sourceKey + '|' + group.dataset.jobId;
    var row = group.closest('.row');

    if (!feedback[key] && group.dataset.dbLabel) {
      feedback[key] = {
        source_key: group.dataset.sourceKey, job_id: group.dataset.jobId,
        company: group.dataset.company, title: group.dataset.title,
        department: group.dataset.department || null,
        score: group.dataset.score ? parseInt(group.dataset.score, 10) : null,
        label: group.dataset.dbLabel
      };
    }

    var restored = feedback[key];
    if (restored) {
      buttons.forEach(function (b) { if (b.dataset.label === restored.label) b.classList.add('fb-active'); });
      if (row) row.classList.add('fb-tagged');
    }

    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var alreadyActive = btn.classList.contains('fb-active');
        buttons.forEach(function (b) { b.classList.remove('fb-active'); });
        if (alreadyActive) {
          delete feedback[key];
          if (row) row.classList.remove('fb-tagged');
        } else {
          btn.classList.add('fb-active');
          feedback[key] = {
            source_key: group.dataset.sourceKey, job_id: group.dataset.jobId,
            company: group.dataset.company, title: group.dataset.title,
            department: group.dataset.department || null,
            score: group.dataset.score ? parseInt(group.dataset.score, 10) : null,
            label: btn.dataset.label
          };
          if (row) row.classList.add('fb-tagged');
        }
        persist();
        refreshExportButton();
      });
    });
  });

  refreshExportButton();

  exportButton().addEventListener('click', function () {
    var rows = Object.keys(feedback).map(function (k) { return feedback[k]; });
    if (rows.length === 0) return;
    var blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = 'radar-feedback-__DIFF_STEM__.json';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  });
})();
</script>
"""


def _render_job_rows(
    jobs: list[Job], *, now: datetime, empty_message: str, undated_new_days: int, undated_stale_days: int
) -> str:
    if not jobs:
        return f'<p class="empty">{_e(empty_message)}</p>'
    parts = []
    for job in jobs:
        new_badge = (
            '<span class="tag tag-new">New</span>'
            if _is_new(job, now=now, undated_new_days=undated_new_days)
            else ""
        )
        tags = _job_tags(job, now=now, undated_new_days=undated_new_days, undated_stale_days=undated_stale_days)
        date_display = _fmt_posted_date(job.posted_at, job.first_seen_at)
        # `raw_active_jobs` only ever attaches `prior_assessment` when its content_hash still
        # matches the job's current one — so its mere presence already means "valid", unlike
        # diff_profile.py's version which has to check staleness itself against a
        # separately-loaded assessments dict.
        score = job.prior_assessment.score if job.prior_assessment else None
        if score is None:
            score_html = '<span class="score score-nr" title="Not yet reviewed by the local model">NR</span>'
            tier_class = ""
        else:
            score_html = f'<span class="score">{score}</span>'
            tier_class = f" tier-{_tier(score)}"
        meta_html = _job_meta_html(job.location_raw, job.salary_evidence)
        job_meta = f'<span class="job-meta">{meta_html}</span>' if meta_html else ""
        feedback_buttons = f'''<span class="feedback-buttons"
              data-source-key="{_e(job.source_key)}" data-job-id="{_e(job.job_id)}"
              data-company="{_e(job.company)}" data-title="{_e(job.title)}"
              data-department="{_e(job.department)}" data-score="{score if score is not None else ''}"
              data-db-label="">
              <button type="button" class="fb-btn fb-relevant" data-label="relevant" title="Relevant">&#128077;</button>
              <button type="button" class="fb-btn fb-okay" data-label="okay" title="Okay">&#128994;</button>
              <button type="button" class="fb-btn fb-irrelevant" data-label="irrelevant" title="Irrelevant">&#128078;</button>
            </span>'''
        parts.append(f"""
        <div class="row{tier_class}">
          <div class="row-top">
            {score_html}
            <span class="job">
              <span class="job-title-line">
                {new_badge}
                <span class="job-title">{_e(job.title)}</span>
              </span>
              <span class="job-company">{_e(job.company)}</span>
              {job_meta}
            </span>
            <span class="tags">{tags}</span>
            <span class="row-end">
              <span class="job-date">{_e(date_display)}</span>
              {feedback_buttons}
              <a class="apply-link" href="{html.escape(job.url, quote=True)}" target="_blank" rel="noopener">View posting &#8599;</a>
            </span>
          </div>
          <div class="row-meta">last seen {_e(str(_local(job.last_seen_at)))}</div>
        </div>""")
    return "".join(parts)


def render_archive_diff_html(
    *, gained: list[Job], lost: list[Job], retained: int, before_count: int, after_count: int,
    active_pool: int, max_age_days: int, undated_new_days: int, undated_stale_days: int,
    output_path: Path, title: str, now: datetime,
) -> None:
    out = (
        _HTML_TEMPLATE.replace("__TITLE__", _e(title))
        .replace("__DIFF_STEM__", _e(output_path.stem))
        .replace("__EVALUATED_AT__", _e(_local(now).isoformat()))
        .replace("__ACTIVE_POOL__", str(active_pool))
        .replace("__MAX_AGE_DAYS__", str(max_age_days))
        .replace("__BEFORE_COUNT__", str(before_count))
        .replace("__AFTER_COUNT__", str(after_count))
        .replace("__RETAINED__", str(retained))
        .replace("__GAINED_COUNT__", str(len(gained)))
        .replace("__LOST_COUNT__", str(len(lost)))
        .replace(
            "__GAINED_ROWS__",
            _render_job_rows(
                gained, now=now, empty_message="Nothing gained.",
                undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
            ),
        )
        .replace(
            "__LOST_ROWS__",
            _render_job_rows(
                lost, now=now, empty_message="Nothing lost.",
                undated_new_days=undated_new_days, undated_stale_days=undated_stale_days,
            ),
        )
    )
    atomic_write_text(output_path, out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--search", type=Path, default=None, help="archive to re-filter (default: newest overall)")
    parser.add_argument("--keyword", default=None, help="resolve --search by keyword, and use as the positive-match override (same as job-hunter search --keyword)")
    parser.add_argument("--output", type=Path, default=None, help="write here instead of overwriting the input archive in place")
    parser.add_argument("--no-report", action="store_true", help="skip writing the HTML gained/lost report")
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)

    search_path = resolve_search_path(search=args.search, keyword=args.keyword)
    keywords = [term.strip() for term in args.keyword.split(",") if term.strip()] if args.keyword else None

    now = datetime.now(UTC)
    data = json.loads(search_path.read_text(encoding="utf-8"))
    before_by_key = {(c["source_key"], c["job_id"]): c for c in data["candidates"]}
    before_count = len(before_by_key)
    new_data = refilter(data, keywords=keywords, now=now)
    after_by_key = {(c["source_key"], c["job_id"]): c for c in new_data["candidates"]}
    after_count = len(after_by_key)
    gained_keys = after_by_key.keys() - before_by_key.keys()
    lost_keys = before_by_key.keys() - after_by_key.keys()
    retained = len(after_by_key.keys() & before_by_key.keys())

    output_path = args.output or search_path
    atomic_write_text(output_path, json.dumps(new_data, indent=2, default=str, ensure_ascii=False) + "\n")

    print(
        f"Re-filtered {search_path} -> {output_path}: {after_count} candidate(s) "
        f"(was {before_count}, {len(lost_keys)} removed, {len(gained_keys)} gained)"
    )

    if not args.no_report:
        # Reconstructs Job objects from the archive dicts directly (both before_by_key's and
        # after_by_key's entries are exactly job.model_dump_json() output — see refilter()
        # above) rather than re-querying SQLite, so the report always matches what was just
        # written to output_path, not a second, potentially-inconsistent snapshot.
        gained_jobs = [Job(**after_by_key[key]) for key in gained_keys]
        lost_jobs = [Job(**before_by_key[key]) for key in lost_keys]
        gained_jobs.sort(key=lambda job: (-(job.posted_at.timestamp() if job.posted_at else 0), job.title))
        lost_jobs.sort(key=lambda job: (-(job.posted_at.timestamp() if job.posted_at else 0), job.title))

        settings = load_settings()
        source_scope = _successful_source_scope(data)
        active_pool = len(raw_active_jobs(settings.database_path, source_scope))

        report_path = Path("data/profile-diff") / f"archive-{search_path.stem}-{_report_timestamp(now)}.html"
        render_archive_diff_html(
            gained=gained_jobs, lost=lost_jobs, retained=retained,
            before_count=before_count, after_count=after_count, active_pool=active_pool,
            max_age_days=settings.search.max_posting_age_days,
            undated_new_days=settings.search.undated_new_days,
            undated_stale_days=settings.search.undated_stale_days,
            output_path=report_path,
            title=f"Archive Refilter: {search_path.stem}", now=now,
        )
        print(f"Wrote {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
