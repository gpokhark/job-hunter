from pathlib import Path

from job_hunter.config import load_companies, load_settings


def test_project_configs_validate():
    root = Path(__file__).parents[1]
    assert load_settings(root / "config/settings.yaml").version == 1
    companies = load_companies(root / "config/companies.yaml")
    assert len(companies) == 21 and len({item.key for item in companies}) == 21
