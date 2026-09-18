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

A second mode, `--no-scrape` (see `docs/pipeline-refilter-stale-source-plan.md` section 4.2),
skips the live-search stage entirely and instead re-runs `scripts/refilter_archive.py` against an
already-resolved archive — the "I edited candidate_profile.yaml, show me the report reflecting
that, without a new scrape" workflow. It's implemented as a second branch through this same
`run_pipeline()` function rather than a parallel command, since it still needs ~90% of the same
plumbing (manifest writing, subprocess supervision, path pinning) the live-search branch already
has; only its first stage differs (REFILTER instead of SEARCH), and its `--review` flag defaults
review OFF rather than ON — deliberately the opposite sense from the live-search branch's
`--skip-review` opt-out, flagged explicitly here rather than hidden, since two flags with
opposite-sense defaults depending on mode is a real (if minor) UX wrinkle worth being honest
about instead of pretending it's not there.
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
from .search_archive import archive_path, resolve_search_path

RUNS_DIR = Path("data/runs")

_REVIEW_SUMMARY_RE = re.compile(
    r"Reviewed (\d+) job\(s\); skipped (\d+) already-assessed"
)
_REVIEW_FAILURE_RE = re.compile(r"^  skipped: ", re.MULTILINE)
_RADAR_PATH_RE = re.compile(r"^Wrote (.+?) \|", re.MULTILINE)
# scripts/refilter_archive.py's own two stdout lines: a summary ("Re-filtered X -> Y: N
# candidate(s) (was M, L removed, G gained)") and, unless --no-report, a second "Wrote
# <path>" line for its HTML diff report — parsed the same way _REVIEW_SUMMARY_RE/
# _RADAR_PATH_RE above already parse their own tool's stdout, rather than having
# refilter_archive.py hand back structured data some other way (e.g. a JSON sidecar) that
# every other stage in this file would then be the only one not to use.
_REFILTER_SUMMARY_RE = re.compile(
    r"Re-filtered .+? -> .+?: \d+ candidate\(s\) \(was \d+, (\d+) removed, (\d+) gained\)"
)
_REFILTER_REPORT_RE = re.compile(r"^Wrote (.+)$", re.MULTILINE)


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


def _parse_refilter_output(stdout: str) -> tuple[int | None, int | None, str | None]:
    """(gained, lost, diff_report_path) from scripts/refilter_archive.py's own stdout — see
    `_REFILTER_SUMMARY_RE`/`_REFILTER_REPORT_RE` above. `lost` is that script's own "removed"
    count, renamed here to match `PipelineManifest.lost`/the vocabulary refilter_archive.py's
    own HTML diff report already uses (its "Retained"/"Gained"/"Lost" stat tiles), not a new
    synonym invented for the manifest. Returns (None, None, None) for `gained`/`lost` when the
    summary line isn't found at all (an unexpected stdout shape) — unlike `_parse_review_output`,
    which defaults its counts to 0, there's a real difference here between "zero gained" and
    "couldn't tell," and a manifest reader should be able to see that difference rather than
    have a parse failure silently reported as a legitimate zero."""
    match = _REFILTER_SUMMARY_RE.search(stdout)
    lost, gained = (int(match.group(1)), int(match.group(2))) if match else (None, None)
    report_match = _REFILTER_REPORT_RE.search(stdout)
    diff_report = report_match.group(1) if report_match else None
    return gained, lost, diff_report


def _run_review_stage(
    manifest: PipelineManifest, *, project_root: Path, archive: Path, keyword: str | None, limit: int | None,
) -> bool:
    """Runs `scripts/review_with_lm_studio.py` against `archive` as a subprocess and updates
    `manifest`'s reviewed/skipped_cached/failed/status fields from its own stdout — factored out
    of `run_pipeline` once both the live-search branch and `--no-scrape`'s optional `--review`
    step needed to run this identical subprocess-plus-parse sequence (see
    `docs/pipeline-refilter-stale-source-plan.md` section 4.2). On a hard failure (non-zero
    exit), this already finalizes the manifest itself — status (MODEL_UNAVAILABLE vs FAILED,
    same distinction the pre-refactor inline code made), stage DONE, completed_at, then writes
    it — since the caller has nothing more useful to do in that case; it returns False so the
    caller knows to stop immediately rather than proceed to the radar stage. On success it sets
    `manifest.status` (PARTIAL if any individual job failed, COMPLETE otherwise) but deliberately
    leaves `stage`/`completed_at`/the actual `write_manifest()` call to the caller, since what
    stage comes next (radar, or straight to done) differs between the two call sites."""
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
        manifest.status = (
            PipelineStatus.MODEL_UNAVAILABLE
            if "can't reach LM Studio" in proc.stderr
            else PipelineStatus.FAILED
        )
        manifest.error = stderr_tail or f"review exited {proc.returncode}"
        manifest.stage = PipelineStage.DONE
        manifest.completed_at = datetime.now(UTC)
        write_manifest(manifest)
        return False
    manifest.status = PipelineStatus.PARTIAL if per_job_failures else PipelineStatus.COMPLETE
    return True


def _run_radar_stage(manifest: PipelineManifest, *, project_root: Path, archive: Path, keyword: str | None) -> None:
    """Runs `scripts/render_radar.py` against `archive` as a subprocess and updates
    `manifest.radar` (on success) or `manifest.status`/`manifest.error` (on failure) — the final
    stage both the live-search branch and `--no-scrape` branch share identically, factored out
    for the same reason as `_run_review_stage` above. Leaves `stage`/`completed_at`/the final
    `write_manifest()` call to the caller."""
    radar_cmd = [
        sys.executable, "scripts/render_radar.py",
        "--project", str(project_root), "--search", str(archive),
    ]
    if keyword:
        radar_cmd += ["--keyword", keyword]
    proc = subprocess.run(radar_cmd, cwd=project_root, capture_output=True, text=True)
    if proc.returncode != 0:
        manifest.status = PipelineStatus.FAILED
        manifest.error = (
            proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"radar exited {proc.returncode}"
        )
    else:
        manifest.radar = _parse_radar_path(proc.stdout)


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
    no_scrape: bool = False,
    review: bool = False,
) -> PipelineManifest:
    """Runs one pipeline: live search -> review -> radar by default, or, with `no_scrape=True`,
    refilter (an already-resolved archive re-evaluated against SQLite + the current profile, no
    network) -> optional review (opt in via `review=True`) -> radar. See this module's docstring
    and `docs/pipeline-refilter-stale-source-plan.md` section 4.2 for the full rationale; `cli.py`
    is responsible for rejecting `--companies` together with `--no-scrape` before this is ever
    called (refiltering re-evaluates an archive's own already-attempted source scope, not a fresh
    company selection), but the check is repeated here too as a `ValueError` so a direct caller
    (a test, or any future non-CLI embedder of this function) can't silently get a
    `companies_filter` that quietly does nothing."""
    if no_scrape and companies_filter:
        raise ValueError(
            "--companies has no effect with --no-scrape — refiltering re-evaluates an "
            "already-resolved archive's own source scope (whatever it originally attempted "
            "when it was first collected), not a fresh company selection; see "
            "docs/pipeline-refilter-stale-source-plan.md section 4.2"
        )

    keywords = [term.strip() for term in keyword.split(",") if term.strip()] if keyword else None
    profile: CandidateProfile = load_profile()

    manifest = PipelineManifest(
        run_id=new_run_id(),
        project_root=str(project_root),
        keyword=keyword,
        stage=PipelineStage.REFILTER if no_scrape else PipelineStage.SEARCH,
        status=PipelineStatus.RUNNING,
        profile_fingerprint=_fingerprint(project_root / "config" / "candidate_profile.yaml"),
        resume_fingerprint=_fingerprint(profile.resume_path),
        model=_load_model_name(project_root),
    )
    write_manifest(manifest)

    try:
        return await _run_pipeline_body(
            manifest,
            settings=settings,
            project_root=project_root,
            keyword=keyword,
            keywords=keywords,
            profile=profile,
            companies_filter=companies_filter,
            limit=limit,
            new_only=new_only,
            refresh_details=refresh_details,
            max_candidates=max_candidates,
            skip_review=skip_review,
            skip_radar=skip_radar,
            no_scrape=no_scrape,
            review=review,
        )
    except Exception as exc:
        # Every failure branch inside `_run_pipeline_body` already finalizes the manifest
        # itself before returning — this except only catches what *isn't* one of those, e.g.
        # `resolve_search_path` raising `FileNotFoundError` for a --no-scrape run with no
        # matching archive, or `select_companies` raising `ValueError` for a bad --companies
        # value. Without this, the manifest this function already wrote above (status=RUNNING)
        # is left stuck that way forever — `pipeline-status` would report a run that failed
        # instantly as still in progress. Always re-raises: `cli.py`'s existing top-level
        # exception handler is what actually prints the clean error/exit code, unchanged.
        manifest.status = PipelineStatus.FAILED
        manifest.error = f"{type(exc).__name__}: {exc}"
        manifest.stage = PipelineStage.DONE
        manifest.completed_at = datetime.now(UTC)
        write_manifest(manifest)
        raise


async def _run_pipeline_body(
    manifest: PipelineManifest,
    *,
    settings: Settings,
    project_root: Path,
    keyword: str | None,
    keywords: list[str] | None,
    profile: CandidateProfile,
    companies_filter: str | None,
    limit: int | None,
    new_only: bool,
    refresh_details: bool,
    max_candidates: int | None,
    skip_review: bool,
    skip_radar: bool,
    no_scrape: bool,
    review: bool,
) -> PipelineManifest:
    """The actual stage sequencing for `run_pipeline`, split out so `run_pipeline` itself can
    wrap this whole body in one try/except that finalizes the manifest on any exception this
    body doesn't already handle itself (see `run_pipeline`'s own try/except)."""
    if no_scrape:
        # No fresh `archive_path()` here — --no-scrape's whole point is re-evaluating an
        # archive that already exists, resolved the identical way review/radar already resolve
        # one (`--search`/`--keyword`, "newest overall" with neither given).
        archive = resolve_search_path(search=None, keyword=keyword)
        manifest.archive = str(archive)
        write_manifest(manifest)

        refilter_cmd = [
            sys.executable, "scripts/refilter_archive.py",
            "--project", str(project_root), "--search", str(archive),
        ]
        if keyword:
            refilter_cmd += ["--keyword", keyword]
        proc = subprocess.run(refilter_cmd, cwd=project_root, capture_output=True, text=True)
        if proc.returncode != 0:
            manifest.status = PipelineStatus.FAILED
            manifest.error = (
                proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"refilter exited {proc.returncode}"
            )
            manifest.stage = PipelineStage.DONE
            manifest.completed_at = datetime.now(UTC)
            write_manifest(manifest)
            return manifest

        gained, lost, diff_report = _parse_refilter_output(proc.stdout)
        manifest.gained = gained
        manifest.lost = lost
        manifest.diff_report = diff_report
        # refilter_archive.py rewrites `archive` in place with its own recomputed
        # prefilter_candidates count — re-read it so manifest.candidates means the same thing
        # here it means for a live search below, rather than staying null just because this
        # branch never called Collector.search() itself.
        archive_data = json.loads(archive.read_text(encoding="utf-8"))
        manifest.candidates = archive_data.get("summary", {}).get("prefilter_candidates")

        if manifest.candidates == 0:
            manifest.status = PipelineStatus.NO_CANDIDATES
            manifest.stage = PipelineStage.DONE
            manifest.completed_at = datetime.now(UTC)
            write_manifest(manifest)
            return manifest

        if review:
            manifest.stage = PipelineStage.REVIEW
            write_manifest(manifest)
            if not _run_review_stage(
                manifest, project_root=project_root, archive=archive, keyword=keyword, limit=limit
            ):
                return manifest
        else:
            # Review defaults OFF in --no-scrape mode, opt in via --review — the opposite
            # default from the live-search branch's --skip-review opt-out (see this module's
            # docstring and section 4.2's explicit naming-asymmetry note). Candidates the
            # refilter surfaced are shown at whatever cached score they already carry (or "NR"
            # in the radar report) rather than reviewed automatically every time.
            manifest.status = PipelineStatus.PARTIAL
        manifest.stage = PipelineStage.RADAR if not skip_radar else PipelineStage.DONE
        write_manifest(manifest)

        if not skip_radar:
            _run_radar_stage(manifest, project_root=project_root, archive=archive, keyword=keyword)
            manifest.stage = PipelineStage.DONE

        manifest.completed_at = datetime.now(UTC)
        write_manifest(manifest)
        return manifest

    # --- live-search branch (unchanged behavior from before --no-scrape existed) ---
    companies: list[CompanyConfig] = select_companies(load_companies(), companies_filter)

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
        if not _run_review_stage(
            manifest, project_root=project_root, archive=archive, keyword=keyword, limit=limit
        ):
            return manifest
        manifest.stage = PipelineStage.RADAR if not skip_radar else PipelineStage.DONE
        write_manifest(manifest)

    if not skip_radar:
        _run_radar_stage(manifest, project_root=project_root, archive=archive, keyword=keyword)
        manifest.stage = PipelineStage.DONE

    manifest.completed_at = datetime.now(UTC)
    write_manifest(manifest)
    return manifest
