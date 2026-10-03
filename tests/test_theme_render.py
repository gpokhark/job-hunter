import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import render_radar  # noqa: E402
from render_radar import (  # noqa: E402
    LiveState,
    add_selection_arguments,
    build,
    render,
    resolve_page_theme,
    selection_render_kwargs,
)

from job_hunter.config import (  # noqa: E402
    CandidateProfile,
    ContactInfo,
    RadarConfig,
    Settings,
    VisionConfig,
)
from job_hunter.theme import EMPTY, load_theme  # noqa: E402

THEMES_DIR = render_radar._TEMPLATE_DIR / "themes"


def _inputs(tmp_path):
    search = tmp_path / "search.json"
    search.write_text(json.dumps({
        "summary": {"sources_succeeded": 1, "sources_attempted": 1},
        "candidates": [{
            "source_key": "x", "job_id": "1", "posted_at": "2026-08-25T00:00:00Z",
            "first_seen_at": None, "location_raw": "Detroit, MI", "visa_sponsorship": "unmentioned",
            "sponsorship_evidence": None, "salary_evidence": None, "work_arrangement": "unknown",
            "company": "Acme", "title": "Scored Role", "url": "https://example.com/1",
        }],
        "source_health": [],
    }))
    assess = tmp_path / "assessments.json"
    assess.write_text(json.dumps([{
        "source_key": "x", "job_id": "1", "score": 88, "company": "Acme", "title": "Scored Role",
        "url": "https://example.com/1", "matches": ["m"], "gaps": ["g"],
    }]))
    return dict(
        search_path=search, assessments_path=assess, title="Test Radar", keyword_label=None,
        new_days=10, now=datetime(2026, 8, 31, tzinfo=UTC),
    )


def _profile(name="Jane Doe", **vision):
    return CandidateProfile(
        contact=ContactInfo(name=name, email="jane@real-mail.test"), vision=VisionConfig(**vision)
    )


def _config_of(html):
    return json.loads(re.search(r'id="mf-config">(.*?)</script>', html, re.S).group(1))


def test_auto_theme_changes_nothing(tmp_path):
    inputs = _inputs(tmp_path)
    plain, _ = render(**inputs)
    assert render(theme=EMPTY, **inputs)[0] == plain
    assert render(theme=load_theme("auto", _profile(), templates_dir=THEMES_DIR), **inputs)[0] == plain
    assert "mf-config" not in plain and ":root:root:root" not in plain


@pytest.mark.parametrize("name", ["forest", "forest-dawn"])
def test_forest_themes_are_injected_once(tmp_path, name):
    theme = load_theme(name, _profile(), templates_dir=THEMES_DIR)
    html, _ = render(theme=theme, **_inputs(tmp_path))
    assert html.count('id="mf-config"') == 1
    assert html.count("window.JobHunterTheme") == 1
    assert html.count(":root:root:root {") >= 1
    assert _config_of(html)["theme"] == name and _config_of(html)["firstName"] == "Jane"
    assert (".mf-sun {" in html) == (name == "forest-dawn")  # the rule lives only in dawn's CSS
    assert "<title>Test Radar</title>" in html  # the generated markup keeps its plain title


def test_theme_works_in_live_mode_next_to_the_live_scripts(tmp_path):
    state = LiveState(
        feedback={}, versions={"archive": "a", "assessments": "s", "feedback": "f"},
        archive_name="search.json",
    )
    theme = load_theme("forest", _profile(), templates_dir=THEMES_DIR)
    html, _ = render(live=True, live_state=state, theme=theme, **_inputs(tmp_path))
    assert "window.__RADAR_LIVE__" in html and 'id="mf-config"' in html
    assert html.index("window.__RADAR_LIVE__") < html.index('id="mf-config"')


def test_hostile_text_cannot_break_out_of_the_config_block(tmp_path):
    hostile = '</script><img src=x onerror=alert(1)>&" '
    profile = _profile(name="Jane</script><script>alert(1)</script>", role=hostile, pay_note=hostile,
                       start_note=hostile)
    theme = load_theme("forest", profile, templates_dir=THEMES_DIR)
    html, _ = render(theme=theme, **_inputs(tmp_path))
    assert "<script>alert(1)" not in html
    assert "<img src=x" not in html
    config = _config_of(html)
    assert config["role"] == hostile and config["firstName"].startswith("Jane")


def test_a_placeholder_name_means_no_first_name(tmp_path):
    theme = load_theme("forest", _profile(name="Your Name"), templates_dir=THEMES_DIR)
    html, _ = render(theme=theme, **_inputs(tmp_path))
    assert _config_of(html)["firstName"] is None


def test_build_writes_the_themed_page(tmp_path):
    out = tmp_path / "out.html"
    build(output_path=out, theme=load_theme("forest-dawn", _profile(), templates_dir=THEMES_DIR),
          **_inputs(tmp_path))
    assert 'id="mf-config"' in out.read_text(encoding="utf-8")


def _args(*argv):
    parser = argparse.ArgumentParser()
    add_selection_arguments(parser)
    return parser.parse_args(list(argv))


def test_resolve_page_theme_precedence():
    settings = Settings(radar=RadarConfig(theme="forest-dawn"))
    assert resolve_page_theme(_args(), settings, None).name == "forest-dawn"
    assert resolve_page_theme(_args("--theme", "forest"), settings, None).name == "forest"
    assert resolve_page_theme(_args("--theme", "auto"), settings, None) == EMPTY
    assert resolve_page_theme(_args(), Settings(), _profile()) == EMPTY


def test_selection_kwargs_carry_the_theme():
    settings = Settings(radar=RadarConfig(theme="forest"))
    kwargs = selection_render_kwargs(_args(), settings, _profile())
    assert kwargs["theme"].name == "forest"
    assert kwargs["theme"].script_html.count('id="mf-config"') == 1


def test_an_unknown_cli_theme_is_rejected():
    with pytest.raises(SystemExit):
        _args("--theme", "sunset")
