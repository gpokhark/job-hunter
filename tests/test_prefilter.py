from datetime import UTC, datetime, timedelta

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.prefilter import passes_prefilter, passes_recency, relevance_score


def job(**updates):
    values = dict(
        source_key="x",
        source_platform="test",
        company="Acme",
        job_id="1",
        title="Senior Systems Engineer",
        url="https://example.com/1",
        us_eligible=True,
        location_confidence=LocationConfidence.HIGH,
    )
    values.update(updates)
    return Job(**values)


def test_hard_filters_location_and_excluded_title():
    profile = CandidateProfile(target_domains=["systems"], exclude_title_terms=["intern"])
    assert passes_prefilter(job(), profile)
    assert not passes_prefilter(job(us_eligible=False), profile)
    assert not passes_prefilter(job(title="Systems Intern"), profile)


def test_description_can_supply_positive_match():
    profile = CandidateProfile(target_domains=["sensor fusion"])
    assert passes_prefilter(
        job(title="Engineer", description="Develop sensor fusion systems"), profile
    )


def test_title_matches_rank_above_description_only_matches():
    profile = CandidateProfile(target_domains=["ADAS"])
    titled = job(title="ADAS Engineer", description="Engineering role")
    described = job(title="Engineer", description="ADAS engineering role")
    assert relevance_score(titled, profile) > relevance_score(described, profile)


def test_recency_excludes_postings_older_than_cutoff():
    now = datetime(2026, 8, 29, tzinfo=UTC)
    recent = job(posted_at=now - timedelta(days=10))
    stale = job(posted_at=now - timedelta(days=45))
    assert passes_recency(recent, 30, now=now)
    assert not passes_recency(stale, 30, now=now)


def test_recency_keeps_jobs_with_unknown_posted_date():
    assert passes_recency(job(posted_at=None), 30)
