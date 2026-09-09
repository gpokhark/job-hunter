from pathlib import Path

import pytest
from pydantic import ValidationError

from job_hunter.config import RetentionConfig, SearchConfig, load_companies, load_settings


def test_project_configs_validate():
    root = Path(__file__).parents[1]
    settings = load_settings(root / "config/settings.yaml")
    assert settings.version == 1
    companies = load_companies(root / "config/companies.yaml")
    assert len(companies) == 63 and len({item.key for item in companies}) == 63
    # config/settings.yaml's own committed retention: values, not just the model defaults.
    assert settings.retention.closed_job_after_days == 10
    assert settings.retention.report_after_days == 15
    assert settings.retention.keep_latest_reports_per_slug == 2
    assert settings.search.undated_new_days == 15
    assert settings.search.undated_stale_days == 45


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
