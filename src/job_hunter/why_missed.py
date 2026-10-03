"""`job-hunter why-missed`: explain, stage by stage, why a stored job was or was not in the
candidates/radar, and for a gate rejection propose the smallest title terms that would admit it
(with how many other already-rejected jobs each would also admit).

Everything here is deterministic and read-only: it never edits the profile, the DB, an archive or
the radar. Suggestions only ever reach `candidate_profile.yaml` through the `job-feedback` skill,
which previews with `scripts/diff_profile.py` and stops for the user's confirmation."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .active_pool import StoredJob, connect_readonly, find_jobs, url_host
from .config import CandidateProfile, load_profile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency
from .search_archive import resolve_search_path
from .vocabulary import BROAD_TERM_THRESHOLD, phrase_gain, rejected_pool, title_phrases


@dataclass(frozen=True)
class ArchiveInfo:
    path: str
    in_candidates: bool
    source_status: str | None


@dataclass
class Stage:
    name: str
    ok: bool | None  # None: not applicable / unknown
    detail: str


@dataclass(frozen=True)
class TermSuggestion:
    term: str
    gain: int
    broad: bool
    samples: tuple[str, ...]


@dataclass
class WhyMissed:
    source_key: str
    job_id: str
    title: str
    stages: list[Stage]
    verdict: str
    suggestions: list[TermSuggestion] = field(default_factory=list)
    preview_commands: list[str] = field(default_factory=list)
    # stages before the prefilter that also fail: a new title term alone will not surface the job
    also_blocked_by: list[str] = field(default_factory=list)
    # whether the job itself is in the rejected pool the gains are counted over
    job_in_pool: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def suggest_terms(
    job: Job,
    profile: CandidateProfile,
    pool: list[Job],
    *,
    max_suggestions: int = 5,
    broad_threshold: int = BROAD_TERM_THRESHOLD,
) -> list[TermSuggestion]:
    """Title phrases that, appended to `target_title_terms`, admit `job` under the real gate,
    most precise first (smallest gain, then longest). A phrase that is not a substring of the
    job's own lower-cased title, or that a later rule (e.g. a soft-exclude) still rejects, is not
    offered. `gain` counts every pool job admitted, this one included when it is in the pool."""
    title_lower = job.title.lower()
    suggestions: list[TermSuggestion] = []
    for phrase in title_phrases(job.title):
        if phrase not in title_lower:
            continue
        widened = profile.model_copy(
            update={"target_title_terms": [*profile.target_title_terms, phrase]}
        )
        if not evaluate_prefilter(job, widened).passes:
            continue
        gain = phrase_gain(profile, phrase, pool)
        suggestions.append(TermSuggestion(phrase, gain.count, gain.count > broad_threshold, gain.samples))
    suggestions.sort(key=lambda s: (s.gain, -len(s.term), s.term))
    return suggestions[:max_suggestions]


def _day(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d") if moment else "?"


def _prefilter_stage(job: Job, profile: CandidateProfile, keywords: list[str] | None):
    decision = evaluate_prefilter(job, profile, keywords=keywords)
    if decision.passes:
        rescued = f" (rescued by {decision.rescued_by!r})" if decision.rescued_by else ""
        return decision, Stage("prefilter", True, f"matched {decision.term!r}{rescued}")
    if decision.rule is PrefilterRule.NO_POSITIVE_MATCH:
        department = "empty" if not job.department else repr(job.department)
        detail = (
            "no_positive_match: no target term appears in the title or department "
            f"(department is {department})"
        )
    elif decision.term:
        detail = f"{decision.rule.value}: matched {decision.term!r}"
    else:
        detail = decision.rule.value
    return decision, Stage("prefilter", False, detail)


def explain(
    stored: StoredJob,
    *,
    profile: CandidateProfile,
    max_age_days: int,
    source_health: dict | None,
    archive: ArchiveInfo | None,
    assessment: dict | None,
    pool: Callable[[], list[Job]],
    keywords: list[str] | None = None,
    now: datetime | None = None,
    broad_threshold: int = BROAD_TERM_THRESHOLD,
    max_suggestions: int = 5,
) -> WhyMissed:
    job = stored.job
    stages: list[Stage] = []

    collected = (
        f"status {stored.status}; first seen {_day(job.first_seen_at)}, last seen "
        f"{_day(job.last_seen_at)}, missed {stored.missing_count} run(s)"
    )
    if source_health:
        collected += (
            f"; source last run: {source_health.get('last_status')} "
            f"({source_health.get('last_job_count')} jobs)"
        )
    stages.append(Stage("collected", stored.status == "active", collected))
    stages.append(
        Stage("U.S.-eligible", bool(job.us_eligible), job.location_evidence or job.location_raw or "no evidence recorded")
    )
    recent = passes_recency(job, max_age_days, now=now)
    posted = f"posted {_day(job.posted_at)}" if job.posted_at else "no posted date (kept)"
    stages.append(Stage("recency", recent, f"{posted}; max age {max_age_days} days"))

    decision, prefilter = _prefilter_stage(job, profile, keywords)
    stages.append(prefilter)

    if archive is None:
        stages.append(Stage("in archive", None, "no search archive found to check"))
    elif archive.in_candidates:
        stages.append(Stage("in archive", True, f"yes, among the candidates in {archive.path}"))
    elif archive.source_status in ("failed", "unsupported"):
        stages.append(
            Stage(
                "in archive", False,
                f"no - {archive.path} has this source as {archive.source_status!r}, so the radar can "
                "only show this job through the stale-source fallback if it passes the filters, and "
                "that fallback never scores it",
            )
        )
    else:
        stages.append(
            Stage(
                "in archive", False,
                f"no - not among the candidates in {archive.path} (collected after it was written, "
                "or filtered out when it was built)",
            )
        )

    if assessment is None:
        stages.append(Stage("assessed", False, "not assessed (the local review has not scored it)"))
    elif assessment.get("content_hash") != job.content_hash:
        stages.append(
            Stage("assessed", False, f"score {assessment.get('score')} is stale: the posting changed since it was scored")
        )
    else:
        stages.append(Stage("assessed", True, f"score {assessment.get('score')}"))

    failing = next((s for s in stages if s.ok is False), None)
    if failing is not None:
        verdict = f"Stopped at: {failing.name} - {failing.detail}"
    elif archive is None:
        verdict = "Passes every filter. No search archive was found to check membership."
    else:
        verdict = (
            "No blocking stage found: it is in the archive and assessed, so it should appear in "
            "the radar (check its section and score)."
        )

    suggestions: list[TermSuggestion] = []
    previews: list[str] = []
    blocked: list[str] = []
    in_pool = False
    if decision.rule is PrefilterRule.NO_POSITIVE_MATCH and keywords is None:
        jobs = pool()
        in_pool = any(j.source_key == job.source_key and j.job_id == job.job_id for j in jobs)
        suggestions = suggest_terms(
            job, profile, jobs, max_suggestions=max_suggestions, broad_threshold=broad_threshold
        )
        previews = [
            f'uv run python scripts/diff_profile.py --add "target_title_terms:{s.term}"'
            for s in suggestions
        ]
        blocked = [s.name for s in stages[: stages.index(prefilter)] if s.ok is False]
    return WhyMissed(
        job.source_key, job.job_id, job.title, stages, verdict, suggestions, previews, blocked, in_pool
    )


def render_text(result: WhyMissed) -> str:
    marks = {True: "ok  ", False: "FAIL", None: "n/a "}
    lines = [f'Why was "{result.title}" ({result.source_key}:{result.job_id}) not in the radar?']
    lines += [f"  [{marks[s.ok]}] {s.name}: {s.detail}" for s in result.stages]
    lines.append(f"Verdict: {result.verdict}")
    if result.suggestions:
        lines.append("")
        scope = (
            "including this one" if result.job_in_pool
            else "this job is not among them: it is closed, ineligible or stale"
        )
        lines.append(
            "Suggested title terms (each shows how many already-rejected jobs it would admit, "
            f"{scope}):"
        )
        if result.also_blocked_by:
            lines.append(
                f"  Note: this job is also blocked by {', '.join(result.also_blocked_by)}; a title "
                "term alone will not bring it into the radar."
            )
        for number, s in enumerate(result.suggestions, 1):
            broad = "  [broad]" if s.broad else ""
            lines.append(f'  {number}. "{s.term}" admits {s.gain} job(s){broad}')
            if s.samples:
                lines.append("       e.g. " + "; ".join(s.samples))
            lines.append(f"       preview: {result.preview_commands[number - 1]}")
    lines.append("")
    lines.append(
        "Nothing was changed. To add a term, use the job-feedback skill, which previews the exact "
        "jobs gained/lost and stops for your confirmation before writing the profile."
    )
    return "\n".join(lines)


def _query_one(database_path: Path, sql: str, params: tuple) -> dict | None:
    conn = connect_readonly(database_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _archive_info(search: Path | None, keyword: str | None, job: Job) -> ArchiveInfo | None:
    try:
        path = resolve_search_path(search=search, keyword=keyword)
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # No archive to check is fine when none was asked for; an explicit --search that is
        # missing is the caller's mistake and propagates (main() reports it, exit 2).
        if search is not None:
            raise
        return None
    in_candidates = any(
        c.get("source_key") == job.source_key and c.get("job_id") == job.job_id
        for c in data.get("candidates", [])
    )
    status = next(
        (h.get("status") for h in data.get("source_health", []) if h.get("source_key") == job.source_key),
        None,
    )
    return ArchiveInfo(str(path), in_candidates, status)


def cli_why_missed(args, settings) -> int:
    if not Path(settings.database_path).exists():
        print(f"job-hunter: no database at {settings.database_path}; run a search first", file=sys.stderr)
        return 2
    try:
        return _why_missed(args, settings)
    except sqlite3.OperationalError as exc:
        print(f"job-hunter: could not read the database ({exc})", file=sys.stderr)
        return 2


def likely_sources(database_path: Path, ref: str, *, limit: int = 3) -> list[tuple[str, str, dict | None]]:
    """For a URL that matched no stored job: the sources it most likely belongs to, read-only, as
    `(source_key, why, source_health row or None)`. First the sources whose stored canonical
    URLs share the URL's host; otherwise a source whose key is one of the host's labels
    (`careers.ford.com` -> `ford`, since a careers front end often differs from the ATS host)."""
    if "://" not in ref:
        return []
    host = url_host(urlsplit(ref))
    if not host:
        return []
    conn = connect_readonly(database_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_key, canonical_url FROM jobs WHERE instr(lower(canonical_url), ?) > 0",
            (host,),
        ).fetchall()
        keys = sorted({r["source_key"] for r in rows if url_host(urlsplit(r["canonical_url"] or "")) == host})
        why = "stored jobs share this host"
        if not keys:
            labels = set(host.split("."))
            known = [r["source_key"] for r in conn.execute("SELECT source_key FROM source_health")]
            keys = sorted(k for k in known if {k, k.replace("_", ""), k.replace("_", "-")} & labels)
            why = "its key is part of this host"
        result = []
        for key in keys[:limit]:
            health = conn.execute(
                "SELECT last_status, last_attempt_at, last_success_at, last_job_count "
                "FROM source_health WHERE source_key=?", (key,),
            ).fetchone()
            result.append((key, why, dict(health) if health else None))
        return result
    finally:
        conn.close()


def _stamp(value: str | None) -> str:
    return value[:16].replace("T", " ") if value else "never"


def _why_missed(args, settings) -> int:
    ref = args.ref
    cap = 50
    matches = find_jobs(settings.database_path, ref, limit=cap + 1)
    if not matches:
        print(f"job-hunter: no stored job matches {ref!r}.", file=sys.stderr)
        print(
            "  Try the job id (e.g. 71202), source_key:job_id (e.g. ford:71202) or part of the title. "
            "A careers-site URL only matches when it carries an id this project stored.",
            file=sys.stderr,
        )
        sources = likely_sources(settings.database_path, ref)
        for key, why, health in sources:
            if health:
                state = (
                    f"last run: {health['last_status']} at {_stamp(health['last_attempt_at'])}, "
                    f"{health['last_job_count']} jobs, last success {_stamp(health['last_success_at'])}"
                )
            else:
                state = "no recorded source health"
            print(
                f"  Likely source: {key} ({why}) - {state}. If the job was never collected, check "
                f"it with `job-hunter source-test {key}`.",
                file=sys.stderr,
            )
        if not sources:
            print(
                "  If the job was never collected, check its source with `job-hunter source-test <key>`.",
                file=sys.stderr,
            )
        return 2
    if len(matches) > 1:
        count = f"more than {cap}" if len(matches) > cap else str(len(matches))
        print(
            f"job-hunter: {count} stored jobs match {ref!r}; re-run with source_key:job_id:",
            file=sys.stderr,
        )
        for number, match in enumerate(matches[:20], 1):
            print(
                f"  {number:>2}. {match.job.source_key}:{match.job.job_id}  {match.job.title}  [{match.status}]",
                file=sys.stderr,
            )
        return 2
    stored = matches[0]
    job = stored.job
    profile = load_profile()
    keywords = [t.strip() for t in args.keyword.split(",") if t.strip()] if args.keyword else None
    health = _query_one(
        settings.database_path,
        "SELECT last_status, last_job_count FROM source_health WHERE source_key=?", (job.source_key,),
    )
    assessment = _query_one(
        settings.database_path,
        "SELECT score, content_hash FROM assessments WHERE source_key=? AND job_id=?",
        (job.source_key, job.job_id),
    )
    result = explain(
        stored,
        profile=profile,
        max_age_days=settings.search.max_posting_age_days,
        source_health=health,
        archive=_archive_info(args.search, args.keyword, job),
        assessment=assessment,
        pool=lambda: rejected_pool(settings.database_path, profile, settings.search.max_posting_age_days),
        keywords=keywords,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_text(result))
    return 0
