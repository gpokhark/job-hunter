from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkArrangement(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class LocationConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SponsorshipStatus(StrEnum):
    """A posting's explicit stance on visa sponsorship, when one is stated — see
    `sponsorship.py`'s module docstring. Purely informational, never a filter: a job
    scored NOT_AVAILABLE still passes every other stage exactly like any other job."""

    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    UNMENTIONED = "unmentioned"


class HealthStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class PrefilterRule(StrEnum):
    """Which check in `prefilter.py`'s `evaluate_prefilter` decided a job's pass/fail outcome —
    see `docs/profile-diff-plan.md` section 6. Short-circuit evaluation means this is the
    *decisive* check in the code's fixed precedence order, not an exhaustive list of every check
    that would also have failed."""

    NOT_US_ELIGIBLE = "not_us_eligible"
    EXCLUDE_TITLE_TERMS = "exclude_title_terms"
    EXCLUDE_TERMS = "exclude_terms"
    NO_POSITIVE_MATCH = "no_positive_match"
    SOFT_EXCLUDED = "soft_excluded"
    POSITIVE_MATCH = "positive_match"


class JobSummary(BaseModel):
    source_key: str
    source_platform: str
    company: str
    job_id: str
    title: str
    url: str
    location_raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    department: str | None = None
    employment_type: str | None = None
    posted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @field_validator("job_id", "title", "url")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class JobDetail(BaseModel):
    description: str | None = None
    location_raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    work_arrangement: WorkArrangement | None = None
    department: str | None = None
    employment_type: str | None = None
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None


class Assessment(BaseModel):
    """An LLM sub-agent's fitness verdict on one job, persisted so a later run can skip
    re-reviewing it (see `storage.py`'s `assessments` table and `job-hunter
    record-assessment`). Keyed by (source_key, job_id); `content_hash` pins it to the
    exact job content it was evaluated against — a job whose content_hash has since
    changed is treated as unassessed again, not silently reused."""

    source_key: str
    job_id: str
    company: str
    title: str
    url: str
    content_hash: str | None = None
    score: int = Field(ge=0, le=100)
    recommended: bool
    matches: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    resume_path: str | None = None
    assessed_at: datetime = Field(default_factory=utcnow)


class Job(JobSummary):
    us_eligible: bool
    location_confidence: LocationConfidence
    location_evidence: str | None = None
    visa_sponsorship: SponsorshipStatus = SponsorshipStatus.UNMENTIONED
    sponsorship_evidence: str | None = None
    description: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_evidence: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    content_hash: str | None = None
    is_new: bool = False
    is_changed: bool = False
    prior_assessment: Assessment | None = None


class JobFeedback(BaseModel):
    """A human's click-through verdict on one job from a rendered radar report — "relevant",
    "okay", or "irrelevant". Keyed by (source_key, job_id), upserted (never appended): a later
    label for the same job replaces the earlier one rather than creating a second, contradictory
    row — see `docs/feedback-exclusion-plan.md` section 8 for why this has to be upsert semantics,
    not an append-only log. Purely an input to `scripts/suggest_exclusions.py`'s suggestions;
    `prefilter.py` never reads this table directly."""

    source_key: str
    job_id: str
    company: str
    title: str
    department: str | None = None
    score: int | None = None
    label: str
    recorded_at: datetime = Field(default_factory=utcnow)


class SourceHealth(BaseModel):
    source_key: str
    company: str
    status: HealthStatus
    job_count: int = 0
    message: str | None = None
    error_type: str | None = None
    attempted_at: datetime = Field(default_factory=utcnow)


class SearchSummary(BaseModel):
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    jobs_observed: int = 0
    us_eligible: int = 0
    prefilter_candidates: int = 0
    stale_excluded: int = 0
    partial_failure: bool = False


class RunInfo(BaseModel):
    run_id: str
    started_at: datetime
    completed_at: datetime | None = None


class PipelineStage(StrEnum):
    SEARCH = "search"
    # `--no-scrape` mode's own first stage, replacing SEARCH — see pipeline.py's `run_pipeline`
    # and docs/pipeline-refilter-stale-source-plan.md section 4.2. Distinct from SEARCH rather
    # than reusing it because it runs a different subprocess (scripts/refilter_archive.py, not
    # a live Collector.search()) and populates different manifest fields (`gained`/`lost`/
    # `diff_report` instead of a freshly-collected job count) — collapsing the two into one
    # stage name would hide which of two very different operations actually produced a run's
    # `candidates` count.
    REFILTER = "refilter"
    REVIEW = "review"
    RADAR = "radar"
    DONE = "done"


class PipelineStatus(StrEnum):
    """Distinguishes *why* a pipeline run ended, mirroring the review step's own possible
    outcomes rather than collapsing everything into a bare success/failure exit code — see
    docs/agent-runtime-audit.md's "Partial review exit status" note."""

    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    NO_CANDIDATES = "no_candidates"
    MODEL_UNAVAILABLE = "model_unavailable"
    # Another job-hunter process already held the shared `run_lock("job-hunter")` (see
    # pipeline.py/cleanup.py/scripts/refilter_archive.py) — this run never started any stage.
    LOCK_HELD = "lock_held"
    # A stage's subprocess exceeded `settings.pipeline.stage_timeout_seconds` and was killed —
    # see pipeline.py's per-stage `subprocess.run(..., timeout=...)`.
    TIMED_OUT = "timed_out"


#: `job-hunter pipeline`/`pipeline-status`'s non-success exit-code set — `PARTIAL`/`NO_CANDIDATES`
#: exit 0 (both are documented, expected outcomes: a candidate list with some review failures, or
#: a genuinely empty one), everything else here exits 2. Shared between the two CLI call sites
#: (`cli.py`) so the contract can't drift between "just ran" and "polled later" the same run.
PIPELINE_NON_SUCCESS_STATUSES = frozenset(
    {
        PipelineStatus.FAILED,
        PipelineStatus.MODEL_UNAVAILABLE,
        PipelineStatus.LOCK_HELD,
        PipelineStatus.TIMED_OUT,
    }
)


class PipelineManifest(BaseModel):
    """The durable record of one `job-hunter pipeline` run, at `data/runs/<run_id>/manifest.json`
    — a machine-readable stage contract an agent (or a human) can poll instead of re-parsing
    command prose/stdout. Written by `pipeline.py`, never by a skill."""

    run_id: str
    project_root: str
    # Set once at run start (`os.getpid()`) — `pipeline-status` uses this to distinguish a
    # manifest genuinely still `RUNNING` from one whose process has died without updating it
    # (reported as `abandoned`; see cli.py's `pipeline-status` handling). `None` only for a
    # manifest written before this field existed.
    pid: int | None = None
    # `runlock.process_start_time(pid)`'s raw `ps -o lstart=` output at the moment `pid` was
    # recorded above -- a process-*identity* check, not just liveness, so `pipeline-status` can
    # tell "this exact process is still running" from "the OS reused this pid number for a
    # different, unrelated process after the original one died" (docs/agent-runtime-audit.md's
    # "PID reuse" finding). `None` for a manifest written before this field existed, or one
    # written where `ps` wasn't available -- `pipeline-status` falls back to PID-only liveness in
    # either case, never treating a missing value as evidence of anything.
    pid_start_time: str | None = None
    keyword: str | None = None
    stage: PipelineStage = PipelineStage.SEARCH
    status: PipelineStatus = PipelineStatus.RUNNING
    archive: str | None = None
    radar: str | None = None
    candidates: int | None = None
    # Populated only by --no-scrape mode's REFILTER stage, parsed from scripts/
    # refilter_archive.py's own stdout summary line the same way `reviewed`/`skipped_cached`
    # below are parsed from review_with_lm_studio.py's — see pipeline.py's
    # `_parse_refilter_output`. `gained`/`lost` name the exact vocabulary refilter_archive.py's
    # own HTML diff report already uses (not "added"/"removed" or some other synonym), so a
    # human reading a manifest and that report side by side sees the same two words for the
    # same two counts. All three stay None for a live-search (non-`--no-scrape`) run, since
    # nothing was refiltered — there was nothing to diff against.
    diff_report: str | None = None
    gained: int | None = None
    lost: int | None = None
    reviewed: int | None = None
    skipped_cached: int | None = None
    failed: int = 0
    profile_fingerprint: str | None = None
    resume_fingerprint: str | None = None
    model: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None


class SearchResult(BaseModel):
    run: RunInfo
    summary: SearchSummary
    source_health: list[SourceHealth]
    candidates: list[Job]
