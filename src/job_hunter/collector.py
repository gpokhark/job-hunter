from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from .adapters import adapter_class
from .config import CandidateProfile, CompanyConfig, Settings
from .health import detect_count_anomaly
from .location import evaluate_location
from .models import HealthStatus, Job, RunInfo, SearchResult, SearchSummary, SourceHealth
from .normalizer import description_hash
from .prefilter import is_recent, passes_prefilter, passes_recency
from .sponsorship import evaluate_sponsorship
from .storage import Storage

LOGGER = logging.getLogger(__name__)


class Collector:
    def __init__(
        self, settings: Settings, companies: list[CompanyConfig], profile: CandidateProfile
    ):
        self.settings = settings
        self.companies = companies
        self.profile = profile

    async def search(
        self,
        *,
        include_seen: bool = True,
        new_only: bool = False,
        refresh_details: bool = False,
        max_candidates: int | None = None,
        keywords: list[str] | None = None,
    ) -> SearchResult:
        started = datetime.now(UTC)
        run_id = str(uuid4())
        limits = httpx.Limits(
            max_connections=self.settings.collection.max_connections,
            max_keepalive_connections=self.settings.collection.max_keepalive_connections,
        )
        timeout = httpx.Timeout(self.settings.collection.timeout_seconds)
        semaphore = asyncio.Semaphore(self.settings.collection.max_concurrent_sources)
        with Storage(self.settings.database_path) as storage:
            storage.begin_run(run_id, started)
            async with httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=True,
                headers={"User-Agent": self.settings.collection.user_agent},
            ) as client:
                tasks = [
                    self._collect_source(company, client, storage, semaphore, refresh_details)
                    for company in self.companies
                ]
                results = await asyncio.gather(*tasks)
            health = [item[0] for item in results]
            jobs = [job for _, source_jobs in results for job in source_jobs]
            # Attach a prior LLM assessment when the job's content hasn't changed since
            # it was reviewed, so the skill can skip re-reviewing it (and spending
            # tokens on it again) without any extra round-trip — the candidate already
            # carries its own verdict. A changed content_hash means the posting was
            # edited since that review, so it's treated as unassessed again.
            assessments = storage.all_assessments()
            for job in jobs:
                prior = assessments.get((job.source_key, job.job_id))
                if prior and prior.content_hash == job.content_hash:
                    job.prior_assessment = prior
            candidates = [
                job for job in jobs if passes_prefilter(job, self.profile, keywords=keywords)
            ]
            max_age_days = self.settings.search.max_posting_age_days
            stale_excluded = sum(not passes_recency(job, max_age_days) for job in candidates)
            candidates = [job for job in candidates if passes_recency(job, max_age_days)]
            if new_only:
                candidates = [job for job in candidates if job.is_new]
            elif not include_seen:
                candidates = [job for job in candidates if job.is_new or job.is_changed]
            # No cap by default: every candidate that clears passes_prefilter/passes_recency
            # is passed on for LLM assessment, ranked newest-first (so an interrupted
            # local-LLM review has already covered the freshest postings). There used to be
            # a default recommendation.max_results*3 cap here, back when passes_prefilter's
            # loose description-wide matching routinely passed 70%+ of all U.S.-eligible
            # jobs — that cap silently discarded most of what it admitted, using a coarse
            # keyword-count heuristic (relevance_score) as the tiebreaker for what survived.
            # Now that the gate itself is precise (see passes_prefilter's docstring),
            # capping is opt-in only, via --max-candidates.
            candidates.sort(
                key=lambda job: (
                    not (job.is_new or job.is_changed),
                    -(job.posted_at.timestamp() if job.posted_at else 0),
                    job.title,
                )
            )
            if max_candidates is not None:
                candidates = candidates[:max_candidates]
            succeeded = sum(
                item.status in {HealthStatus.OK, HealthStatus.WARNING} for item in health
            )
            summary = SearchSummary(
                sources_attempted=len(health),
                sources_succeeded=succeeded,
                sources_failed=len(health) - succeeded,
                jobs_observed=len(jobs),
                us_eligible=sum(job.us_eligible for job in jobs),
                prefilter_candidates=len(candidates),
                stale_excluded=stale_excluded,
                partial_failure=0 < succeeded < len(health),
            )
            completed = datetime.now(UTC)
            status = (
                "success"
                if succeeded == len(health)
                else "partial_failure"
                if succeeded
                else "failed"
            )
            storage.finish_run(
                run_id,
                status=status,
                attempted=len(health),
                succeeded=succeeded,
                observed=len(jobs),
                candidates=len(candidates),
            )
            return SearchResult(
                run=RunInfo(run_id=run_id, started_at=started, completed_at=completed),
                summary=summary,
                source_health=health,
                candidates=candidates,
            )

    async def _collect_source(
        self,
        company: CompanyConfig,
        client: httpx.AsyncClient,
        storage: Storage,
        semaphore: asyncio.Semaphore,
        refresh_details: bool,
    ) -> tuple[SourceHealth, list[Job]]:
        async with semaphore:
            adapter = adapter_class(company.adapter)(
                company,
                client,
                self.settings.collection,
                self.settings.search.max_posting_age_days,
            )
            previous_count = storage.previous_job_count(company.key)
            try:
                summaries = await adapter.fetch_summaries()
                health = SourceHealth(
                    source_key=company.key,
                    company=company.company,
                    status=HealthStatus.OK,
                    job_count=len(summaries),
                )
                health = detect_count_anomaly(
                    health, previous_count, int(company.config.get("anomaly_minimum_previous", 20))
                )
                # storage.get_job is a synchronous, in-process sqlite read with no
                # awaits of its own — safe to call up front for every summary before
                # any concurrent detail-fetching starts, since nothing here writes yet.
                priors = [storage.get_job(s.source_key, s.job_id) for s in summaries]
                detail_semaphore = asyncio.Semaphore(self.settings.collection.max_concurrent_details)

                max_age_days = self.settings.search.max_posting_age_days

                async def _detail_for(summary, prior):
                    should_detail = refresh_details or prior is None or prior["description"] is None
                    if (
                        should_detail
                        and not refresh_details
                        and not is_recent(summary.posted_at, max_age_days)
                    ):
                        # A listing-level date already proves this job is stale; it will
                        # be excluded by passes_recency regardless of its description, so
                        # skip the wasted detail fetch (this is what actually made large
                        # full-catalog sources like Apple/Ford/Stellantis slow — most of
                        # their total job count is older than the recency cutoff).
                        should_detail = False
                    if not should_detail:
                        return None
                    # Ambiguous remote/detail location must also be inspected before exclusion.
                    async with detail_semaphore:
                        return await adapter.fetch_detail(summary)

                details = await asyncio.gather(
                    *(_detail_for(summary, prior) for summary, prior in zip(summaries, priors, strict=True))
                )
                jobs: list[Job] = []
                for summary, prior, detail in zip(summaries, priors, details, strict=True):
                    initial = evaluate_location(
                        summary.location_raw,
                        country=summary.country,
                        state=summary.state,
                        arrangement=summary.work_arrangement,
                    )
                    description = detail.description if detail else (prior["description"] if prior else None)
                    sponsorship = evaluate_sponsorship(description)
                    decision = (
                        evaluate_location(
                            (
                                detail.location_raw
                                if detail and detail.location_raw
                                else summary.location_raw
                            ),
                            country=(
                                detail.country if detail and detail.country else summary.country
                            ),
                            state=(detail.state if detail and detail.state else summary.state),
                            description=description,
                            arrangement=(
                                detail.work_arrangement if detail else summary.work_arrangement
                            ),
                        )
                        if detail
                        else initial
                    )
                    values = summary.model_dump(exclude={"raw"})
                    values.update(
                        city=(detail.city if detail and detail.city else summary.city),
                        state=decision.state or summary.state,
                        country=decision.country or summary.country,
                        work_arrangement=decision.arrangement,
                        posted_at=(detail.posted_at if detail and detail.posted_at else summary.posted_at),
                    )
                    job = Job(
                        **values,
                        us_eligible=decision.us_eligible,
                        location_confidence=decision.confidence,
                        location_evidence=decision.evidence,
                        visa_sponsorship=sponsorship.status,
                        sponsorship_evidence=sponsorship.evidence,
                        description=description,
                        salary_min=detail.salary_min if detail else None,
                        salary_max=detail.salary_max if detail else None,
                        salary_currency=detail.salary_currency if detail else None,
                        content_hash=description_hash(description),
                        last_seen_at=datetime.now(UTC),
                    )
                    jobs.append(storage.upsert_job(job))
                if health.status == HealthStatus.OK:
                    stale_before = datetime.now(UTC) - timedelta(days=max_age_days)
                    storage.mark_missing(
                        company.key, [job.job_id for job in jobs], stale_before=stale_before
                    )
                storage.update_health(health)
                return health, jobs
            except Exception as exc:
                LOGGER.exception("Source %s failed", company.key)
                health = SourceHealth(
                    source_key=company.key,
                    company=company.company,
                    status=HealthStatus.UNSUPPORTED
                    if company.adapter == "unsupported"
                    else HealthStatus.FAILED,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                storage.update_health(health)
                return health, []
            finally:
                await adapter.aclose()


def select_companies(companies: list[CompanyConfig], csv: str | None) -> list[CompanyConfig]:
    enabled = [company for company in companies if company.enabled]
    if not csv:
        return enabled
    requested = {key.strip().lower() for key in csv.split(",") if key.strip()}
    by_key = {company.key.lower(): company for company in companies}
    unknown = requested - by_key.keys()
    if unknown:
        raise ValueError(f"Unknown companies: {', '.join(sorted(unknown))}")
    return [by_key[key] for key in requested]
