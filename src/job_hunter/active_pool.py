"""SQLite's currently-active, US-eligible job pool, scoped and filtered for reuse by more than
one caller — lifted out of `scripts/refilter_archive.py`'s originally-private `_active_jobs()`
into the installed `job_hunter` package (see `docs/pipeline-refilter-stale-source-plan.md`
section 4.4) once a *second* caller needed the identical query/filtering logic.

That second caller is `scripts/render_radar.py`'s stale-source-collection fallback (plan section
4.3): when a source fails to scrape on a given run, the radar report still shows that source's
last-known-good jobs rather than silently showing zero, and answering "what does this one failed
source's current picture look like" is exactly the same question `refilter_archive.py` already
asks across every source in an archive's scope, just narrowed to one `source_key` at a time. A
scripts-importing-scripts chain already exists for this kind of reuse in this codebase (
`scripts/diff_profile.py` imports directly from `scripts/render_radar.py`), so reaching into
`refilter_archive.py`'s private internals from `render_radar.py` wouldn't have been unprecedented
— but a query this generic (not tied to either script's own report-rendering concerns) belongs in
`job_hunter` proper instead, where it's directly unit-testable alongside everything else in
`tests/` rather than only reachable by the `sys.path.insert(...)` every `scripts/*.py` test file
already has to do to import its script under test.

Two functions are exported, matching the two different things a caller of the old `_active_jobs()`
actually needed from it:

- `raw_active_jobs()` — every active/US-eligible job in SQLite, optionally scoped to a set of
  source keys, with **no** prefilter/recency applied yet. `refilter_archive.py`'s own `refilter()`
  needs exactly this shape (unfiltered) because it has to count *why* a job didn't survive
  (`stale_excluded` specifically means "failed recency", not "failed prefilter for some other
  reason") — a fully-filtered single-source list can't reconstruct that per-reason breakdown once
  the excluded jobs are already gone from it.
- `source_jobs()` — one source's current active/US-eligible/prefilter-passing/recency-passing
  jobs, built on top of `raw_active_jobs()`. This is the one `render_radar.py`'s stale-source
  fallback actually calls: it never needs `refilter_archive.py`'s per-reason exclusion count, only
  "what would show up for this one source today," so the fully-filtered shape is the right one for
  it to depend on directly instead of re-deriving the same two checks (`passes_prefilter`,
  `passes_recency`) itself a third time.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from .config import CandidateProfile
from .models import Job
from .prefilter import passes_prefilter, passes_recency
from .storage import Storage

# jobs.canonical_url -> Job.url is the one required rename; every other column already lines up
# with a Job field by name. Identical allowlist to scripts/diff_profile.py's own `_JOB_COLUMNS`
# (duplicated there, not imported from here) — this project's existing convention for these
# small SQLite-row-shaping helpers is per-module self-containment (see CLAUDE.md's
# scripts/refilter_archive.py section), which this module doesn't change; it only relocates
# refilter_archive.py's own copy so a second, non-script caller can reach it without importing a
# sibling script.
_JOB_COLUMNS = (
    "source_key", "company", "job_id", "source_platform", "title",
    "location_raw", "city", "state", "country", "work_arrangement",
    "us_eligible", "location_confidence", "location_evidence",
    "visa_sponsorship", "sponsorship_evidence", "department",
    "employment_type", "posted_at", "description", "salary_min",
    "salary_max", "salary_currency", "salary_evidence", "content_hash", "first_seen_at",
    "last_seen_at",
)


def _row_to_job(row: sqlite3.Row) -> Job:
    data = {column: row[column] for column in _JOB_COLUMNS}
    data["url"] = row["canonical_url"]
    return Job(**data)


def connect_readonly(database_path: Path) -> sqlite3.Connection:
    """A genuinely read-only connection (`mode=ro`): no directory creation, no WAL pragma, no
    schema creation and no migrations, unlike `Storage(...)`. Diagnostics use this so that
    asking "why" never changes the database it is asking about."""
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def raw_active_jobs(
    database_path: Path, source_scope: set[str] | None = None, *, readonly: bool = False
) -> list[Job]:
    """Every currently-active, US-eligible job in SQLite, optionally restricted to a source-key
    scope (`None` means every source currently sitting in the database, not just one company) —
    no prefilter or recency applied. Attaches `prior_assessment` exactly like a live search's
    `collector.py` does, and exactly like the old `_active_jobs()` always has: only when the
    stored assessment's own `content_hash` still matches the job's *current* one, so a job whose
    posting changed since it was last reviewed is treated as unassessed again rather than served
    a stale verdict.

    `source_scope`, when given, is pushed into the SQL `WHERE` clause rather than fetched-then-
    filtered-in-Python — `render_radar.py`'s stale-source fallback calls this once per `failed`
    source (`source_jobs()` below), and this table is the one CLAUDE.md itself documents as
    230MB, 98.5% of it `jobs.description` text; pulling every active row across every source into
    Python only to discard all but one source's worth per call would mean N full-table-plus-
    description scans for N failed sources in a single render, for no reason a `WHERE
    source_key IN (...)` can't avoid outright. An empty (but non-`None`) `source_scope` is a
    genuine "restrict to nothing" (see `refilter_archive.py`'s `_successful_source_scope`
    docstring — every attempted source having failed is a known scope of zero, not an unknown
    one) and short-circuits before touching SQLite at all, since an empty SQL `IN ()` is invalid
    syntax, not merely slow."""
    if source_scope is not None and not source_scope:
        return []
    sql, params = "SELECT * FROM jobs WHERE status='active' AND us_eligible=1", ()
    if source_scope is not None:
        sql += f" AND source_key IN ({','.join('?' for _ in source_scope)})"
        params = tuple(source_scope)
    if readonly:
        # `readonly=True` (the why-missed / near-misses diagnostics, docs/SPEC.md 7.5) reads the
        # same rows through a `mode=ro` connection; `Storage(...)` would create/migrate the schema.
        conn = connect_readonly(database_path)
        try:
            rows = conn.execute(sql, params).fetchall()
            assessments = {
                (row["source_key"], row["job_id"]): Storage._row_to_assessment(row)
                for row in conn.execute("SELECT * FROM assessments").fetchall()
            }
        finally:
            conn.close()
    else:
        with Storage(database_path) as storage:
            rows = storage.connection.execute(sql, params).fetchall()
            assessments = storage.all_assessments()
    jobs = []
    for row in rows:
        job = _row_to_job(row)
        prior = assessments.get((job.source_key, job.job_id))
        if prior and prior.content_hash == job.content_hash:
            job.prior_assessment = prior
        jobs.append(job)
    return jobs


def source_jobs(
    database_path: Path,
    source_key: str,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    keywords: list[str] | None = None,
    now: datetime | None = None,
) -> list[Job]:
    """One source's current active, US-eligible, prefilter-passing, recency-passing jobs — the
    exact pool `render_radar.py`'s stale-source-collection fallback needs to answer "what does
    this one failed source's last-known-good picture look like today" (see
    `docs/pipeline-refilter-stale-source-plan.md` section 4.3), without pulling in every other
    source's jobs the way `raw_active_jobs()`'s unscoped/multi-source shape would. Applies the
    same two checks `scripts/refilter_archive.py`'s own `refilter()` already applies to its
    (multi-source) pool: `passes_prefilter` against the *current* `profile` (or a `keywords`
    override, meaning exactly what it means everywhere else in this project — a full replacement
    of the profile's own positive-match terms, not a narrowing of them), then `passes_recency`
    against `max_age_days`. A source down long enough will eventually show zero fallback jobs
    here (every one of them aged past the recency cutoff) while the caller can still report the
    source itself as failed — confirmed as the intended, consistent behavior, not a bug to guard
    against (see the plan's section 3, decision 2)."""
    now = now or datetime.now(UTC)
    jobs = raw_active_jobs(database_path, {source_key})
    kept = []
    for job in jobs:
        if not passes_prefilter(job, profile, keywords=keywords):
            continue
        if not passes_recency(job, max_age_days, now=now):
            continue
        kept.append(job)
    return kept


@dataclass(frozen=True)
class StoredJob:
    """A stored job plus the two storage columns the `Job` model does not carry."""

    job: Job
    status: str
    missing_count: int


_SOURCE_AND_ID = re.compile(r"^([A-Za-z0-9_]+):([^/\s].*)$")
# Query parameters that only track how a link was shared; never part of a job's identity.
_TRACKING_PARAM = re.compile(r"^(?:utm_|mc_)|^(?:gclid|fbclid|msclkid)$", re.IGNORECASE)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_QUERY_ID = re.compile(r"[A-Za-z0-9_-]{4,}")
_ORDER = " ORDER BY source_key, job_id"


def url_host(parts) -> str:
    """Lower-cased host of a `urlsplit` result, without a leading `www.`."""
    host = (parts.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _normalized_url(url: str, *, with_query: bool) -> str:
    """Scheme/host lower-cased, `www.` and trailing `/` dropped, fragment dropped, and either
    the query dropped or reduced to its sorted non-tracking parameters."""
    parts = urlsplit(url.strip())
    base = f"{parts.scheme.lower()}://{url_host(parts)}{parts.path.rstrip('/')}"
    if not with_query:
        return base
    pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAM.match(k)
    )
    return f"{base}?{urlencode(pairs)}" if pairs else base


def _has_digit(token: str) -> bool:
    return any(c.isdigit() for c in token)


_WORKDAY_TAIL = re.compile(r"[A-Za-z]{1,4}-?\d[\d-]*")
_DIGITS_DASH_DIGITS = re.compile(r"\d{4,}-\d+")


def distinctive_id_tokens(url: str) -> set[str]:
    """The id tokens of `url` whose shape alone identifies one posting: a UUID, a Workday tail
    (`_R0000391568`, `_JR102607`, `_R-097854-1`) or an Apple-style `digits-digits` id. Only these
    are trusted when no stored job shares the URL's host; a plain number (`12345`, `?page=12345`)
    could be any source's id."""
    segment = next((s for s in reversed(urlsplit(url.strip()).path.split("/")) if s and _has_digit(s)), None)
    if not segment:
        return set()
    segment = re.sub(r"\.(?:html?|aspx?|php)$", "", segment, flags=re.IGNORECASE)
    found = set(_UUID.findall(segment))
    if "_" in segment:
        tail = segment.rsplit("_", 1)[1]
        if _WORKDAY_TAIL.fullmatch(tail) or _DIGITS_DASH_DIGITS.fullmatch(tail):
            found.add(tail)
    leading = _DIGITS_DASH_DIGITS.match(segment)
    if leading:
        found.add(leading.group())
    return found


def url_id_tokens(url: str) -> tuple[list[str], list[str]]:
    """Id candidates carried by a job URL, as `(strong, weak)`, each in priority order.

    Strong: non-tracking query values (`?gh_jid=`, `?reqId=`, `?opportunityId=`); the id segment
    (the *last* path segment containing a digit; earlier digit runs are shared location/category
    ids) as a whole (`19528`, `200462446-0836`, a UUID); its Workday tail after the last `_`
    (`R0000391568`, `JR102607`, `R-097854-1`); any UUID in it; a leading `digits-digits` id.
    Weak: the alphanumeric runs inside the id segment (`REF1018E`, `0836`), which may be shared
    by many postings, so callers only trust them on the same host and when unique per source.
    Every token is >= 4 characters and contains a digit."""
    parts = urlsplit(url.strip())
    strong: list[str] = []
    weak: list[str] = []

    def add(bucket: list[str], token: str) -> None:
        if len(token) >= 4 and _has_digit(token) and token not in strong and token not in weak:
            bucket.append(token)

    for key, value in parse_qsl(parts.query):
        if not _TRACKING_PARAM.match(key) and _QUERY_ID.fullmatch(value.strip()):
            add(strong, value.strip())
    segment = next((s for s in reversed(parts.path.split("/")) if s and _has_digit(s)), None)
    if segment:
        segment = re.sub(r"\.(?:html?|aspx?|php)$", "", segment, flags=re.IGNORECASE)
        add(strong, segment)
        if "_" in segment:
            add(strong, segment.rsplit("_", 1)[1])
        for uuid in _UUID.findall(segment):
            add(strong, uuid)
        leading = re.match(r"\d{4,}-\d+", segment)
        if leading:
            add(strong, leading.group())
        for run in re.findall(r"[A-Za-z0-9]+", segment):
            add(weak, run)
    return strong, weak


def _full_rows(conn: sqlite3.Connection, rowids: list[int], limit: int) -> list[sqlite3.Row]:
    if not rowids:
        return []
    marks = ",".join("?" for _ in rowids)
    return conn.execute(
        f"SELECT * FROM jobs WHERE rowid IN ({marks}){_ORDER} LIMIT ?", (*rowids, limit)
    ).fetchall()


def _url_lookups(conn: sqlite3.Connection, ref: str, limit: int):
    """URL resolution, most exact first (see `find_jobs`). Never falls through to titles."""
    ref_parts = urlsplit(ref)
    host = url_host(ref_parts)
    same_host_rows = conn.execute(
        "SELECT rowid, source_key, job_id, canonical_url FROM jobs WHERE instr(lower(canonical_url), ?) > 0",
        (host,),
    ).fetchall() if host else []
    # (a) the same URL once normalized, with its non-tracking query. Then the query-less form,
    # but only for a ref that is genuinely bare (no non-tracking query left: `/jobs?gh_jid=9` must
    # never resolve to a stored `/jobs?gh_jid=1`) and only when that identifies a single job.
    bare_ref = _normalized_url(ref, with_query=True) == _normalized_url(ref, with_query=False)
    for with_query in (True, False) if bare_ref else (True,):
        wanted = _normalized_url(ref, with_query=with_query)
        hits = [r["rowid"] for r in same_host_rows if _normalized_url(r["canonical_url"], with_query=with_query) == wanted]
        if hits and (with_query or len(hits) == 1):
            yield _full_rows(conn, hits, limit)
    strong, weak = url_id_tokens(ref)

    def on_same_host(row) -> bool:
        return url_host(urlsplit(row["canonical_url"] or "")) == host

    # On a host no stored job uses, a plain number or query value could be any source's id, so
    # only distinctive shapes (UUID, Workday tail, digits-digits) may match there.
    if not any(on_same_host(r) for r in same_host_rows):
        distinctive = distinctive_id_tokens(ref)
        strong = [t for t in strong if t in distinctive]
        weak = []

    # (b) an exact stored job_id. A weak token counts only on the same host; when a strong token
    # is a job_id on several sources, the ones on the URL's own host win.
    for token, weak_token in [(t, False) for t in strong] + [(t, True) for t in weak]:
        rows = conn.execute(
            "SELECT rowid, source_key, job_id, canonical_url FROM jobs WHERE job_id=?", (token,)
        ).fetchall()
        local = [r for r in rows if on_same_host(r)]
        rows = local if (weak_token or local) else rows
        if rows:
            yield _full_rows(conn, [r["rowid"] for r in rows], limit)
            return
    # (c) the token is an id token of a stored canonical_url (same extraction on both sides, so
    # it is always bounded by non-alphanumerics). A token matching several jobs of one source is
    # a shared location/category/date segment, not an id, and is ignored for that source.
    for token, weak_token in [(t, False) for t in strong] + [(t, True) for t in weak]:
        candidates = conn.execute(
            "SELECT rowid, source_key, job_id, canonical_url FROM jobs WHERE instr(canonical_url, ?) > 0",
            (token,),
        ).fetchall()
        by_source: dict[str, list[int]] = {}
        for row in candidates:
            stored_strong, stored_weak = url_id_tokens(row["canonical_url"] or "")
            local = on_same_host(row)
            if weak_token and not local:
                continue
            if token in stored_strong or (local and token in stored_weak):
                by_source.setdefault(row["source_key"], []).append(row["rowid"])
        hits = [ids[0] for ids in by_source.values() if len(ids) == 1]
        if hits:
            yield _full_rows(conn, hits, limit)
            return


def _lookups(conn: sqlite3.Connection, ref: str, limit: int):
    """Successive attempts to resolve `ref`; `find_jobs` returns the first non-empty one."""
    if "://" not in ref:
        match = _SOURCE_AND_ID.match(ref)
        if match:
            yield conn.execute(
                "SELECT * FROM jobs WHERE source_key=? AND job_id=?", (match[1], match[2])
            ).fetchall()
    if ref.lower().startswith(("http://", "https://")):
        yield from _url_lookups(conn, ref, limit)
        return
    if " " not in ref:
        yield conn.execute(f"SELECT * FROM jobs WHERE job_id=?{_ORDER} LIMIT ?", (ref, limit)).fetchall()
    yield conn.execute(
        "SELECT * FROM jobs WHERE instr(lower(title), ?) > 0 "
        "ORDER BY last_seen_at DESC, source_key, job_id LIMIT ?",
        (ref.lower(), limit),
    ).fetchall()


def find_jobs(database_path: Path, ref: str, *, limit: int = 50) -> list[StoredJob]:
    """Resolve a user's reference to stored jobs (any status, any eligibility), read-only.

    Order, first non-empty wins: `source_key:job_id`; for a URL (never falls through to a title
    match): (a) the stored `canonical_url` equal after normalizing (case of scheme/host, `www.`,
    trailing `/`, fragment, tracking query params; then query-less if that is unique), (b) an id
    token from its query values or last digit-bearing path segment equal to a stored `job_id`,
    (c) that token equal to an id token of a stored `canonical_url` (unique within its source);
    a bare `job_id`; finally a case-insensitive title substring (`instr`, so `%`/`_` are
    literal). A careers-site URL whose ids were never stored legitimately resolves to nothing."""
    ref = ref.strip()
    if not ref:
        return []
    conn = connect_readonly(database_path)
    try:
        for rows in _lookups(conn, ref, limit):
            if rows:
                return [StoredJob(_row_to_job(r), r["status"], r["missing_count"]) for r in rows]
        return []
    finally:
        conn.close()
