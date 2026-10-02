from datetime import UTC, datetime

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage
from job_hunter.vocabulary import (
    GENERIC_TITLE_TOKENS,
    PhraseGain,
    phrase_gain,
    rejected_pool,
    title_phrases,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def make_job(**updates):
    values = dict(
        source_key="ford", source_platform="test", company="Ford", job_id="1",
        title="Vehicle Calibration & Test Supervisor", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
        posted_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    values.update(updates)
    return Job(**values)


def test_title_phrases_drop_ampersands_and_order_by_size_then_position():
    phrases = title_phrases("Vehicle Calibration & Test Supervisor")
    assert phrases[:4] == ["vehicle", "calibration", "test", "supervisor"]
    assert "vehicle calibration" in phrases and "test supervisor" in phrases
    # a window spanning the ampersand is still produced here; callers keep only phrases that are
    # real substrings of the title (see why_missed.suggest_terms)
    assert "calibration test" in phrases


def test_title_phrases_skip_windows_made_only_of_generic_tokens():
    assert title_phrases("Senior Engineer") == []
    assert "senior" in GENERIC_TITLE_TOKENS
    assert "senior perception" in title_phrases("Senior Perception Engineer")
    assert "senior" not in title_phrases("Senior Perception Engineer")


def test_title_phrases_are_unique():
    phrases = title_phrases("Test Test Supervisor")
    assert len(phrases) == len(set(phrases))


def test_rejected_pool_holds_only_no_positive_match_jobs_that_are_eligible_and_recent(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(make_job(job_id="1", title="Vehicle Calibration & Test Supervisor"))
        storage.upsert_job(make_job(job_id="2", title="Robotics Engineer"))            # passes the gate
        storage.upsert_job(make_job(job_id="3", title="Calibration Intern"))           # exclude_title_terms
        storage.upsert_job(
            make_job(job_id="4", title="Calibration Lead", posted_at=datetime(2020, 1, 1, tzinfo=UTC))
        )                                                                               # not recent
        storage.upsert_job(make_job(job_id="5", title="Chef", us_eligible=False))      # not eligible
    profile = CandidateProfile(target_title_terms=["robotics"], exclude_title_terms=["intern"])
    pool = rejected_pool(db, profile, 30, now=NOW)
    assert [j.job_id for j in pool] == ["1"]


def _pool():
    return [
        make_job(job_id="a", title="Vehicle Calibration & Test Supervisor"),
        make_job(job_id="b", source_key="apple", title="Camera Calibration and Test Engineer"),
        make_job(job_id="c", source_key="abb", title="Test Supervisor"),
        make_job(job_id="d", title="Chef"),
    ]


def test_phrase_gain_counts_pool_jobs_the_real_gate_would_admit():
    profile = CandidateProfile(target_title_terms=["robotics"])
    gain = phrase_gain(profile, "calibration", _pool())
    assert isinstance(gain, PhraseGain)
    assert gain.count == 2
    assert gain.samples == (
        "ford: Vehicle Calibration & Test Supervisor",
        "apple: Camera Calibration and Test Engineer",
    )
    assert phrase_gain(profile, "test supervisor", _pool()).count == 2
    assert phrase_gain(profile, "no such phrase", _pool()) == PhraseGain(0, ())


def test_phrase_gain_respects_soft_excludes_exactly_like_production():
    profile = CandidateProfile(target_title_terms=["robotics"], soft_exclude_terms=["supervisor"])
    assert phrase_gain(profile, "test supervisor", _pool()).count == 0


def test_phrase_gain_samples_are_capped():
    pool = [make_job(job_id=str(i), title=f"Calibration Role {i}") for i in range(10)]
    gain = phrase_gain(CandidateProfile(target_title_terms=["robotics"]), "calibration", pool, sample_size=3)
    assert gain.count == 10 and len(gain.samples) == 3
