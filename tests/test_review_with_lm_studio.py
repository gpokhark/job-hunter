import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

from job_hunter.config import CandidateProfile, Settings

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import review_with_lm_studio  # noqa: E402
from review_with_lm_studio import (  # noqa: E402
    _eta_suffix,
    _extract_json,
    _format_duration,
    _run_review,
    main,
    review_one,
)

_CONFIG = {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "timeout_seconds": 30}


def test_extract_json_handles_surrounding_prose():
    verdict = {"score": 80, "recommended": True, "matches": ["a"], "gaps": ["b"]}
    wrapped = f"Sure, here's my review:\n{json.dumps(verdict)}\nLet me know if you need more."
    assert _extract_json(wrapped) == verdict


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError, match="not valid JSON"):
        _extract_json("I cannot complete this request.")


@respx.mock
def test_review_one_posts_expected_payload_and_parses_verdict():
    verdict = {"score": 91, "recommended": True, "matches": ["Python", "ADAS"], "gaps": ["No ROS"]}

    def _respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "local-model"
        messages = body["messages"]
        assert messages[0]["role"] == "system"
        assert "Rubric:" in messages[0]["content"]
        assert "Staff Engineer" in messages[1]["content"]
        assert "Acme" in messages[1]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(verdict)}}]}
        )

    respx.post("http://127.0.0.1:1234/v1/chat/completions").mock(side_effect=_respond)
    with httpx.Client() as client:
        result = review_one(
            client,
            _CONFIG,
            resume="Experienced Python engineer.",
            rubric="Score 0-100 based on fit.",
            title="Staff Engineer",
            company="Acme",
            location="Detroit, MI",
            url="https://example.com/job",
            description="Build ADAS features.",
        )
    assert result == verdict


def test_negative_limit_is_rejected(monkeypatch, capsys):
    """docs/agent-runtime-audit.md's "input validation" finding -- a bare type=int previously let
    --limit -1 flow into a downstream `to_review[:args.limit]` slice as a silently-valid but
    surprising "all but the last one" instead of a clear command-line error."""
    monkeypatch.setattr(sys, "argv", ["review_with_lm_studio.py", "--limit", "-1"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "non-negative" in capsys.readouterr().err


def test_format_duration_scales_units():
    assert _format_duration(45) == "45s"
    assert _format_duration(125) == "2m05s"
    assert _format_duration(3725) == "1h02m"


def test_eta_suffix_empty_before_any_job_completes():
    assert _eta_suffix(started=0.0, done=0, total=10) == ""


def test_eta_suffix_reports_measured_average_and_remaining(monkeypatch):
    times = iter([100.0])  # one call to time.monotonic() inside _eta_suffix
    monkeypatch.setattr(review_with_lm_studio.time, "monotonic", lambda: next(times))
    # started=0.0, one job done after 100s elapsed -> avg 100s/job, 4 remaining -> ~400s
    suffix = _eta_suffix(started=0.0, done=1, total=5)
    assert suffix == " [avg 1m40s/job, ~6m40s remaining]"


@respx.mock
def test_run_review_writes_progress_log_and_final_summary(tmp_path, monkeypatch):
    """`job-hunter pipeline` runs this whole script as a subprocess with stdout/stderr fully
    buffered until it exits (see pipeline.py's `_run_stage_subprocess`), so `--progress-log` is
    the only way live per-job progress reaches anything tail-able during that kind of run."""
    monkeypatch.setattr(
        review_with_lm_studio, "check_lm_studio", lambda config: (True, "http://fake/v1 -- ok")
    )
    verdict = {"score": 88, "recommended": True, "matches": ["Python", "ADAS"], "gaps": ["No ROS"]}
    respx.post("http://127.0.0.1:1234/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}}]})
    )

    settings = Settings(database_path=tmp_path / "jobs.sqlite3")
    profile = CandidateProfile(resume_path=tmp_path / "resume.txt")
    candidate = {
        "source_key": "acme", "job_id": "42", "company": "Acme", "title": "Staff Engineer",
        "url": "https://example.com/42", "location_raw": "Remote", "content_hash": "hash-1",
        "description": "Build things.",
    }
    progress_log = tmp_path / "runs" / "review.log"

    exit_code = _run_review(
        [candidate], skipped_cached=3, config=_CONFIG, settings=settings, profile=profile,
        resume="Experienced engineer.", rubric="Score 0-100.", progress_log=progress_log,
    )

    assert exit_code == 0
    log_text = progress_log.read_text()
    assert "Starting local LLM review of 1 job(s); 3 already cached." in log_text
    assert "Reviewing [1/1] Acme — Staff Engineer" in log_text
    assert "done [1/1, 0 remaining]: score=88 recommended=True" in log_text
    assert "Reviewed 1 job(s); skipped 3 already-assessed (unchanged) job(s)." in log_text
