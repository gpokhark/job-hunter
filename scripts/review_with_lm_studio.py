#!/usr/bin/env python3
"""Review every U.S.-eligible job-hunter candidate against your resume using a local model
served by LM Studio's OpenAI-compatible API — no cloud/agent tokens spent on scoring. Sends
one job at a time, sequentially (never in parallel), and persists each verdict immediately
via the same SQLite-backed assessment store the `job-hunter` skill uses, so a later run (of
this script, or a fresh `job-hunter search`) skips any job whose content hasn't changed.

Reviews everything by default — there is no cap on how many jobs get reviewed. Grouping the
*reviewed* jobs for the user (strong matches vs. borderline, flagging standouts) is a separate
step the `job-hunter` skill does afterward, with no cap on how many it reports either; pass
--limit here only if you explicitly want to review fewer than all eligible candidates this run.

Setup: in LM Studio, Developer tab > Start Server. Then:
    cp config/lm_studio.example.yaml config/lm_studio.yaml
and edit base_url to match the server's address (same machine: http://localhost:1234/v1;
another machine on your LAN: that machine's IP instead of localhost).

Usage:
    uv run job-hunter search --json --output data/latest_search.json   # if not already run
    uv run python scripts/review_with_lm_studio.py
    uv run python scripts/assessments_to_csv.py                        # human-readable view
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from job_hunter.atomic import atomic_write_text
from job_hunter.config import load_profile, load_settings
from job_hunter.lm_studio_health import check_lm_studio, load_lm_studio_config
from job_hunter.models import Assessment
from job_hunter.rootutil import add_project_argument, chdir_to_project_root, nonneg_int
from job_hunter.runlock import RunLockHeld, run_lock_or_inherited
from job_hunter.search_archive import resolve_search_path
from job_hunter.storage import Storage

_ROOT = Path(__file__).resolve().parents[1]
_SCORING_RUBRIC_PATH = _ROOT / "skills/job-reviewer/references/scoring.md"

_SYSTEM_PROMPT = """You are a senior hiring manager and technical domain expert responsible
for hiring the role described in the JOB POSTING. Evaluate the candidate's RESUME against that
specific role as if deciding whether to advance the candidate to an interview.

Apply a strict, evidence-based standard:

1. Treat the job posting as the source of truth for the role's responsibilities, seniority,
   required qualifications, and preferred qualifications.
2. Treat the resume as the only evidence of the candidate's experience and qualifications.
3. Never infer or invent experience, skills, education, certifications, leadership scope,
   industry knowledge, or tool proficiency that the resume does not explicitly establish.
4. If a qualification is absent or unclear in the resume, label it "not evidenced." Do not
   automatically claim that the candidate lacks it.
5. Distinguish required qualifications from preferred qualifications:
   - Missing or unevidenced required qualifications must materially reduce the score.
   - Missing preferred qualifications should receive a smaller penalty.
   - A hard required qualification that is clearly unmet should normally prevent a recommendation.
6. Evaluate depth, recency, duration, scale, ownership, and demonstrated outcomes — not merely
   keyword overlap.
7. Assess whether the candidate's seniority and scope match the role. Penalize both substantial
   underqualification and material overqualification.
8. Give credit for transferable experience only when the resume contains concrete evidence that
   reasonably maps to the job requirement. State that mapping explicitly.
9. Do not award points for location — U.S. eligibility has already been established upstream.
10. Do not consider protected characteristics or make assumptions about age, gender, ethnicity,
    disability, family status, religion, or other protected traits.
11. Calibrate scores conservatively:
    - 90-100: Exceptional fit; nearly all critical requirements are directly evidenced, with
      highly relevant scope and seniority.
    - 80-89: Strong fit; most critical requirements are evidenced, with only limited gaps.
    - 75-79: Good but imperfect fit; credible interview candidate with meaningful gaps to verify.
    - 60-74: Partial fit; several important requirements or the expected scope are not evidenced.
    - 40-59: Weak fit; limited relevant overlap or major qualification gaps.
    - 0-39: Poor fit; fundamentally mismatched domain, responsibilities, or seniority.
12. Set "recommended" to true only when score is at least {threshold} and no clearly unmet
    hard requirement makes the candidate unsuitable.

Before assigning the final score, internally compare every major job requirement with resume
evidence. Do not output that internal reasoning. Return only the required JSON.

Each item in "matches" must identify the relevant job requirement and the specific supporting
resume evidence. Each item in "gaps" must identify the required or preferred qualification at
issue, whether it is missing / weaker than requested / simply not evidenced, and why it affects
fitness for this role.

Respond with ONLY a single JSON object — no prose before or after it, no markdown code fence —
matching exactly this schema:
{{"score": <integer 0-100>, "recommended": <true or false>, "matches": [<2 to 4 concise evidence-based strings>], "gaps": [<1 to 3 concise evidence-based strings>]}}

Scoring Rubric:
{rubric}
"""


class ModelVerdict(BaseModel):
    """Strict schema for a local model's raw JSON verdict — rejects out-of-range scores,
    malformed booleans (e.g. the string "false", which Python treats as truthy), and
    evidence lists outside the sizes the prompt asks for, rather than silently coercing
    them."""

    score: int = Field(ge=0, le=100)
    recommended: bool
    matches: list[str] = Field(min_length=2, max_length=4)
    gaps: list[str] = Field(min_length=1, max_length=3)

_USER_PROMPT = """RESUME:
{resume}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
URL: {url}

Description:
{description}
"""


def _extract_json(text: str) -> dict[str, Any]:
    """Local models don't always follow "JSON only" instructions perfectly — try a
    straight parse first, then fall back to pulling the first {...} block out of
    whatever surrounding prose the model added. Raises if neither works; a malformed
    verdict must never be silently stored."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"model response was not valid JSON: {text[:500]!r}") from None
    return json.loads(match.group(0))


def review_one(
    client: httpx.Client,
    config: dict[str, Any],
    *,
    resume: str,
    rubric: str,
    title: str,
    company: str,
    location: str,
    url: str,
    description: str,
    threshold: int = 75,
) -> dict[str, Any]:
    """Send exactly one job to the local model and return its parsed, validated verdict."""
    response = client.post(
        f"{config['base_url'].rstrip('/')}/chat/completions",
        json={
            "model": config.get("model", "local-model"),
            "temperature": config.get("temperature", 0.2),
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT.format(rubric=rubric, threshold=threshold),
                },
                {
                    "role": "user",
                    "content": _USER_PROMPT.format(
                        resume=resume,
                        title=title,
                        company=company,
                        location=location,
                        url=url,
                        description=description or "(no description available)",
                    ),
                },
            ],
        },
        timeout=config.get("timeout_seconds", 180),
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    raw_verdict = _extract_json(content)
    try:
        verdict = ModelVerdict.model_validate(raw_verdict)
    except ValidationError as exc:
        raise ValueError(f"model verdict failed schema validation: {exc}") from exc
    return verdict.model_dump()


def _refresh_export(storage: Storage, database_path: Path) -> None:
    rows = storage.export_assessments()
    path = database_path.parent / "assessments.json"
    atomic_write_text(path, json.dumps(rows, indent=2, default=str, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "search JSON to read; if omitted, resolved via --keyword (newest archive for "
            "that keyword's slug) or, with neither given, the newest archive overall — see "
            "docs/skill-split-plan.md section 4. An explicit --input always wins."
        ),
    )
    parser.add_argument(
        "--keyword",
        default=None,
        help=(
            "resolve --input from the newest data/searches/{slug}_*.json archive matching "
            "this keyword, instead of passing --input directly; ignored if --input is given"
        ),
    )
    parser.add_argument(
        "--companies",
        default=None,
        help=(
            "disambiguate --keyword resolution among archives sharing that keyword by "
            "--companies scope (same value originally passed to job-hunter search/pipeline "
            "--companies) — omit to resolve only among unscoped archives; ignored if --input "
            "is given"
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/lm_studio.yaml"), help="LM Studio connection config"
    )
    parser.add_argument(
        "--limit",
        type=nonneg_int,
        default=None,
        help=(
            "max NEW reviews this run (default: no cap — review every eligible candidate; "
            "the job-hunter skill reports every reviewed job scoring 50+ afterward, with no "
            "separate cap on how many get reported)"
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="re-review even jobs with a valid prior assessment"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "print the resolved input path and how many candidates are already cached vs. "
            "still to review, then exit — no model calls, no changes made"
        ),
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        default=None,
        help=(
            "also write {\"reviewed\": N, \"skipped_cached\": N, \"failed\": N} to this path on "
            "a successful run — a structured result for a caller (job-hunter pipeline) to read "
            "instead of parsing this script's own human-readable stdout. Purely additive: stdout "
            "is unchanged either way."
        ),
    )
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    args.input = resolve_search_path(search=args.input, keyword=args.keyword, companies=args.companies)

    settings = load_settings()
    config = load_lm_studio_config(args.config)
    rubric = _SCORING_RUBRIC_PATH.read_text(encoding="utf-8")

    profile = load_profile()
    if not profile.resume_path or not profile.resume_path.exists():
        print("job-hunter: no resume found (set resume_path in candidate_profile.yaml)", file=sys.stderr)
        return 2
    resume = profile.resume_path.read_text(encoding="utf-8")

    data = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = [c for c in data.get("candidates", []) if c.get("us_eligible")]
    if not candidates:
        print(f"No U.S.-eligible candidates in {args.input}.")
        _write_result_json(args.result_json, reviewed=0, skipped_cached=0, failed=0)
        return 0

    skipped_cached = 0
    with Storage(settings.database_path) as storage:
        to_review = []
        for candidate in candidates:
            source_key, job_id = candidate["source_key"], candidate["job_id"]
            content_hash = candidate.get("content_hash")
            if not args.force and storage.get_valid_assessment(source_key, job_id, content_hash):
                skipped_cached += 1
                continue
            to_review.append(candidate)
    if args.limit is not None:
        to_review = to_review[: args.limit]

    if args.status:
        print(
            f"{args.input}: {len(to_review)} remaining, {skipped_cached} already cached, "
            f"{len(candidates)} total U.S.-eligible candidates. No model calls made."
        )
        return 0

    try:
        # Shared with `job-hunter pipeline`/`job-hunter cleanup --apply`/`refilter_archive.py`'s
        # in-place rewrite (see docs/agent-runtime-audit.md's pipeline-lock finding) — one lock
        # name so a review run and any of those can never race the same SQLite/assessments state.
        # `run_lock_or_inherited` (not `run_lock`) because `job-hunter pipeline` already holds
        # this exact lock for the whole run before spawning this script as a subprocess —
        # reacquiring it here would deadlock against that parent (confirmed live).
        with run_lock_or_inherited("job-hunter"):
            return _run_review(
                to_review, skipped_cached, config, settings, profile, resume, rubric,
                result_json=args.result_json,
            )
    except RunLockHeld as exc:
        print(
            f"job-hunter: {exc} — another job-hunter run is already in progress for this "
            "project (data/assessments.json, the sequential local model, and other shared "
            "state can only be driven by one run at a time). Wait for it to finish, or remove "
            "the lock file if you're sure it's stale.",
            file=sys.stderr,
        )
        return 2


def _write_result_json(path: Path | None, *, reviewed: int, skipped_cached: int, failed: int) -> None:
    if path is None:
        return
    atomic_write_text(
        path, json.dumps({"reviewed": reviewed, "skipped_cached": skipped_cached, "failed": failed}) + "\n"
    )


def _run_review(
    to_review, skipped_cached, config, settings, profile, resume, rubric, *, result_json: Path | None = None
) -> int:
    reachable, detail = check_lm_studio(config)
    if not reachable:
        print(f"job-hunter: {detail}", file=sys.stderr)
        return 2

    with httpx.Client() as client:
        reviewed = 0
        failed = 0
        with Storage(settings.database_path) as storage:
            for candidate in to_review:
                source_key, job_id = candidate["source_key"], candidate["job_id"]
                content_hash = candidate.get("content_hash")
                print(
                    f"Reviewing [{reviewed + 1}/{len(to_review)}] "
                    f"{candidate['company']} — {candidate['title']} ..."
                )
                try:
                    verdict = review_one(
                        client,
                        config,
                        resume=resume,
                        rubric=rubric,
                        title=candidate["title"],
                        company=candidate["company"],
                        location=candidate.get("location_raw") or "",
                        url=candidate["url"],
                        description=candidate.get("description") or "",
                        threshold=profile.minimum_recommendation_score,
                    )
                except Exception as exc:  # a bad response must not stop the remaining jobs
                    print(f"  skipped: {exc}", file=sys.stderr)
                    failed += 1
                    continue
                assessment = Assessment(
                    source_key=source_key,
                    job_id=job_id,
                    company=candidate["company"],
                    title=candidate["title"],
                    url=candidate["url"],
                    content_hash=content_hash,
                    resume_path=str(profile.resume_path),
                    **verdict,
                )
                storage.upsert_assessment(assessment)
                _refresh_export(storage, settings.database_path)
                reviewed += 1
                print(f"  score={assessment.score} recommended={assessment.recommended}")

    print(f"Reviewed {reviewed} job(s); skipped {skipped_cached} already-assessed (unchanged) job(s).")
    _write_result_json(result_json, reviewed=reviewed, skipped_cached=skipped_cached, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
