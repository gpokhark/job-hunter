#!/usr/bin/env python3
"""Deterministic market-comparables lookup against job-hunter's own collected job database
(data/jobs.sqlite3) -- stdlib + sqlite3 only, a genuinely read-only connection, changes nothing.

Finds already-scraped, US-eligible postings whose title matches any of a list of keywords
(optionally scoped to specific companies/states), printing company, title, location, status,
salary range, and sponsorship stance for each match. Used by the salary-compare skill's Step 4
to find real company/role comparables for market research -- deciding which matches are genuine
peers (seniority level, metro) and writing the narrative is the skill's job; this script only
fetches candidate rows, no interpretation.

Usage:
    uv run python scripts/market_lookup.py --title-like "Validation,ADAS,Autonomy,Autonomous Vehicle"
    uv run python scripts/market_lookup.py --title-like "..." --companies caterpillar,ford,rivian,gm,slate
    uv run python scripts/market_lookup.py --title-like "..." --states MI,AZ --json
    uv run python scripts/market_lookup.py --title-like "..." --include-closed --include-no-salary
    # the canonical Title/Company/Location/Salary/Sponsorship/Job-link table -- paste straight
    # into a negotiation plan or comparison doc instead of hand-writing prose:
    uv run python scripts/market_lookup.py --title-like "..." --markdown
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from job_hunter.config import load_settings
from job_hunter.rootutil import add_project_argument, chdir_to_project_root

FIELDS = [
    "source_key", "company", "title", "city", "state", "status",
    "salary_min", "salary_max", "salary_currency", "visa_sponsorship",
    "canonical_url", "posted_at",
]


def fetch_matches(
    database_path: Path,
    title_keywords: list[str],
    *,
    companies: list[str] | None = None,
    states: list[str] | None = None,
    include_closed: bool = False,
    require_salary: bool = True,
) -> list[dict]:
    """Read-only lookup -- see diff_profile.py's `_read_only_jobs` for why `mode=ro` matters:
    Storage()'s own __init__ runs CREATE TABLE IF NOT EXISTS/_migrate()/commit() on open, which
    is idempotent but not an actual read-only guarantee for a tool whose whole premise here is
    "never touches the collector's data"."""
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        clauses = ["us_eligible = 1"]
        params: list[object] = []

        title_clauses = ["title LIKE ?" for _ in title_keywords]
        clauses.append("(" + " OR ".join(title_clauses) + ")")
        params.extend(f"%{kw}%" for kw in title_keywords)

        if companies:
            clauses.append("source_key IN (" + ",".join("?" * len(companies)) + ")")
            params.extend(companies)
        if states:
            clauses.append("state IN (" + ",".join("?" * len(states)) + ")")
            params.extend(states)
        if not include_closed:
            clauses.append("status = 'active'")
        if require_salary:
            clauses.append("salary_min IS NOT NULL AND salary_max IS NOT NULL")

        sql = f"SELECT {', '.join(FIELDS)} FROM jobs WHERE {' AND '.join(clauses)} ORDER BY company, title"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def render_text(rows: list[dict]) -> str:
    if not rows:
        return "No matches."
    lines = [f"{len(rows)} match(es):\n"]
    for r in rows:
        loc = ", ".join(x for x in (r["city"], r["state"]) if x) or "n/a"
        if r["salary_min"] is not None and r["salary_max"] is not None:
            sal = f"${r['salary_min']:,.0f}-${r['salary_max']:,.0f}"
        else:
            sal = "n/a"
        lines.append(
            f"- {r['company']} | {r['title']} | {loc} | {r['status']} | {sal} | "
            f"sponsorship: {r['visa_sponsorship']}"
        )
        lines.append(f"    {r['canonical_url']}")
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown_table(rows: list[dict]) -> str:
    """The canonical comparable-postings table format: Title | Company | Location | Salary |
    Sponsorship | Job link. Paste this straight into a negotiation plan or comparison doc instead
    of hand-writing prose -- see salary-compare's SKILL.md Step 4."""
    if not rows:
        return "No matches."
    lines = [
        "| Title | Company | Location | Salary | Sponsorship | Job link |",
        "|---|---|---|---:|---|---|",
    ]
    for r in rows:
        loc = ", ".join(x for x in (r["city"], r["state"]) if x) or "n/a"
        if r["salary_min"] is not None and r["salary_max"] is not None:
            sal = f"${r['salary_min']:,.0f}-${r['salary_max']:,.0f}"
        else:
            sal = "n/a"
        lines.append(
            f"| {_md_cell(r['title'])} | {_md_cell(r['company'])} | {_md_cell(loc)} | {sal} | "
            f"{r['visa_sponsorship']} | [link]({r['canonical_url']}) |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title-like", required=True, help="comma-separated keywords; a posting matches if its title contains ANY of them (case-insensitive)")
    ap.add_argument("--companies", help="comma-separated source_key values from config/companies.yaml (default: search every company)")
    ap.add_argument("--states", help="comma-separated 2-letter state codes (default: any)")
    ap.add_argument("--include-closed", action="store_true", help="also include postings no longer active (still useful as historical comparables)")
    ap.add_argument("--include-no-salary", action="store_true", help="also include matches with no disclosed salary_min/max (shown for title-match context, not usable as a comparable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--markdown", action="store_true", help="render as the canonical Title/Company/Location/Salary/Sponsorship/Job-link table -- paste straight into a negotiation plan instead of hand-writing prose")
    add_project_argument(ap)
    args = ap.parse_args(argv)
    chdir_to_project_root(args.project)

    settings = load_settings()
    keywords = [k.strip() for k in args.title_like.split(",") if k.strip()]
    if not keywords:
        ap.error("--title-like must have at least one keyword")
    companies = [c.strip() for c in args.companies.split(",") if c.strip()] if args.companies else None
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()] if args.states else None

    rows = fetch_matches(
        settings.database_path, keywords,
        companies=companies, states=states,
        include_closed=args.include_closed, require_salary=not args.include_no_salary,
    )

    if args.json:
        print(json.dumps(rows, indent=2))
    elif args.markdown:
        print(render_markdown_table(rows))
    else:
        print(render_text(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
