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
    if companies:
        company_slug = slugify("-".join(sorted(c.strip() for c in companies.split(",") if c.strip())))
        if company_slug:
            slug = f"{slug}__companies-{company_slug}"
    date_str = (now or datetime.now().astimezone()).strftime("%Y-%m-%d")
    return SEARCH_DIR / f"{slug}_{date_str}.json"


def resolve_search_path(
    *, search: Path | str | None = None, keyword: str | None = None
) -> Path:
    """Resolve which archived search file a downstream stage (review or radar) should use.

    - `search` given: used verbatim, no resolution — the caller already knows exactly which
      archive it wants (an explicit override always wins).
    - `keyword` given, no `search`: resolves to the newest data/searches/{slug}_*.json for
      that keyword's slug — this is how a stage can be pointed at *any* prior run, not just
      the most recent one overall, by name.
    - Neither given: resolves to the newest archive of any keyword — a cold-start
      convenience only ("I don't know/care which run"), never a substitute for passing
      `keyword` explicitly when the caller already knows it (e.g. a skill that just told
      job-scout which keyword to search must pass that same keyword forward, not rely on
      this default, or it risks resuming the wrong run if another search happened since).

    Raises FileNotFoundError with what's actually available rather than guessing.
    """
    if search is not None:
        path = Path(search)
        if not path.exists():
            raise FileNotFoundError(f"{path} does not exist")
        return path

    pattern = f"{slugify(keyword)}_*.json" if keyword else "*.json"
    matches = list(SEARCH_DIR.glob(pattern)) if SEARCH_DIR.exists() else []
    if not matches:
        available = sorted(p.name for p in SEARCH_DIR.glob("*.json")) if SEARCH_DIR.exists() else []
        scope = f" matching keyword {keyword!r}" if keyword else ""
        raise FileNotFoundError(
            f"No archived search found{scope} in {SEARCH_DIR}. "
            f"Available: {available or '(none)'} — run job-scout (job-hunter search --archive) first."
        )
    return max(matches, key=lambda p: p.stat().st_mtime)
