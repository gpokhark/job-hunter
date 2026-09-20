import httpx
import pytest
import respx
import yaml

from job_hunter.lm_studio_health import check_lm_studio, load_lm_studio_config

_CONFIG = {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "timeout_seconds": 30}


@respx.mock
def test_check_lm_studio_ok_when_models_endpoint_responds():
    respx.get("http://127.0.0.1:1234/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "local-model"}]})
    )
    ok, detail = check_lm_studio(_CONFIG)
    assert ok is True
    assert "local-model" in detail


@respx.mock
def test_check_lm_studio_fails_on_connection_error():
    respx.get("http://127.0.0.1:1234/v1/models").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    ok, detail = check_lm_studio(_CONFIG)
    assert ok is False
    assert "can't reach LM Studio" in detail
    assert "127.0.0.1:1234" in detail


@respx.mock
def test_check_lm_studio_fails_on_non_2xx_response():
    """A stale base_url pointing at some other, unrelated service on that port must not be
    reported as a healthy LM Studio server just because the TCP connection succeeded."""
    respx.get("http://127.0.0.1:1234/v1/models").mock(return_value=httpx.Response(404))
    ok, detail = check_lm_studio(_CONFIG)
    assert ok is False
    assert "can't reach LM Studio" in detail


def test_check_lm_studio_fails_fast_with_no_base_url():
    ok, detail = check_lm_studio({})
    assert ok is False
    assert "no base_url configured" in detail


@respx.mock
def test_check_lm_studio_reports_no_model_loaded():
    respx.get("http://127.0.0.1:1234/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    ok, detail = check_lm_studio(_CONFIG)
    assert ok is True
    assert "no model loaded" in detail


def test_load_lm_studio_config_falls_back_to_example(tmp_path):
    example = tmp_path / "lm_studio.example.yaml"
    example.write_text(yaml.safe_dump({"base_url": "http://placeholder:1234/v1"}))
    config = load_lm_studio_config(tmp_path / "lm_studio.yaml")
    assert config["base_url"] == "http://placeholder:1234/v1"


def test_load_lm_studio_config_raises_with_no_config_and_no_example(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_lm_studio_config(tmp_path / "lm_studio.yaml")
