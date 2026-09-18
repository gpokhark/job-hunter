"""`job-hunter pipeline` — search, local-LLM review, and radar report run end to end from one
Python-owned command, writing a durable `data/runs/<run_id>/manifest.json` at every stage.

This exists so the pipeline has one machine-readable stage contract instead of only prose (the
`job-hunter` skill's `SKILL.md`) plus newest-file resolution (`search_archive.py`) — an agent (or
a human) can poll `job-hunter pipeline-status` instead of re-parsing command stdout. The skill
itself stays a thin wrapper: this module is what actually sequences search -> review -> radar,
the review/radar stages still delegate to `scripts/review_with_lm_studio.py` /
`scripts/render_radar.py` exactly as the skill's prose already documents, via subprocess — those
scripts stay the deterministic, standalone tools they already are (see CLAUDE.md's "Scoring
itself is delegated entirely to scripts/review_with_lm_studio.py"); this module does not
reimplement their logic, only calls and supervises them.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .atomic import atomic_write_text
from .collector import Collector, select_companies
from .config import CandidateProfile, CompanyConfig, Settings, load_companies, load_profile
from .models import PipelineManifest, PipelineStage, PipelineStatus
from .search_archive import archive_path

RUNS_DIR = Path("data/runs")

_REVIEW_SUMMARY_RE = re.compile(
    r"Reviewed (\d+) job\(s\); skipped (\d+) already-assessed"
)
_REVIEW_FAILURE_RE = re.compile(r"^  skipped: ", re.MULTILINE)
_RADAR_PATH_RE = re.compile(r"^Wrote (.+?) \|", re.MULTILINE)


def new_run_id(now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)).astimezone()
    return f"{moment.strftime('%Y-%m-%d-T-%H-%M-%S')}-{uuid.uuid4().hex[:8]}"


def manifest_path(run_id: str, *, runs_dir: Path = RUNS_DIR) -> Path:
    return runs_dir / run_id / "manifest.json"


def write_manifest(manifest: PipelineManifest, *, runs_dir: Path = RUNS_DIR) -> None:
    manifest.updated_at = datetime.now(UTC)
    atomic_write_text(
        manifest_path(manifest.run_id, runs_dir=runs_dir), manifest.model_dump_json(indent=2) + "\n"
    )


def read_manifest(run_id: str, *, runs_dir: Path = RUNS_DIR) -> PipelineManifest:
    path = manifest_path(run_id, runs_dir=runs_dir)
    if not path.exists():
        raise FileNotFoundError(f"no pipeline run manifest at {path}")
    return PipelineManifest.model_validate_json(path.read_text(encoding="utf-8"))


def latest_run_id(*, runs_dir: Path = RUNS_DIR) -> str | None:
    if not runs_dir.exists():
        return None
    manifests = list(runs_dir.glob("*/manifest.json"))
    if not manifests:
        return None
    return max(manifests, key=lambda p: p.stat().st_mtime).parent.name


def _fingerprint(path: Path | None) -> str | None:
    """A short, human-comparable content fingerprint — recorded on the manifest for provenance
    only (e.g. "was this run's resume the same as that other run's?"). Never used to gate or
    invalidate the assessment cache — that stays keyed on job content_hash alone, by design
    (see CLAUDE.md's "Working in this repo" and docs/SPEC.md section 8.4)."""
    if path is None or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _load_model_name(project_root: Path) -> str | None:
    config_path = project_root / "config" / "lm_studio.yaml"
    if not config_path.exists():
        return None
    import yaml

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return data.get("model") if isinstance(data, dict) else None


def _parse_review_output(stdout: str) -> tuple[int, int, int]:
    """(reviewed, skipped_cached, per_job_failures) from review_with_lm_studio.py's own stdout —
    its final summary line plus a count of the "  skipped: <error>" lines it prints for
    individual model-call failures it deliberately continues past rather than aborting on."""
    match = _REVIEW_SUMMARY_RE.search(stdout)
    reviewed, skipped_cached = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    per_job_failures = len(_REVIEW_FAILURE_RE.findall(stdout))
    return reviewed, skipped_cached, per_job_failures


def _parse_radar_path(stdout: str) -> str | None:
    match = _RADAR_PATH_RE.search(stdout)
    return match.group(1) if match else None


async def run_pipeline(
    settings: Settings,
    project_root: Path,
    *,
    keyword: str | None = None,
    companies_filter: str | None = None,
    limit: int | None = None,
    new_only: bool = False,
    refresh_details: bool = False,
    max_candidates: int | None = None,
    skip_review: bool = False,
    skip_radar: bool = False,
) -> PipelineManifest:
    keywords = [term.strip() for term in keyword.split(",") if term.strip()] if keyword else None
    profile: CandidateProfile = load_profile()
    companies: list[CompanyConfig] = select_companies(load_companies(), companies_filter)

    manifest = PipelineManifest(
        run_id=new_run_id(),
        project_root=str(project_root),
        keyword=keyword,
        stage=PipelineStage.SEARCH,
        status=PipelineStatus.RUNNING,
        profile_fingerprint=_fingerprint(project_root / "config" / "candidate_profile.yaml"),
        resume_fingerprint=_fingerprint(profile.resume_path),
        model=_load_model_name(project_root),
    )
    write_manifest(manifest)

    result = await Collector(settings, companies, profile).search(
        include_seen=not new_only,
        new_only=new_only,
        refresh_details=refresh_details,
        max_candidates=max_candidates,
        keywords=keywords,
    )
    archive = archive_path(keyword, companies=companies_filter)
    rendered = json.dumps(result.model_dump(mode="json"), indent=2, default=str, ensure_ascii=False)
    atomic_write_text(archive, rendered + "\n")
    manifest.archive = str(archive)
    manifest.candidates = result.summary.prefilter_candidates

    if manifest.candidates == 0:
        manifest.status = PipelineStatus.NO_CANDIDATES
        manifest.stage = PipelineStage.DONE
        manifest.completed_at = datetime.now(UTC)
        write_manifest(manifest)
        return manifest

    if skip_review:
        manifest.status = PipelineStatus.PARTIAL
        manifest.stage = PipelineStage.RADAR if not skip_radar else PipelineStage.DONE
        write_manifest(manifest)
    else:
        manifest.stage = PipelineStage.REVIEW
        write_manifest(manifest)
        review_cmd = [
            sys.executable, "scripts/review_with_lm_studio.py",
            "--project", str(project_root), "--input", str(archive),
        ]
        if keyword:
            review_cmd += ["--keyword", keyword]
        if limit is not None:
            review_cmd += ["--limit", str(limit)]
        proc = subprocess.run(review_cmd, cwd=project_root, capture_output=True, text=True)
        reviewed, skipped_cached, per_job_failures = _parse_review_output(proc.stdout)
        manifest.reviewed = reviewed
        manifest.skipped_cached = skipped_cached
        manifest.failed = per_job_failures
        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
            if "can't reach LM Studio" in proc.stderr:
                manifest.status = PipelineStatus.MODEL_UNAVAILABLE
            else:
                manifest.status = PipelineStatus.FAILED
            manifest.error = stderr_tail or f"review exited {proc.returncode}"
            manifest.stage = PipelineStage.DONE
            manifest.completed_at = datetime.now(UTC)
            write_manifest(manifest)
            return manifest
        manifest.status = PipelineStatus.PARTIAL if per_job_failures else PipelineStatus.COMPLETE
        manifest.stage = PipelineStage.RADAR if not skip_radar else PipelineStage.DONE
        write_manifest(manifest)

    if not skip_radar:
        radar_cmd = [
            sys.executable, "scripts/render_radar.py",
            "--project", str(project_root), "--search", str(archive),
        ]
        if keyword:
            radar_cmd += ["--keyword", keyword]
        proc = subprocess.run(radar_cmd, cwd=project_root, capture_output=True, text=True)
        if proc.returncode != 0:
            manifest.status = PipelineStatus.FAILED
            manifest.error = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"radar exited {proc.returncode}"
        else:
            manifest.radar = _parse_radar_path(proc.stdout)
        manifest.stage = PipelineStage.DONE

    manifest.completed_at = datetime.now(UTC)
    write_manifest(manifest)
    return manifest
