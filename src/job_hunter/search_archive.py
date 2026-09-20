"""The naming/resolution convention for `job-hunter search --archive` output, shared by the
CLI (which writes archives) and by scripts/skills that need to find one afterward (which read
them). One implementation, not several copies — see docs/skill-split-plan.md section 4 for why
this replaced an earlier "maintained pointer file" idea: the archive directory's own filenames
already encode everything needed (keyword slug + date), so resolution is a pure lookup against
what's actually on disk, nothing to keep in sync or let drift.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

SEARCH_DIR = Path("data/searches")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


#: The literal marker `archive_path()` inserts between a keyword slug and a company scope's own
#: slug (see below) — `slugify()` only ever produces lowercase alphanumerics and single hyphens,
#: never underscores, so this two-underscore marker can never collide with a real keyword slug and
#: is safe to search for literally (`resolve_search_path()` does exactly that to tell a
#: company-scoped archive's filename apart from an unscoped one sharing the same keyword).
_COMPANIES_MARKER = "__companies-"


def _company_slug(companies: str | None) -> str:
    """The same sort-then-slugify-then-join `archive_path()` has always used for its own
    `__companies-{slug}` filename suffix, factored out so `resolve_search_path()` can compute the
    identical slug for a `--companies` value it's asked to resolve *by* — an empty string (not
    `None`) when `companies` is `None`/blank/only-commas-and-whitespace, matching `archive_path()`'s
    own "falls back to no scope" behavior for the same inputs."""
    if not companies:
        return ""
    return slugify("-".join(sorted(c.strip() for c in companies.split(",") if c.strip())))


def archive_path(
    keyword: str | None, *, companies: str | None = None, now: datetime | None = None
) -> Path:
    """The deterministic data/searches/{slug}_{date}.json path for --archive, computed from
    the same --keyword string passed to `search` (or "default" without one) and today's date
    in the device's local timezone (not UTC — a run late at night in a US timezone was landing
    on tomorrow's UTC date, splitting one evening's run across two archive files) — same inputs
    always produce the same path, so a caller can predict it without parsing stdout, and a
    same-day rerun with the same keyword deliberately overwrites rather than accumulating
    duplicates. `now`, when passed explicitly (e.g. by tests), is formatted using whatever
    tzinfo it already carries rather than being forced through a local conversion — only the
    no-argument production default resolves the real system-local instant.

    `companies` (the same --companies filter `search`/`pipeline` accept) is folded into the slug
    whenever it actually restricts the run — confirmed live the hard way: without this, a
    --companies-scoped run and a full/default run share the exact same filename and silently
    clobber each other with zero warning (a company-scoped `job-hunter pipeline` run overwrote a
    same-day 65-source/474-candidate archive and its radar report with a 20-candidate,
    single-company one). Sorted before slugifying so "honda,toyota" and "toyota,honda" — the same
    scope, different order — still resolve to one file, not two. Omitted (the default, and the
    overwhelming majority of real runs) leaves the filename exactly as before."""
    slug = slugify(keyword) if keyword else "default"
    company_slug = _company_slug(companies)
    if company_slug:
        slug = f"{slug}{_COMPANIES_MARKER}{company_slug}"
    date_str = (now or datetime.now().astimezone()).strftime("%Y-%m-%d")
    return SEARCH_DIR / f"{slug}_{date_str}.json"


def resolve_search_path(
    *, search: Path | str | None = None, keyword: str | None = None, companies: str | None = None
) -> Path:
    """Resolve which archived search file a downstream stage (review or radar) should use.

    - `search` given: used verbatim, no resolution — the caller already knows exactly which
      archive it wants (an explicit override always wins). `companies` is ignored in this case.
    - `keyword` given, no `search`: resolves to the newest data/searches/{slug}_*.json for
      that keyword's slug — this is how a stage can be pointed at *any* prior run, not just
      the most recent one overall, by name.
    - Neither given: resolves to the newest archive of any keyword — a cold-start
      convenience only ("I don't know/care which run"), never a substitute for passing
      `keyword` explicitly when the caller already knows it (e.g. a skill that just told
      job-scout which keyword to search must pass that same keyword forward, not rely on
      this default, or it risks resuming the wrong run if another search happened since).

    `companies` (the same --companies filter `archive_path()` folds into a scoped filename)
    disambiguates which of possibly *several* same-keyword archives to resolve — without it, a
    company-scoped and an unscoped archive sharing a keyword and day used to be indistinguishable
    by this function's own glob (`{slug}_*.json` matches both, since a scoped filename is just
    that same prefix followed by `__companies-{scope}_{date}.json`), so a standalone
    review/radar/`resolve-search` invocation could silently resolve to whichever one happened to
    have the newer mtime — confirmed as a real gap in docs/agent-runtime-audit.md. Passing the
    identical `--companies` value used to *create* the archive now resolves to exactly that scope;
    omitting it (the default) now resolves only among *unscoped* archives — `job-hunter pipeline`
    itself never needs this, since it always already knows and passes its own exact archive path
    (see `pipeline.py`) rather than resolving one.

    Raises FileNotFoundError with what's actually available rather than guessing.
    """
    if search is not None:
        path = Path(search)
        if not path.exists():
            raise FileNotFoundError(f"{path} does not exist")
        return path

    slug = slugify(keyword) if keyword else None
    company_slug = _company_slug(companies)
    if company_slug:
        pattern = f"{slug or '*'}{_COMPANIES_MARKER}{company_slug}_*.json"
        matches = list(SEARCH_DIR.glob(pattern)) if SEARCH_DIR.exists() else []
    else:
        pattern = f"{slug}_*.json" if slug else "*.json"
        candidates = list(SEARCH_DIR.glob(pattern)) if SEARCH_DIR.exists() else []
        # Excludes a company-scoped archive sharing this keyword's slug (or, with no keyword at
        # all, sharing nothing but the "*.json" glob) — see this function's own docstring.
        matches = [p for p in candidates if _COMPANIES_MARKER not in p.name]
    if not matches:
        available = sorted(p.name for p in SEARCH_DIR.glob("*.json")) if SEARCH_DIR.exists() else []
        scope = f" matching keyword {keyword!r}" if keyword else ""
        scope += f" and --companies {companies!r}" if company_slug else ""
        raise FileNotFoundError(
            f"No archived search found{scope} in {SEARCH_DIR}. "
            f"Available: {available or '(none)'} — run job-scout (job-hunter search --archive) first."
        )
    return max(matches, key=lambda p: p.stat().st_mtime)
