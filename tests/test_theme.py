import json
import re

import pytest

from job_hunter.config import CandidateProfile, ContactInfo, VisionConfig
from job_hunter.theme import (
    EMPTY,
    THEMES,
    build_vision,
    display_first_name,
    json_for_script,
    load_theme,
    resolve_theme_name,
)


def _profile(name="Jane Doe", **vision):
    return CandidateProfile(
        contact=ContactInfo(name=name, email="jane@real-mail.test"), vision=VisionConfig(**vision)
    )


def _stub_templates(tmp_path, js="/*js*/", forest="/*forest*/", dawn="/*dawn*/"):
    (tmp_path / "forest.css").write_text(forest, encoding="utf-8")
    (tmp_path / "forest-dawn.css").write_text(dawn, encoding="utf-8")
    (tmp_path / "offer.js").write_text(js, encoding="utf-8")
    return tmp_path


def _config_of(script_html):
    match = re.search(r'id="mf-config">(.*?)</script>', script_html, re.S)
    return json.loads(match.group(1))


def test_theme_names():
    assert THEMES == ("auto", "forest", "forest-dawn")


def test_resolve_prefers_the_cli_then_settings():
    assert resolve_theme_name("forest", "auto") == "forest"
    assert resolve_theme_name(None, "forest-dawn") == "forest-dawn"
    assert resolve_theme_name(None, "auto") == "auto"


def test_resolve_rejects_unknown_names():
    with pytest.raises(ValueError, match="sunset"):
        resolve_theme_name("sunset", "auto")
    with pytest.raises(ValueError):
        resolve_theme_name(None, "sunset")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Jane Doe", "Jane"),
        ("  Jane   Doe ", "Jane"),
        ("Jane", "Jane"),
        ("Dr. Jane Doe", "Jane"),
        ("Prof Jane Doe", "Jane"),
        ("Mx. Rowan Lee", "Rowan"),
        ("Dr.", None),
        ("Dr. Prof.", None),
        ("", None),
        (None, None),
        ("Your Name", None),
    ],
)
def test_display_first_name(name, expected):
    profile = CandidateProfile(contact=ContactInfo(name=name, email="jane@real-mail.test"))
    assert display_first_name(profile) == expected


def test_display_first_name_respects_the_opt_out_and_missing_profile():
    assert display_first_name(_profile(use_first_name=False)) is None
    assert display_first_name(None) is None
    assert display_first_name(CandidateProfile()) is None


def test_build_vision_defaults_and_overrides():
    assert build_vision(None) == {
        "firstName": None,
        "role": "Your next role",
        "pay": "Better. Higher paying.",
        "when": "Start date: soon",
    }
    custom = build_vision(_profile(role="Staff Engineer", pay_note="Higher", start_note="Monday"))
    assert custom == {"firstName": "Jane", "role": "Staff Engineer", "pay": "Higher", "when": "Monday"}


def test_json_for_script_is_inert_inside_a_script_element():
    value = {"a": "</script><b>&  ", "n": 1}
    out = json_for_script(value)
    for raw in ("<", ">", "&", " ", " "):
        assert raw not in out
    assert json.loads(out) == value


def test_auto_loads_nothing():
    assert load_theme("auto", _profile()) == EMPTY
    assert EMPTY.css == "" and EMPTY.script_html == ""


def test_unknown_theme_is_rejected():
    with pytest.raises(ValueError):
        load_theme("sunset", None)


def test_forest_packages_css_and_a_config_block(tmp_path):
    theme = load_theme("forest", _profile(), templates_dir=_stub_templates(tmp_path))
    assert theme.name == "forest"
    assert theme.css == "/*forest*/"
    assert "/*js*/" in theme.script_html
    assert _config_of(theme.script_html) == {
        "theme": "forest", "firstName": "Jane", "role": "Your next role",
        "pay": "Better. Higher paying.", "when": "Start date: soon",
    }


def test_dawn_layers_its_css_after_forest(tmp_path):
    theme = load_theme("forest-dawn", None, templates_dir=_stub_templates(tmp_path))
    assert theme.css == "/*forest*/\n/*dawn*/"
    assert _config_of(theme.script_html)["theme"] == "forest-dawn"
    assert _config_of(theme.script_html)["firstName"] is None


def test_assets_with_a_terminator_are_refused(tmp_path):
    with pytest.raises(ValueError, match="script"):
        load_theme("forest", None, templates_dir=_stub_templates(tmp_path, js="x</script>y"))
    with pytest.raises(ValueError, match="style"):
        load_theme("forest", None, templates_dir=_stub_templates(tmp_path, forest="a</style>b"))


def test_a_missing_asset_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_theme("forest", None, templates_dir=tmp_path)
