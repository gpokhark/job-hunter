import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from review_with_lm_studio import _extract_json, review_one  # noqa: E402

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
