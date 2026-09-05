#!/usr/bin/env python3
"""PROTOTYPE — standalone, read-only, not wired into the pipeline.

Explores whether ranking description-only keyword matches by TF-IDF cosine similarity against
the resume can safely recover real candidates the title+department-only prefilter gate misses,
without reintroducing the boilerplate false-positive problem that gate was built to avoid.

Reads directly from the existing SQLite job store and the configured resume. Makes no writes,
and does not touch prefilter.py/collector.py/cli.py. See docs/broad-match-plan.md for the full
design and rationale.

Usage:
    uv run python scripts/prototype_tfidf_broad_match.py --keyword ADAS
    uv run python scripts/prototype_tfidf_broad_match.py --keyword ADAS,Robotics --top 15
    uv run python scripts/prototype_tfidf_broad_match.py --keyword Robotics --csv /tmp/robotics_broad.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from job_hunter.config import load_profile, load_settings
from job_hunter.storage import Storage

_TAG = re.compile(r"<[^>]+>")
_BLOCK_SPLIT = re.compile(r"</p>|<br\s*/?>|</li>|</div>|</h[1-6]>", re.I)
_WORD = re.compile(r"[a-z][a-z0-9+#./\-]{1,}")

_STOPWORDS = frozenset(
    """
    a an the and or but if then else for of to in on at by with without from as is are was were
    be been being this that these those it its it's you your we our us they their he she his her
    not no yes do does did have has had will would should could can may might must shall about
    into over under between among per via across through during before after above below up down
    out off again further here there when where why how all any both each few more most other
    some such only own same so than too very s t just don now etc
    """.split()  # noqa: SIM905 -- a plain word list reads far worse as a literal
)

_MIN_CHUNK_LEN = 40


def strip_tags_to_paragraphs(html: str | None) -> list[str]:
    """Split raw HTML into block-level text chunks (paragraph-ish), tags stripped, whitespace
    normalized. Used both for boilerplate-recurrence detection (needs chunk boundaries) and,
    joined back together, as the cleaned text fed into TF-IDF."""
    if not html:
        return []
    out = []
    for chunk in _BLOCK_SPLIT.split(html):
        text = re.sub(r"\s+", " ", _TAG.sub(" ", chunk)).strip()
        if text:
            out.append(text)
    return out


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]


def detect_boilerplate(
    jobs: list[dict[str, Any]], *, min_postings: int, threshold: float
) -> dict[str, set[str]]:
    """Per source_key, paragraphs appearing in >= threshold fraction of that source's postings —
    company-recurring boilerplate, detected by recurrence rather than HTML structure (see
    docs/broad-match-plan.md section 4.1 for why)."""
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        by_source[job["source_key"]].append(job)

    boilerplate: dict[str, set[str]] = {}
    for source_key, postings in by_source.items():
        if len(postings) < min_postings:
            continue
        doc_freq: Counter[str] = Counter()
        for job in postings:
            paragraphs = {p for p in strip_tags_to_paragraphs(job.get("description")) if len(p) >= _MIN_CHUNK_LEN}
            doc_freq.update(paragraphs)
        n = len(postings)
        boilerplate[source_key] = {p for p, count in doc_freq.items() if count / n >= threshold}
    return boilerplate


def clean_description(job: dict[str, Any], boilerplate: dict[str, set[str]]) -> str:
    paragraphs = strip_tags_to_paragraphs(job.get("description"))
    strip_set = boilerplate.get(job["source_key"], set())
    return " ".join(p for p in paragraphs if p not in strip_set)


class TfIdf:
    """Hand-rolled TF-IDF + cosine similarity — no scikit-learn/numpy needed at this corpus
    size (~10K short documents). See docs/broad-match-plan.md section 4.2."""

    def __init__(self, documents: dict[str, str]):
        doc_tokens = {doc_id: tokenize(text) for doc_id, text in documents.items()}
        n = len(doc_tokens)
        df: Counter[str] = Counter()
        for tokens in doc_tokens.values():
            df.update(set(tokens))
        self.idf = {term: math.log((1 + n) / (1 + count)) + 1.0 for term, count in df.items()}
        self.vectors = {doc_id: self._vectorize(tokens) for doc_id, tokens in doc_tokens.items()}

    def _vectorize(self, tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        vec = {}
        for term, count in tf.items():
            idf = self.idf.get(term)
            if idf is None:
                continue
            vec[term] = (1 + math.log(count)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {term: v / norm for term, v in vec.items()}

    def vectorize_query(self, text: str) -> dict[str, float]:
        return self._vectorize(tokenize(text))

    @staticmethod
    def cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
        if len(vec_b) < len(vec_a):
            vec_a, vec_b = vec_b, vec_a
        return sum(v * vec_b.get(term, 0.0) for term, v in vec_a.items())


def _is_recent(posted_at: str | None, cutoff: datetime) -> bool:
    if not posted_at:
        return True
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt >= cutoff


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keyword", required=True, help="comma-separated keyword(s), e.g. 'ADAS,Robotics'")
    parser.add_argument("--top", type=int, default=20, help="how many ranked broad-pool rows to print")
    parser.add_argument("--min-postings", type=int, default=5, help="min postings/source before boilerplate detection runs")
    parser.add_argument("--boilerplate-threshold", type=float, default=0.5, help="fraction of a source's postings a chunk must appear in to count as boilerplate")
    parser.add_argument("--csv", type=Path, default=None, help="optional path to also write the ranked broad pool as CSV")
    args = parser.parse_args()

    settings = load_settings()
    profile = load_profile()
    if not profile.resume_path or not profile.resume_path.exists():
        print("prototype: no resume found (set resume_path in candidate_profile.yaml)")
        return 2
    resume_text = profile.resume_path.read_text(encoding="utf-8")

    with Storage(settings.database_path) as storage:
        jobs = storage.export_active()

    print(f"Loaded {len(jobs)} active jobs from {settings.database_path}")

    boilerplate = detect_boilerplate(jobs, min_postings=args.min_postings, threshold=args.boilerplate_threshold)
    total_removed = 0
    total_kept = 0
    for job in jobs:
        raw_len = len(" ".join(strip_tags_to_paragraphs(job.get("description"))))
        clean_len = len(clean_description(job, boilerplate))
        total_removed += max(raw_len - clean_len, 0)
        total_kept += clean_len
    removed_pct = 100 * total_removed / (total_removed + total_kept) if (total_removed + total_kept) else 0
    print(
        f"Boilerplate stripping: {len(boilerplate)} source(s) had >= {args.min_postings} postings "
        f"and were checked; ~{removed_pct:.1f}% of total description text removed as boilerplate "
        f"(threshold={args.boilerplate_threshold:.0%})."
    )
    for source_key, chunks in sorted(boilerplate.items()):
        if chunks:
            print(f"  {source_key}: {len(chunks)} boilerplate chunk(s) detected")

    doc_id = lambda j: f"{j['source_key']}:{j['job_id']}"  # noqa: E731
    cleaned = {doc_id(j): clean_description(j, boilerplate) for j in jobs}
    tfidf = TfIdf(cleaned)
    resume_vec = tfidf.vectorize_query(resume_text)

    # Reference postings found manually in the prior investigation — printed for calibration
    # regardless of --keyword, so ranking quality is visible without digging through the DB again.
    reference = [
        ("PACCAR", "Electrical Engineer - Siemens Capital Administrator", "expected LOW (incidental 'ADAS' mention)"),
        ("Hyundai Motor America", "Senior Manager, Forensic Investigations", "expected LOW (incidental 'ADAS' mention)"),
        ("Honda Research Institute USA", "AI Research Scientist: Computational Social Systems", "expected MID-HIGH (genuine robotics-adjacent role)"),
    ]
    print("\nCalibration — known reference postings' similarity to the resume:")
    all_scores = sorted(
        ((tfidf.cosine(resume_vec, tfidf.vectors[doc_id(j)]), j) for j in jobs),
        key=lambda pair: -pair[0],
    )
    rank_by_key = {doc_id(j): i + 1 for i, (_, j) in enumerate(all_scores)}
    for company, title_prefix, expectation in reference:
        match = next((j for j in jobs if j["company"] == company and j["title"].startswith(title_prefix)), None)
        if match is None:
            print(f"  {company} — {title_prefix!r}: not found in current active jobs (may have closed since)")
            continue
        score = tfidf.cosine(resume_vec, tfidf.vectors[doc_id(match)])
        rank = rank_by_key[doc_id(match)]
        print(f"  {company} — {match['title']}: score={score:.4f}, rank={rank}/{len(jobs)} ({expectation})")

    exclude_title = [t.lower() for t in profile.exclude_title_terms]
    exclude_terms = [t.lower() for t in profile.exclude_terms]
    cutoff = datetime.now(UTC) - timedelta(days=settings.search.max_posting_age_days)

    def base_ok(job: dict[str, Any]) -> bool:
        if not job.get("us_eligible"):
            return False
        title = (job.get("title") or "").lower()
        if any(t in title for t in exclude_title):
            return False
        full = f"{job.get('title') or ''} {job.get('department') or ''} {job.get('description') or ''}".lower()
        if any(t in full for t in exclude_terms):
            return False
        return _is_recent(job.get("posted_at"), cutoff)

    keywords = [k.strip().lower() for k in args.keyword.split(",") if k.strip()]
    csv_rows: list[dict[str, Any]] = []

    for kw in keywords:
        eligible = [j for j in jobs if base_ok(j)]
        tight = []
        broad = []
        for job in eligible:
            title_dept = f"{job.get('title') or ''} {job.get('department') or ''}".lower()
            if kw in title_dept:
                tight.append(job)
                continue
            full_clean = f"{title_dept} {cleaned[doc_id(job)]}".lower()
            if kw in full_clean:
                broad.append(job)

        ranked = sorted(broad, key=lambda j: -tfidf.cosine(resume_vec, tfidf.vectors[doc_id(j)]))

        print(f"\n=== '{kw}' — tight matches: {len(tight)}, broad (description-only) matches: {len(ranked)} ===")
        print(f"{'score':>7}  {'company':<32}  {'title'}")
        for job in ranked[: args.top]:
            score = tfidf.cosine(resume_vec, tfidf.vectors[doc_id(job)])
            print(f"{score:7.4f}  {job['company'][:32]:<32}  {job['title']}")
            csv_rows.append(
                {
                    "keyword": kw,
                    "score": round(score, 4),
                    "company": job["company"],
                    "title": job["title"],
                    "source_key": job["source_key"],
                    "department": job.get("department") or "",
                    "url": job.get("canonical_url", ""),
                }
            )

    if args.csv and csv_rows:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nWrote {len(csv_rows)} row(s) to {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
