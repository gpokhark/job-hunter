from pathlib import Path

import pytest
from pydantic import ValidationError

from job_hunter.config import (
    CandidateProfile,
    RadarConfig,
    RetentionConfig,
    SearchConfig,
    VisionConfig,
    load_companies,
    load_settings,
)


def test_project_configs_validate():
    root = Path(__file__).parents[1]
    settings = load_settings(root / "config/settings.example.yaml")
    assert settings.version == 1
    companies = load_companies(root / "config/companies.yaml")
    assert len(companies) == 85 and len({item.key for item in companies}) == 85
    # config/settings.example.yaml's own committed retention: values, not just the model defaults.
    assert settings.retention.closed_job_after_days == 10
    assert settings.retention.report_after_days == 15
    assert settings.retention.keep_latest_reports_per_slug == 2
    assert settings.search.undated_new_days == 15
    assert settings.search.undated_stale_days == 45
    # A fresh clone must ship a safe pipeline stage-timeout default (docs/agent-runtime-audit.md's
    # "timeout is still opt-in" finding) — the Python-level PipelineConfig default stays None/no
    # timeout (see its own docstring), but this file's shipped value must not be commented out.
    assert settings.pipeline.stage_timeout_seconds == 28800


def test_search_config_undated_defaults_when_key_absent():
    """An older settings.yaml with no undated_new_days/undated_stale_days keys must still
    validate — same backward-compatible pattern as every other Settings sub-section."""
    assert SearchConfig().undated_new_days == 15
    assert SearchConfig().undated_stale_days == 45


def test_search_config_rejects_invalid_undated_values():
    with pytest.raises(ValidationError):
        SearchConfig(undated_new_days=0)
    with pytest.raises(ValidationError):
        SearchConfig(undated_stale_days=0)


def test_retention_config_defaults_when_key_absent():
    """An older settings.yaml with no retention: key at all must still validate — same
    backward-compatible pattern as every other Settings sub-section."""
    assert RetentionConfig().closed_job_after_days == 10
    assert RetentionConfig().report_after_days == 15
    assert RetentionConfig().keep_latest_reports_per_slug == 2


def test_retention_config_rejects_invalid_values():
    with pytest.raises(ValidationError):
        RetentionConfig(closed_job_after_days=0)
    with pytest.raises(ValidationError):
        RetentionConfig(report_after_days=0)
    with pytest.raises(ValidationError):
        RetentionConfig(keep_latest_reports_per_slug=-1)


def test_shipped_settings_keep_the_default_page_theme():
    root = Path(__file__).parents[1]
    assert load_settings(root / "config/settings.example.yaml").radar.theme == "auto"


def test_radar_config_theme_values():
    assert RadarConfig().theme == "auto"
    assert RadarConfig(theme="forest").theme == "forest"
    assert RadarConfig(theme="forest-dawn").theme == "forest-dawn"
    with pytest.raises(ValidationError):
        RadarConfig(theme="sunset")


def test_vision_config_defaults_and_overrides():
    vision = VisionConfig()
    assert vision.role == "Your next role"
    assert vision.pay_note == "Better. Higher paying."
    assert vision.start_note == "Start date: soon"
    assert vision.use_first_name is True
    custom = VisionConfig(role="Staff Perception Engineer", use_first_name=False)
    assert custom.role == "Staff Perception Engineer" and custom.use_first_name is False


def test_vision_config_rejects_empty_and_overlong_text():
    with pytest.raises(ValidationError):
        VisionConfig(role="")
    with pytest.raises(ValidationError):
        VisionConfig(pay_note="x" * 61)


def test_candidate_profile_has_a_default_vision():
    assert CandidateProfile().vision == VisionConfig()
