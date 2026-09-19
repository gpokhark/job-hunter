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

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .atomic import atomic_write_text
from .collector import Collector, select_companies
from .config import CandidateProfile, CompanyConfig, Settings, load_companies, load_profile
from .models import PipelineManifest, PipelineStage, PipelineStatus
from .runlock import (
    LOCK_INHERITED_ENV,
    RunLockHeld,
    current_lock_token,
    process_start_time,
    run_lock,
)
from .search_archive import archive_path, resolve_search_path

RUNS_DIR = Path("data/runs")

# Captured at import time so `_run_stage_subprocess`'s own Popen usage can be mocked in tests
# (`monkeypatch.setattr("job_hunter.pipeline._popen", ...)`) without mutating the process-wide
# `subprocess` module -- `subprocess.Popen`/`subprocess.run` are the *same* module object
# everywhere they're imported (there's only ever one `sys.modules["subprocess"]`), so patching
# `job_hunter.pipeline.subprocess.Popen` directly would also silently break any other code this
# process calls that itself shells out via `subprocess.run`/`Popen` during the same test --
# confirmed live: `runlock.process_start_time`'s own `subprocess.run(["ps", ...])` call (used by
# `run_pipeline` itself when writing a fresh manifest) started raising `TypeError` once tests
# began mocking Popen this way, since it was hitting the same globally-patched fake.
_popen = subprocess.Popen


class _StageTimedOut(Exception):
    """Raised by `_run_stage_subprocess` in place of letting `subprocess.TimeoutExpired`
    propagate raw — carries whatever partial stdout/stderr the child produced before being
    killed, always as genuine `str` (see `_decode_timeout_output` for why that's not automatic)."""

    def __init__(self, stdout: str, stderr: str):
        self.stdout = stdout
        self.stderr = stderr
        super().__init__("pipeline stage subprocess timed out")


def _decode_timeout_output(value: str | bytes | None) -> str:
    """`subprocess.TimeoutExpired.stdout`/`.stderr` can carry raw `bytes` even when `Popen`/`run`
    was called with `text=True` — confirmed live on this project's own Python (3.12.13):
    `communicate()` collects partial output as bytes before the timeout fires, and only a
    *successful* `communicate()` call applies the text-mode decode step; the timeout path skips
    it. Passing that `bytes` value straight into `PipelineManifest.error` (a `str` field) mostly
    "works" by accident — pydantic's lax `str` validation silently decodes valid UTF-8 bytes — but
    a real kill can land mid-multi-byte-character, producing truncated, invalid UTF-8 that instead
    raises `PydanticSerializationError` out of `write_manifest()`'s `model_dump_json()` call,
    turning a clean `TIMED_OUT` finalization into an unhandled crash (confirmed with a direct
    reproduction: `TimeoutExpired.stderr` of `b"...\\xe2\\x82"` serializes fine as an *attribute*
    but blows up at JSON-dump time). `errors="replace"` never raises, at the cost of a `�`
    replacement character in place of whatever byte(s) got cut off — acceptable for an error
    message that's read by a human/agent, not parsed."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_stage_subprocess(
    cmd: list[str], *, project_root: Path, timeout: int | None, lock_inherited: bool = False
) -> subprocess.CompletedProcess[str]:
    """Runs one stage's subprocess (refilter/review/radar), applying
    `settings.pipeline.stage_timeout_seconds` (`None` means no timeout, preserving pre-timeout
    behavior). Raises `_StageTimedOut` instead of the raw `subprocess.TimeoutExpired` so callers
    have a single type to catch regardless of which stage they're running.

    Uses `Popen` directly (`start_new_session=True`) rather than the `subprocess.run(...,
    timeout=...)` convenience wrapper, specifically so a timeout can kill the child's whole
    *process group*, not just the direct child — `subprocess.run`'s own internal timeout handling
    calls `process.kill()` on the single immediate process only, even with `start_new_session=True`
    set, so any grandchild that child spawned (e.g. a future adapter/script shelling out to
    something) would otherwise survive the kill, still holding files/sockets/model connections
    (docs/agent-runtime-audit.md's "process-group cancellation" finding). On `TimeoutExpired`
    from the first `communicate(timeout=...)`, `os.killpg` targets the whole group; the process
    (or its group) may have already exited in the gap between the timeout firing and this call
    (e.g. it finished right as it was being killed), so `OSError` here is expected and swallowed,
    not a bug. A second, no-timeout `communicate()` call afterward drains whatever partial
    stdout/stderr the child produced before being killed — mirrors what `subprocess.run` did
    internally, needed here since we're no longer using it.

    `lock_inherited=True` (refilter/review only — see their own call sites) sets
    `LOCK_INHERITED_ENV` in the child's environment to the *current* value of `run_pipeline`'s own
    held `run_lock("job-hunter")` token (`current_lock_token` re-reads the live lock file rather
    than this function needing the token threaded down as a parameter — see that helper's
    docstring): `run_pipeline` already holds that lock for the whole run before this subprocess is
    ever spawned, and both of those scripts otherwise try to acquire the identically-named lock
    themselves, which would deadlock against their own parent (confirmed live) —
    `run_lock_or_inherited` on their side is what actually reads this env var, checks it against
    the lock file's own recorded token, and only then skips locking (docs/agent-runtime-audit.md's
    "lock bypass is caller-controlled" finding — a bare boolean env var, trusted on its presence
    alone, let any standalone caller skip the lock by setting it themselves). If no lock is
    actually held right now (shouldn't happen given this function's own call sites, but fail safe
    rather than crash on a `None` token), the env var is simply left unset, so the child falls
    back to acquiring the lock itself."""
    env = None
    if lock_inherited:
        token = current_lock_token("job-hunter")
        if token:
            env = {**os.environ, LOCK_INHERITED_ENV: token}
    proc = _popen(
        cmd,
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        stdout, stderr = proc.communicate()
        raise _StageTimedOut(
            _decode_timeout_output(stdout), _decode_timeout_output(stderr)
        ) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _finalize_timed_out(manifest: PipelineManifest, exc: _StageTimedOut, *, timeout: int | None) -> None:
    stderr_tail = exc.stderr.strip().splitlines()[-1] if exc.stderr.strip() else ""
    manifest.status = PipelineStatus.TIMED_OUT
    manifest.error = stderr_tail or f"stage exceeded stage_timeout_seconds ({timeout}s) and was killed"
    manifest.stage = PipelineStage.DONE
    manifest.completed_at = datetime.now(UTC)
    write_manifest(manifest)


def _result_json_path(run_id: str, stage_name: str, *, runs_dir: Path = RUNS_DIR) -> Path:
    return runs_dir / run_id / f"{stage_name}.result.json"


def _read_result_json(path: Path) -> dict | None:
    """The structured `--result-json` a stage's subprocess wrote on success, or `None` if it's
    missing or not valid JSON — a stage that exits 0 but leaves no readable result file is treated
    as a failure by callers (see their own docstrings), not silently zero-defaulted, since a
    missing file after a *successful* exit means something is wrong with the contract itself,
    not with the job data."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
    """The most recently *started* run, by each manifest's own `started_at` field — not
    filesystem mtime (docs/agent-runtime-audit.md's "run identity is not authoritative" finding).
    An imported/copied run directory (a backup, a copy between machines) or a partially-written
    manifest could otherwise become "latest" purely by having a newer mtime than the real latest
    run, even though its own recorded `started_at` is older. mtime is used only as a tiebreaker
    between two manifests with an identical `started_at` (should be rare, given `new_run_id`'s own
    uniqueness) — a manifest that fails to parse at all (corrupt content, or a foreign file that
    happens to sit at that path) is never preferred over one that parses successfully, regardless
    of either timestamp."""
    if not runs_dir.exists():
        return None
    manifest_paths = list(runs_dir.glob("*/manifest.json"))
    if not manifest_paths:
        return None

    def sort_key(path: Path) -> tuple[int, datetime, float]:
        try:
            manifest = PipelineManifest.model_validate_json(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            return (0, datetime.min.replace(tzinfo=UTC), 0.0)
        return (1, manifest.started_at, mtime)

    return max(manifest_paths, key=sort_key).parent.name


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


def _run_review_stage(
    manifest: PipelineManifest, *, project_root: Path, archive: Path, keyword: str | None, limit: int | None,
    stage_timeout_seconds: int | None,
) -> bool:
    """Runs `scripts/review_with_lm_studio.py` against `archive` as a subprocess and updates
    `manifest`'s reviewed/skipped_cached/failed/status fields from its own `--result-json` output
    (not stdout regex-parsing — see docs/agent-runtime-audit.md's "structured stage results"
    finding) — factored out of `run_pipeline` once both the live-search branch and
    `--no-scrape`'s optional `--review` step needed to run this identical subprocess-plus-read
    sequence (see `docs/pipeline-refilter-stale-source-plan.md` section 4.2). On a hard failure
    (non-zero exit, a timeout, or a missing/unparseable result file after a zero exit), this
    already finalizes the manifest itself — status (MODEL_UNAVAILABLE vs FAILED vs TIMED_OUT,
    same distinction the pre-refactor inline code made), stage DONE, completed_at, then writes
    it — since the caller has nothing more useful to do in that case; it returns False so the
    caller knows to stop immediately rather than proceed to the radar stage. On success it sets
    `manifest.status` (PARTIAL if any individual job failed, COMPLETE otherwise) but deliberately
    leaves `stage`/`completed_at`/the actual `write_manifest()` call to the caller, since what
    stage comes next (radar, or straight to done) differs between the two call sites."""
    result_json = _result_json_path(manifest.run_id, "review")
    review_cmd = [
        sys.executable, "scripts/review_with_lm_studio.py",
        "--project", str(project_root), "--input", str(archive), "--result-json", str(result_json),
    ]
    if keyword:
        review_cmd += ["--keyword", keyword]
    if limit is not None:
        review_cmd += ["--limit", str(limit)]
    try:
        proc = _run_stage_subprocess(
            review_cmd, project_root=project_root, timeout=stage_timeout_seconds, lock_inherited=True
        )
    except _StageTimedOut as exc:
        _finalize_timed_out(manifest, exc, timeout=stage_timeout_seconds)
        return False
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
    result = _read_result_json(result_json)
    if result is None:
        manifest.status = PipelineStatus.FAILED
        manifest.error = f"review exited 0 but wrote no readable result at {result_json}"
        manifest.stage = PipelineStage.DONE
        manifest.completed_at = datetime.now(UTC)
        write_manifest(manifest)
        return False
    manifest.reviewed = result.get("reviewed", 0)
    manifest.skipped_cached = result.get("skipped_cached", 0)
    manifest.failed = result.get("failed", 0)
    manifest.status = PipelineStatus.PARTIAL if manifest.failed else PipelineStatus.COMPLETE
    return True


def _run_radar_stage(
    manifest: PipelineManifest, *, project_root: Path, archive: Path, keyword: str | None,
    stage_timeout_seconds: int | None,
) -> None:
    """Runs `scripts/render_radar.py` against `archive` as a subprocess and updates
    `manifest.radar` (on success) or `manifest.status`/`manifest.error` (on failure/timeout) — the
    final stage both the live-search branch and `--no-scrape` branch share identically, factored
    out for the same reason as `_run_review_stage` above. Leaves `stage`/`completed_at`/the final
    `write_manifest()` call to the caller."""
    result_json = _result_json_path(manifest.run_id, "radar")
    radar_cmd = [
        sys.executable, "scripts/render_radar.py",
        "--project", str(project_root), "--search", str(archive), "--result-json", str(result_json),
    ]
    if keyword:
        radar_cmd += ["--keyword", keyword]
    try:
        proc = _run_stage_subprocess(radar_cmd, project_root=project_root, timeout=stage_timeout_seconds)
    except _StageTimedOut as exc:
        # Unlike `_run_review_stage`'s use of `_finalize_timed_out`, radar's own convention (see
        # its normal failure branch just below) is to set only status/error and leave
        # stage/completed_at/the final write_manifest() to the caller, which always runs
        # unconditionally right after this call regardless of outcome -- self-finalizing here
        # too would just mean writing the manifest twice.
        stderr_tail = exc.stderr.strip().splitlines()[-1] if exc.stderr.strip() else ""
        manifest.status = PipelineStatus.TIMED_OUT
        manifest.error = (
            stderr_tail or f"radar stage exceeded stage_timeout_seconds ({stage_timeout_seconds}s) and was killed"
        )
        return
    if proc.returncode != 0:
        manifest.status = PipelineStatus.FAILED
        manifest.error = (
            proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"radar exited {proc.returncode}"
        )
        return
    result = _read_result_json(result_json)
    if result is None:
        manifest.status = PipelineStatus.FAILED
        manifest.error = f"radar exited 0 but wrote no readable result at {result_json}"
        return
    manifest.radar = result.get("report_path")


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
        pid=os.getpid(),
        pid_start_time=process_start_time(os.getpid()),
        keyword=keyword,
        stage=PipelineStage.REFILTER if no_scrape else PipelineStage.SEARCH,
        status=PipelineStatus.RUNNING,
        profile_fingerprint=_fingerprint(project_root / "config" / "candidate_profile.yaml"),
        resume_fingerprint=_fingerprint(profile.resume_path),
        model=_load_model_name(project_root),
    )
    write_manifest(manifest)

    try:
        # Shared with `job-hunter cleanup --apply`, `scripts/review_with_lm_studio.py`, and
        # `scripts/refilter_archive.py`'s in-place rewrite — the operations that mutate SQLite,
        # an archive file, or assessments.json. One lock name means none of them can ever race a
        # pipeline run touching the same state (see docs/agent-runtime-audit.md's pipeline-lock
        # finding). A held lock is caught below as its own status rather than falling into the
        # generic FAILED branch, since it's an expected, actionable outcome, not an error.
        with run_lock("job-hunter"):
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
    except RunLockHeld as exc:
        manifest.status = PipelineStatus.LOCK_HELD
        manifest.error = str(exc)
        manifest.stage = PipelineStage.DONE
        manifest.completed_at = datetime.now(UTC)
        write_manifest(manifest)
        raise
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
    stage_timeout_seconds = settings.pipeline.stage_timeout_seconds
    if no_scrape:
        # No fresh `archive_path()` here — --no-scrape's whole point is re-evaluating an
        # archive that already exists, resolved the identical way review/radar already resolve
        # one (`--search`/`--keyword`, "newest overall" with neither given).
        archive = resolve_search_path(search=None, keyword=keyword)
        manifest.archive = str(archive)
        write_manifest(manifest)

        refilter_result_json = _result_json_path(manifest.run_id, "refilter")
        refilter_cmd = [
            sys.executable, "scripts/refilter_archive.py",
            "--project", str(project_root), "--search", str(archive),
            "--result-json", str(refilter_result_json),
        ]
        if keyword:
            refilter_cmd += ["--keyword", keyword]
        try:
            proc = _run_stage_subprocess(
                refilter_cmd, project_root=project_root, timeout=stage_timeout_seconds, lock_inherited=True
            )
        except _StageTimedOut as exc:
            _finalize_timed_out(manifest, exc, timeout=stage_timeout_seconds)
            return manifest
        if proc.returncode != 0:
            manifest.status = PipelineStatus.FAILED
            manifest.error = (
                proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"refilter exited {proc.returncode}"
            )
            manifest.stage = PipelineStage.DONE
            manifest.completed_at = datetime.now(UTC)
            write_manifest(manifest)
            return manifest

        refilter_result = _read_result_json(refilter_result_json)
        if refilter_result is None:
            manifest.status = PipelineStatus.FAILED
            manifest.error = f"refilter exited 0 but wrote no readable result at {refilter_result_json}"
            manifest.stage = PipelineStage.DONE
            manifest.completed_at = datetime.now(UTC)
            write_manifest(manifest)
            return manifest
        manifest.gained = refilter_result.get("gained")
        manifest.lost = refilter_result.get("lost")
        manifest.diff_report = refilter_result.get("diff_report")
        manifest.refiltered_at = refilter_result.get("refiltered_at")
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
                manifest, project_root=project_root, archive=archive, keyword=keyword, limit=limit,
                stage_timeout_seconds=stage_timeout_seconds,
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
            _run_radar_stage(
                manifest, project_root=project_root, archive=archive, keyword=keyword,
                stage_timeout_seconds=stage_timeout_seconds,
            )
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
            manifest, project_root=project_root, archive=archive, keyword=keyword, limit=limit,
            stage_timeout_seconds=stage_timeout_seconds,
        ):
            return manifest
        manifest.stage = PipelineStage.RADAR if not skip_radar else PipelineStage.DONE
        write_manifest(manifest)

    if not skip_radar:
        _run_radar_stage(
            manifest, project_root=project_root, archive=archive, keyword=keyword,
            stage_timeout_seconds=stage_timeout_seconds,
        )
        manifest.stage = PipelineStage.DONE

    manifest.completed_at = datetime.now(UTC)
    write_manifest(manifest)
    return manifest
