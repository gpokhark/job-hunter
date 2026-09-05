import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from refilter_archive import refilter  # noqa: E402


def _candidate(job_id, title, posted_at=None, us_eligible=True):
    return {
        "source_key": "apple", "company": "Apple", "job_id": job_id, "source_platform": "test",
        "title": title, "url": f"https://example.com/{job_id}",
        "us_eligible": us_eligible, "location_confidence": "high",
        "posted_at": posted_at,
    }


def test_refilter_drops_a_job_now_soft_excluded(monkeypatch, tmp_path):
    """The exact scenario this tool exists for: a job that passed prefilter at collection time
    must be dropped after a new soft_exclude_terms entry is added, with no re-fetch."""
    from job_hunter.config import CandidateProfile, Settings

    monkeypatch.setattr(
        "refilter_archive.load_profile",
        lambda: CandidateProfile(target_domains=["verification"], soft_exclude_terms=["cad automation"]),
    )
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = {
        "summary": {"prefilter_candidates": 2},
        "candidates": [
            _candidate("1", "CAD Automation and Mixed-Signal Simulation Engineer"),
            _candidate("2", "Design Verification Engineer"),
        ],
    }
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert [c["job_id"] for c in result["candidates"]] == ["2"]
    assert result["summary"]["prefilter_candidates"] == 1


def test_refilter_recomputes_recency_against_now(monkeypatch):
    from job_hunter.config import CandidateProfile, Settings

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["engineer"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = {
        "summary": {},
        "candidates": [_candidate("1", "Engineer", posted_at="2026-01-01T00:00:00Z")],
    }
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert result["candidates"] == []
    assert result["summary"]["stale_excluded"] == 1


def test_refilter_does_not_mutate_input(monkeypatch):
    from job_hunter.config import CandidateProfile, Settings

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["engineer"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = {"summary": {}, "candidates": [_candidate("1", "Engineer")]}
    refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert len(data["candidates"]) == 1  # original untouched
