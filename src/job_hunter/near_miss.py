"""Periodic near-miss discovery: jobs the positive-term gate rejected (`no_positive_match`) whose
descriptions still contain several of the profile's strong-relevance terms, plus vocabulary hints
and a department-coverage table.

This is a *human-only scouting aid* in the spirit of docs/broad-match-plan.md (which concluded
description similarity is unreliable as a filter but good for keyword discovery). Near-misses are
never LLM-scored, never added to `candidates`, never merged into the radar, and nothing here
changes the gate. Pure functions over `raw_active_jobs()`; report writing is below."""

from __future__ import annotations

import csv
import html
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .active_pool import raw_active_jobs
from .atomic import atomic_write_text
from .config import CandidateProfile, load_profile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency
from .vocabulary import phrase_gain, title_phrases

#: Strong terms too generic to count toward a near-miss (kept in the profile for the soft-exclude
#: rescue, but "vehicle"/"driving" alone say nothing about a role). Override with --ignore-term.
GENERIC_STRONG_TERMS = frozenset(
    {"vehicle", "behavior", "behaviour", "driving", "chassis", "camera", "radar"}
)


def html_to_text(text: str) -> str:
    text = re.sub(r"(?i)<\s*(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in html_to_text(text).split("\n") if p.strip()]


def strip_boilerplate(
    jobs: list[Job], *, threshold: float = 0.3, min_postings: int = 5, min_chars: int = 40
) -> dict[tuple[str, str], str]:
    """Description text per `(source_key, job_id)` with per-source boilerplate paragraphs removed:
    a paragraph of >= `min_chars` characters recurring in at least `threshold` of that source's
    postings (and at least twice) is company copy, not role content. Sources with fewer than
    `min_postings` jobs are left untouched (too little evidence to call anything boilerplate)."""
    by_source: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_source[job.source_key].append(job)
    cleaned: dict[tuple[str, str], str] = {}
    for source, group in by_source.items():
        paragraphs = {job.job_id: _paragraphs(job.description or "") for job in group}
        counts: Counter[str] = Counter()
        for items in paragraphs.values():
            counts.update({p.lower() for p in items if len(p) >= min_chars})
        boiler: set[str] = set()
        if len(group) >= min_postings:
            cutoff = threshold * len(group)
            boiler = {p for p, n in counts.items() if n >= 2 and n >= cutoff}
        for job in group:
            keep = [p for p in paragraphs[job.job_id] if p.lower() not in boiler]
            cleaned[(source, job.job_id)] = "\n".join(keep)
    return cleaned


def _term_regex(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def matched_terms(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if _term_regex(t).search(low)]


@dataclass(frozen=True)
class NearMiss:
    source_key: str
    job_id: str
    company: str
    title: str
    url: str
    department: str | None
    posted_at: datetime | None
    first_seen_at: datetime
    terms: tuple[str, ...]
    occurrences: int

    @property
    def score(self) -> int:
        return len(self.terms)


@dataclass(frozen=True)
class SourceCoverage:
    source_key: str
    total: int
    empty_department: int


@dataclass(frozen=True)
class VocabularyHint:
    term: str
    near_miss_jobs: int
    gain: int
    samples: tuple[str, ...]


@dataclass
class ScanResult:
    rows: list[NearMiss]
    pool_size: int
    eligible_recent: int
    empty_department: int
    coverage: list[SourceCoverage] = field(default_factory=list)
    hints: list[VocabularyHint] = field(default_factory=list)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _vocabulary_hints(
    rows: list[NearMiss], profile: CandidateProfile, pool: list[Job], *, top: int = 15, min_jobs: int = 2
) -> list[VocabularyHint]:
    frequency: Counter[str] = Counter()
    for row in rows:
        frequency.update(set(title_phrases(row.title)))
    hints: list[VocabularyHint] = []
    for phrase, count in frequency.items():
        if count < min_jobs:
            continue
        # shown only when adding it would admit at least one currently-rejected job (a phrase the
        # profile already covers, or a soft-exclude still rejects, has gain 0)
        gain = phrase_gain(profile, phrase, pool)
        if gain.count == 0:
            continue
        hints.append(VocabularyHint(phrase, count, gain.count, gain.samples))
    hints.sort(key=lambda h: (-h.near_miss_jobs, h.gain, -len(h.term), h.term))
    return hints[:top]


def scan(
    database_path: Path,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    ignore_terms: frozenset[str] = GENERIC_STRONG_TERMS,
    min_terms: int = 3,
    since: datetime | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> ScanResult:
    eligible = [j for j in raw_active_jobs(database_path) if passes_recency(j, max_age_days, now=now)]
    pool = [j for j in eligible if evaluate_prefilter(j, profile).rule is PrefilterRule.NO_POSITIVE_MATCH]

    ignored = {t.lower() for t in ignore_terms}
    terms = [t for t in profile.strong_relevance_terms if t.lower() not in ignored]
    cleaned = strip_boilerplate(pool)
    rows: list[NearMiss] = []
    for job in pool:
        text = cleaned[(job.source_key, job.job_id)]
        hits = matched_terms(text, terms)
        if len(hits) < min_terms:
            continue
        if since is not None and _aware(job.first_seen_at) <= _aware(since):
            continue
        low = text.lower()
        occurrences = sum(len(_term_regex(t).findall(low)) for t in hits)
        rows.append(
            NearMiss(
                job.source_key, job.job_id, job.company, job.title, job.url, job.department,
                job.posted_at, job.first_seen_at, tuple(hits), occurrences,
            )
        )
    rows.sort(key=lambda r: (-r.score, -r.occurrences, r.source_key, r.title))
    if limit is not None:
        rows = rows[:limit]

    totals: Counter[str] = Counter(j.source_key for j in eligible)
    empties: Counter[str] = Counter(j.source_key for j in eligible if not (j.department or "").strip())
    coverage = sorted(
        (SourceCoverage(k, totals[k], empties[k]) for k in totals if empties[k]),
        key=lambda c: (-c.empty_department, c.source_key),
    )
    return ScanResult(
        rows=rows,
        pool_size=len(pool),
        eligible_recent=len(eligible),
        empty_department=sum(empties.values()),
        coverage=coverage,
        hints=_vocabulary_hints(rows, profile, pool),
    )


def _fmt_day(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d") if moment else ""


def render_csv(rows: list[NearMiss]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["rank", "source", "title", "score", "terms", "posted", "first_seen", "url"])
    for rank, row in enumerate(rows, 1):
        writer.writerow(
            [rank, row.source_key, row.title, row.score, "; ".join(row.terms),
             _fmt_day(row.posted_at), _fmt_day(row.first_seen_at), row.url]
        )
    return buffer.getvalue()


def render_html(result: ScanResult, *, generated_at: datetime, since: datetime | None) -> str:
    e = html.escape
    scope = f"new since {_fmt_day(since)}" if since else "all (no previous scan)"
    empty_pct = (100 * result.empty_department // result.eligible_recent) if result.eligible_recent else 0
    parts = [
        "<!DOCTYPE html><meta charset='utf-8'><title>Near-miss report</title>",
        "<style>body{font:14px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse;width:100%}"
        "td,th{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}"
        "code{background:#f3f3f3;padding:1px 4px}h2{margin-top:2rem}</style>",
        f"<h1>Near-miss report</h1><p>{e(generated_at.astimezone().strftime('%Y-%m-%d %H:%M'))} - "
        f"{len(result.rows)} job(s), {e(scope)}. Scanned {result.pool_size} rejected jobs of "
        f"{result.eligible_recent} eligible and recent. These jobs failed the title/department gate "
        "but mention several strong-relevance terms in their descriptions. They are <b>not</b> "
        "reviewed, scored, or in the radar.</p>",
        "<h2>Near-misses</h2><table><tr><th>#</th><th>Source</th><th>Title</th><th>Score</th>"
        "<th>Terms</th><th>Posted</th><th>Link</th></tr>",
    ]
    for rank, row in enumerate(result.rows, 1):
        parts.append(
            f"<tr><td>{rank}</td><td>{e(row.source_key)}</td><td>{e(row.title)}</td><td>{row.score}</td>"
            f"<td>{e(', '.join(row.terms))}</td><td>{e(_fmt_day(row.posted_at))}</td>"
            f"<td><a href='{e(row.url, quote=True)}'>open</a></td></tr>"
        )
    parts.append("</table><h2>Vocabulary hints</h2>")
    if result.hints:
        parts.append("<table><tr><th>Title term</th><th>In near-misses</th><th>Would admit</th><th>Examples</th><th>Preview</th></tr>")
        for hint in result.hints:
            preview = f'uv run python scripts/diff_profile.py --add "target_title_terms:{hint.term}"'
            parts.append(
                f"<tr><td>{e(hint.term)}</td><td>{hint.near_miss_jobs}</td><td>{hint.gain} job(s)</td>"
                f"<td>{e('; '.join(hint.samples))}</td><td><code>{e(preview)}</code></td></tr>"
            )
        parts.append("</table><p>Add terms only through the job-feedback skill (preview, then confirm).</p>")
    else:
        parts.append("<p>No new title vocabulary stood out.</p>")
    parts.append(
        f"<h2>Department coverage</h2><p>{result.empty_department} of {result.eligible_recent} eligible jobs "
        f"({empty_pct}%) have no department - <b>title is the only gate signal</b> for them. Top sources:</p>"
        "<table><tr><th>Source</th><th>Jobs without a department</th><th>of total</th></tr>"
    )
    for item in result.coverage[:10]:
        parts.append(f"<tr><td>{e(item.source_key)}</td><td>{item.empty_department}</td><td>{item.total}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def read_last_scan(state_path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(state_path.read_text(encoding="utf-8"))["last_scan_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_last_scan(state_path: Path, moment: datetime) -> None:
    atomic_write_text(state_path, json.dumps({"last_scan_at": moment.isoformat()}) + "\n")


def cli_near_misses(args, settings) -> int:
    out_dir = Path(args.output_dir) if args.output_dir else settings.database_path.parent / "near-miss"
    state_path = out_dir / "state.json"
    since = None if args.all else read_last_scan(state_path)
    limit = args.limit if args.limit is not None else (100 if since is None else None)
    now = datetime.now(UTC)
    result = scan(
        settings.database_path,
        load_profile(),
        settings.search.max_posting_age_days,
        ignore_terms=GENERIC_STRONG_TERMS | frozenset(t.lower() for t in args.ignore_term),
        min_terms=args.min_terms,
        since=since,
        limit=limit,
        now=now,
    )
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-T-%H-%M-%S")
    html_path, csv_path = out_dir / f"{stamp}.html", out_dir / f"{stamp}.csv"
    try:
        atomic_write_text(html_path, render_html(result, generated_at=now, since=since))
        atomic_write_text(csv_path, render_csv(result.rows))
        if not args.no_state:
            write_last_scan(state_path, now)
    except OSError as exc:
        print(f"job-hunter: could not write the near-miss report: {exc}", file=sys.stderr)
        return 2
    print(
        f"Near-misses: {len(result.rows)} new job(s) (scanned {result.pool_size} rejected of "
        f"{result.eligible_recent} eligible)"
    )
    print(f"  report: {html_path}")
    print(f"  csv:    {csv_path}")
    return 0
