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


def test_description_alone_no_longer_supplies_a_positive_match():
    """A real-world case proved description-wide matching unreliable for gating: a
    company-wide "about us" paragraph or a long "preferred qualifications" list can
    inject a target term into a posting with no real connection to it (see
    passes_prefilter's docstring). The gate now only looks at title + department."""
    profile = CandidateProfile(target_domains=["sensor fusion"])
    assert not passes_prefilter(
        job(title="Engineer", description="Develop sensor fusion systems"), profile
    )


def test_department_can_supply_a_positive_match():
    """Unlike description, department is curated structured metadata (e.g. Honda's
    "Autonomous Tech Dev Dep"), not marketing prose, so it doesn't share that failure
    mode — it can still surface a genuinely relevant but generically-titled role."""
    profile = CandidateProfile(target_domains=["sensor fusion"])
    assert passes_prefilter(job(title="Engineer", department="Sensor Fusion Team"), profile)


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


def test_keyword_override_replaces_profile_terms():
    profile = CandidateProfile(target_domains=["totally-unrelated-term"])
    robotics = job(title="Robotics Engineer")
    assert not passes_prefilter(robotics, profile)
    assert passes_prefilter(robotics, profile, keywords=["robotics"])


def test_keyword_override_still_respects_hard_filters():
    profile = CandidateProfile(exclude_title_terms=["intern"])
    assert not passes_prefilter(job(title="Robotics Intern"), profile, keywords=["robotics"])
    assert not passes_prefilter(
        job(title="Robotics Engineer", us_eligible=False), profile, keywords=["robotics"]
    )


def test_exclude_terms_still_checks_full_description():
    """Excluding is the opposite risk profile from including: over-excluding on a
    disqualifying phrase found anywhere is safe, so exclude_terms keeps scanning the
    full description even though the positive-match gate no longer does."""
    profile = CandidateProfile(target_domains=["validation"], exclude_terms=["cybersecurity"])
    assert not passes_prefilter(
        job(title="Validation Engineer", description="Focus on cybersecurity compliance"),
        profile,
    )


def test_title_only_domain_terms_still_gate_a_relevant_generic_title():
    """Regression: a title like "Integrated Verification and Validation Technical
    Program Manager" must still pass on its own domain terms once description-wide
    matching is removed — the fix is scope (title+department vs. full description),
    not deleting terms like "validation"/"verification" that are genuinely meaningful
    at the title level."""
    profile = CandidateProfile(target_domains=["verification", "validation"])
    assert passes_prefilter(
        job(title="Integrated Verification and Validation Technical Program Manager (GR 31)"),
        profile,
    )
