# Forest Themes + "Offer Season" Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in `forest` (night) and `forest-dawn` (sunrise) themes, with a manifestation layer and first-name personalisation, to the static radar, live radar and Applications page, while leaving default output byte-identical.

**Architecture:** A pure module `src/job_hunter/theme.py` resolves the theme (CLI > `settings.yaml` > `auto`) and packages bundled CSS/JS plus a JSON config block into a `ResolvedTheme`. The renderers append that to token values that already exist (`__LIVE_STYLE__`, `__LIVE_SCRIPT__`, `__SCRIPTS__`) plus one new `__THEME_CSS__` token on the Applications template, so `auto` expands to empty strings. The live pages dispatch one DOM event when a save newly reaches status `offer`; the theme JS listens and celebrates.

**Tech Stack:** Python 3.11, pydantic, pytest, ruff, vanilla JS (no build step), `node --test`, optional Playwright for a browser smoke test.

**Spec:** `docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md` (this plan amends three details, recorded in Task 1).

## Global Constraints

- `radar.theme: auto` (the default) must leave generated HTML **byte-identical** to today. `tests/test_render_radar.py::test_static_radar_matches_golden` (golden `tests/fixtures/radar_static_golden.html`) must pass **unmodified** after every task.
- Theme values are exactly `auto`, `forest`, `forest-dawn` (hyphen, lowercase).
- Never insert user-controlled text (first name, role, pay note, start note) with `innerHTML`; use `textContent`/`createTextNode`. Embedded JSON uses the `<`, `>`, `&`, U+2028, U+2029 escapes.
- No personal data in tracked files or tests: fixtures use `Jane Doe` / `jane@example.com`-style fake values only (`tests/test_no_owner_pii_in_shareable_files.py` must pass).
- Theme CSS selectors use `:root:root:root` (specificity 0,3,0) so they beat the template's `:root:not([data-theme="light"])` dark-mode block without needing an attribute at parse time.
- Theme asset files are ASCII only (use `\u2726` style escapes in JS, `"\2726"` in CSS) and must not contain `</script` (JS) or `</style` (CSS).
- Ruff: line length 100, rules `E,F,I,UP,B,SIM` (E501 ignored); run `uv run ruff check .` before the final task commit. Python `>=3.11`.
- macOS: never use `sed -i` without `''`; prefer the Edit tool or small Python edits.
- Do not touch `scripts/serve_radar.py` behaviour beyond passing the theme; do not change scoring, filtering or storage.
- Commits end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Behaviours the spec implies but no obvious test exercises; each has an owning task and test.

1. **Names with a leading honorific** ("Dr. Jane Doe", "Prof Jane") must greet "Jane", not "Dr."; a name that is only an honorific greets nobody. *(Task 2 tests)*
2. **Hostile text** in name/role/pay/start (`</script>`, `<img onerror>`, `&`, quotes, U+2028) must be inert in the JSON block and in the DOM (no injected element, no raw `</script` in the page). *(Task 4 render test, Task 7 browser test)*
3. **No usable name** (missing `contact`, example placeholder, `use_first_name: false`) must never print `Dear ,`, `undefined` or `null`; and a browser whose `localStorage` throws must still render the page. *(Task 2, Task 7)*
4. **OS dark mode vs light mode** must not change the forest palette (specificity beats the template's dark block). *(Task 7: computed `--paper` under both color schemes)*
5. **Celebration rules:** fires only when a save *newly* reaches `offer` (re-saving notes on an existing offer does not), and never while offer mode is off. *(Task 6 node test, Task 7 browser test)*

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/job_hunter/config.py` | Modify | `RadarConfig`, `VisionConfig`, wired into `Settings`/`CandidateProfile` |
| `src/job_hunter/theme.py` | Create | pure theme resolution + packaging (`ResolvedTheme`, `load_theme`, ...) |
| `scripts/templates/themes/forest.css` | Create | night palette, backdrop, manifestation components, celebration |
| `scripts/templates/themes/forest-dawn.css` | Create | sunrise overrides layered after `forest.css` |
| `scripts/templates/themes/offer.js` | Create | backdrop, title/card/affirmations, offer-mode switch, celebration, event listener |
| `scripts/render_radar.py` | Modify | `--theme`, `resolve_page_theme`, theme param on `render()` |
| `scripts/render_applications.py` | Modify | theme param, token + script append |
| `scripts/templates/applications_template.html` | Modify | one new `__THEME_CSS__` token |
| `scripts/serve_radar.py` | Modify | pass the theme to the Applications renderer |
| `scripts/templates/radar_live_core.js` | Modify | `isNewOffer(prev, next)` |
| `scripts/templates/radar_live_ui.js`, `applications_ui.js` | Modify | dispatch `jobhunter:application-status` after an acked new offer |
| `config/settings.yaml`, `config/candidate_profile.example.yaml` | Modify | documented `radar:` / `vision:` blocks |
| `tests/test_config.py`, `tests/test_theme.py`, `tests/test_theme_assets.py`, `tests/test_theme_render.py`, `tests/test_render_applications.py`, `tests/js/radar_live_core.test.js`, `tests/test_forest_theme_browser.py` | Create/Modify | per-task tests |
| `tests/fixtures/applications_static_golden.html` | Create | byte-identity baseline for the Applications page |
| `README.md`, `docs/USAGE.md`, `docs/SPEC.md`, `CLAUDE.md`, `docs/img/theme-*.png` | Modify/Create | docs |

---

### Task 1: Config models, example config, spec amendments

**Files:**
- Modify: `src/job_hunter/config.py` (add two models; wire into `Settings` and `CandidateProfile`)
- Modify: `config/settings.yaml` (append `radar:` block)
- Modify: `config/candidate_profile.example.yaml` (append commented `vision:` block)
- Modify: `docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md` (append amendments)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `RadarConfig(theme: Literal["auto","forest","forest-dawn"] = "auto")`, `Settings.radar: RadarConfig`; `VisionConfig(role: str, pay_note: str, start_note: str, use_first_name: bool)` with defaults `"Your next role"`, `"Better. Higher paying."`, `"Start date: soon"`, `True`; `CandidateProfile.vision: VisionConfig`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py` (extend the existing import line to `from job_hunter.config import CandidateProfile, RadarConfig, RetentionConfig, SearchConfig, VisionConfig, load_companies, load_settings`):

```python
def test_shipped_settings_keep_the_default_page_theme():
    root = Path(__file__).parents[1]
    assert load_settings(root / "config/settings.yaml").radar.theme == "auto"


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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'RadarConfig'`.

- [ ] **Step 3: Implement the models** — in `src/job_hunter/config.py`, add directly above `class Settings(BaseModel):`:

```python
class RadarConfig(BaseModel):
    """Look of the generated HTML pages. `auto` is the plain light/dark page; the forest themes
    are opt-in (docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md)."""

    theme: Literal["auto", "forest", "forest-dawn"] = "auto"
```

Add one line at the end of `Settings` (after `pipeline: PipelineConfig = PipelineConfig()`):

```python
    radar: RadarConfig = RadarConfig()
```

Add directly above `class CandidateProfile(BaseModel):`:

```python
class VisionConfig(BaseModel):
    """Display text for the forest themes' "Offer Season" layer. Never a filter or a score input.
    `use_first_name: false` keeps the applicant's name out of the generated pages."""

    role: str = Field("Your next role", min_length=1, max_length=60)
    pay_note: str = Field("Better. Higher paying.", min_length=1, max_length=60)
    start_note: str = Field("Start date: soon", min_length=1, max_length=60)
    use_first_name: bool = True
```

Add one line at the end of `CandidateProfile` (after `contact: ContactInfo = ...`):

```python
    vision: VisionConfig = Field(default_factory=VisionConfig)
```

- [ ] **Step 4: Document the settings** — append to `config/settings.yaml`:

```yaml
# Look of the generated radar / applications pages
# (docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md).
#   auto        the plain light/dark page (default)
#   forest      night forest
#   forest-dawn sunrise
# `scripts/render_radar.py --theme ...` overrides this for a single run.
radar:
  theme: auto
```

Append to `config/candidate_profile.example.yaml`:

```yaml

# Optional: display text for the forest themes' "Offer Season" layer (settings.yaml radar.theme).
# Never a filter or a score input. `use_first_name: false` keeps your name out of the pages.
# vision:
#   role: "Your next role"
#   pay_note: "Better. Higher paying."
#   start_note: "Start date: soon"
#   use_first_name: true
```

- [ ] **Step 5: Amend the spec** — append to the end of `docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md`:

```markdown

## 10. Amendments made while planning (2026-10-03)

These supersede the matching statements above.

- **A1 Tokens.** The renderer substitutes tokens in a single regex pass over `__[A-Z][A-Z0-9_]*__`,
  and golden tests demand byte-identical `auto` output, so no `__THEME_BOOT__` token exists. Radar:
  theme CSS is appended to the value of the existing `__LIVE_STYLE__` token and the theme script to
  `__LIVE_SCRIPT__`. Applications: one new `__THEME_CSS__` token directly before `</style>`, and the
  theme script is appended to the `__SCRIPTS__` value. `ResolvedTheme` is `(name, css, script_html)`.
- **A2 Specificity.** Because there is no boot script, theme CSS selectors use `:root:root:root`
  (0,3,0) instead of `[data-theme=...]`; `offer.js` still sets `data-theme` for hooks.
- **A3 Honorifics.** The first name skips a leading honorific (`Dr`, `Mr`, `Mrs`, `Ms`, `Miss`,
  `Mx`, `Prof`, with or without a trailing dot); a name made only of honorifics yields no first name.
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/config.py config/settings.yaml config/candidate_profile.example.yaml tests/test_config.py docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md
git commit -m "feat(theme): add radar.theme setting and profile vision block

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `theme.py` — resolution, name rules, packaging

**Files:**
- Create: `src/job_hunter/theme.py`
- Test: `tests/test_theme.py`

**Interfaces:**
- Consumes: `CandidateProfile`, `VisionConfig`, `contact_problems` from `job_hunter.config` (Task 1).
- Produces:
  - `THEMES: tuple[str, ...] = ("auto", "forest", "forest-dawn")`
  - `@dataclass(frozen=True) class ResolvedTheme: name: str; css: str; script_html: str`
  - `EMPTY: ResolvedTheme` (name `"auto"`, empty strings)
  - `resolve_theme_name(cli: str | None, configured: str) -> str`
  - `display_first_name(profile: CandidateProfile | None) -> str | None`
  - `build_vision(profile: CandidateProfile | None) -> dict[str, Any]` → keys `firstName`, `role`, `pay`, `when`
  - `json_for_script(value: Any) -> str`
  - `load_theme(name: str, profile: CandidateProfile | None, templates_dir: Path | None = None) -> ResolvedTheme`
  - `DEFAULT_TEMPLATES_DIR: Path` (`<repo>/scripts/templates/themes`)

- [ ] **Step 1: Write the failing tests** — `tests/test_theme.py`:

```python
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
    value = {"a": "</script><b>&\u2028\u2029", "n": 1}
    out = json_for_script(value)
    for raw in ("<", ">", "&", "\u2028", "\u2029"):
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_theme.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_hunter.theme'`.

- [ ] **Step 3: Implement** — create `src/job_hunter/theme.py`:

```python
"""Resolve the radar theme and package it for the generated pages.

Pure apart from reading three bundled asset files: `forest.css`, `forest-dawn.css`, `offer.js`
under `scripts/templates/themes/`. `auto` yields empty strings, so the default pages stay
byte-identical. Spec: docs/superpowers/specs/2026-10-03-forest-offer-theme-design.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from job_hunter.config import CandidateProfile, VisionConfig, contact_problems

THEMES = ("auto", "forest", "forest-dawn")
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "scripts" / "templates" / "themes"
_HONORIFICS = frozenset({"dr", "mr", "mrs", "ms", "miss", "mx", "prof"})


@dataclass(frozen=True)
class ResolvedTheme:
    name: str
    css: str  # appended inside the page's <style>
    script_html: str  # complete <script> elements, appended after the page's own scripts


EMPTY = ResolvedTheme("auto", "", "")


def resolve_theme_name(cli: str | None, configured: str) -> str:
    chosen = cli if cli is not None else configured
    if chosen not in THEMES:
        raise ValueError(f"unknown theme {chosen!r}; choose one of {', '.join(THEMES)}")
    return chosen


def display_first_name(profile: CandidateProfile | None) -> str | None:
    """First name for greetings, or None when it should not be shown: no profile, the applicant
    opted out, the name is missing/still the example placeholder, or it is only honorifics."""
    if profile is None or not profile.vision.use_first_name:
        return None
    if any(p.startswith("name:") for p in contact_problems(profile.contact)):
        return None
    for token in (profile.contact.name or "").split():
        if token.rstrip(".").lower() in _HONORIFICS:
            continue
        return token
    return None


def build_vision(profile: CandidateProfile | None) -> dict[str, Any]:
    vision = profile.vision if profile is not None else VisionConfig()
    return {
        "firstName": display_first_name(profile),
        "role": vision.role,
        "pay": vision.pay_note,
        "when": vision.start_note,
    }


def json_for_script(value: Any) -> str:
    """JSON safe inside a <script> element: `<`, `>`, `&` and the two JS line separators are
    \\u-escaped so hostile text can never close the tag or break a string literal."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def load_theme(
    name: str, profile: CandidateProfile | None, templates_dir: Path | None = None
) -> ResolvedTheme:
    if name not in THEMES:
        raise ValueError(f"unknown theme {name!r}; choose one of {', '.join(THEMES)}")
    if name == "auto":
        return EMPTY
    base = Path(templates_dir) if templates_dir is not None else DEFAULT_TEMPLATES_DIR
    css_parts = [(base / "forest.css").read_text(encoding="utf-8")]
    if name == "forest-dawn":
        css_parts.append((base / "forest-dawn.css").read_text(encoding="utf-8"))
    script = (base / "offer.js").read_text(encoding="utf-8")
    if "</script" in script.lower():
        raise ValueError("offer.js must not contain a script terminator")
    if any("</style" in part.lower() for part in css_parts):
        raise ValueError("theme CSS must not contain a style terminator")
    config = {"theme": name, **build_vision(profile)}
    script_html = (
        f'<script type="application/json" id="mf-config">{json_for_script(config)}</script>\n'
        f"<script>\n{script}\n</script>"
    )
    return ResolvedTheme(name, "\n".join(css_parts), script_html)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_theme.py -q`
Expected: PASS (all cases, including the honorific and placeholder rows).

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/theme.py tests/test_theme.py
git commit -m "feat(theme): add pure theme resolution and packaging module

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Theme assets (`forest.css`, `forest-dawn.css`, `offer.js`)

**Files:**
- Create: `scripts/templates/themes/forest.css`
- Create: `scripts/templates/themes/forest-dawn.css`
- Create: `scripts/templates/themes/offer.js`
- Test: `tests/test_theme_assets.py`

**Interfaces:**
- Consumes: `load_theme`, `DEFAULT_TEMPLATES_DIR` (Task 2).
- Produces: a page contract used by Tasks 4-7. The JS reads `<script type="application/json" id="mf-config">` with keys `theme`, `firstName`, `role`, `pay`, `when`; it listens for the window event `jobhunter:application-status` (`detail.status === "offer"`); it exposes `window.JobHunterTheme.celebrate()`. DOM ids/classes it creates: `#mf-bg`, `#mf-toggle`, `#mf-preview`, `#mf-win` (class `on` when visible), `.mf-line`, `.mf-tag`, `.mf-offer`, `.mf-sub`, `.mf-foot`; elements it relabels carry `data-mf-orig`/`data-mf-new`; `html` gets `data-theme` and the class `mf-off` when offer mode is off; `localStorage["job-hunter-offer-mode"]` is `"off"` or `"on"`.

- [ ] **Step 1: Write the failing test** — `tests/test_theme_assets.py`:

```python
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile, ContactInfo
from job_hunter.theme import DEFAULT_TEMPLATES_DIR, load_theme

FOREST = DEFAULT_TEMPLATES_DIR / "forest.css"
DAWN = DEFAULT_TEMPLATES_DIR / "forest-dawn.css"
JS = DEFAULT_TEMPLATES_DIR / "offer.js"


def test_asset_files_exist_and_are_ascii():
    for path in (FOREST, DAWN, JS):
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").isascii(), f"{path.name} must be ASCII only"


def test_css_outranks_the_page_dark_mode_block_and_defines_the_components():
    css = FOREST.read_text(encoding="utf-8")
    assert ":root:root:root" in css
    assert "[data-theme" not in css
    for selector in ("#mf-bg", "#mf-win", "#mf-toggle", "#mf-preview", ".mf-offer", ".mf-line"):
        assert selector in css
    assert "prefers-reduced-motion" in css
    dawn = DAWN.read_text(encoding="utf-8")
    assert ":root:root:root" in dawn and ".mf-sun" in dawn and ".mf-rays" in dawn


def test_js_is_safe_by_construction():
    js = JS.read_text(encoding="utf-8")
    assert "</script" not in js.lower()
    assert js.count("innerHTML") == 1, "only the numeric SVG tree markup may use innerHTML"
    assert "mf-config" in js and "textContent" in js
    assert "jobhunter:application-status" in js
    assert "job-hunter-offer-mode" in js
    assert "JobHunterTheme" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_parses():
    result = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_the_real_assets_load_for_both_themes():
    profile = CandidateProfile(contact=ContactInfo(name="Jane Doe", email="jane@real-mail.test"))
    forest = load_theme("forest", profile)
    dawn = load_theme("forest-dawn", profile)
    assert FOREST.read_text(encoding="utf-8") in forest.css
    assert dawn.css.startswith(forest.css) and ".mf-sun" in dawn.css
    config = json.loads(re.search(r'id="mf-config">(.*?)</script>', dawn.script_html, re.S).group(1))
    assert config["theme"] == "forest-dawn" and config["firstName"] == "Jane"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_theme_assets.py -q`
Expected: FAIL (`AssertionError: ... forest.css` — the files do not exist yet).

- [ ] **Step 3: Create `scripts/templates/themes/forest.css`**

```css
/* Forest theme (night) + the "Offer Season" layer.
   Injected by src/job_hunter/theme.py only when radar.theme is forest or forest-dawn.
   `:root:root:root` (specificity 0,3,0) outranks the page's own dark-mode block
   (a `:root:not(...)` rule, specificity 0,2,0) without needing an attribute at parse time. */
:root:root:root {
  --paper: #08110d; --ink: #E6EFE8; --ink-soft: #A9C0B1; --surface: #0F1F18; --line: #25402F;
  --accent: #F0B45A; --accent-soft: #1E2A19;
  --score-fair: #8FA9C4; --score-moderate: #4FB3C8; --score-promising: #E8C44E;
  --score-strong: #F08A55; --score-exceptional: #6FD6A0;
  --status-good: #6FD6A0; --status-good-soft: #12301F; --status-warning: #E3AA45;
  --status-warning-soft: #33290F; --danger: #E2695E; --danger-soft: #3A1F1C;
  --arrangement-remote: #6FB1EE; --arrangement-remote-soft: #17293A;
  --arrangement-hybrid: #C0A3EA; --arrangement-hybrid-soft: #2A2038;
  --muted: #86A090; --shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
  --atmosphere-1: rgba(95, 174, 126, 0.14); --atmosphere-2: rgba(240, 180, 90, 0.10);
  color-scheme: dark;
  background: var(--paper);
}
:root:root:root body {
  background:
    radial-gradient(1100px 620px at 10% -12%, var(--atmosphere-1), transparent 60%),
    radial-gradient(900px 560px at 100% -4%, var(--atmosphere-2), transparent 55%),
    transparent;
}
:root:root:root .masthead::before { background-image: none; }
:root:root:root .masthead { overflow: visible; }
:root:root:root h1 { color: var(--ink); text-shadow: 0 2px 24px rgba(0, 0, 0, 0.5); }
:root:root:root .stats-new { border-color: #3a3a1d; }
:root:root:root details.row:hover { border-color: var(--accent); }
:root:root:root .row.tier-exceptional { box-shadow: inset 3px 0 0 var(--score-exceptional); }
:root:root:root main { position: relative; }
@media (min-width: 761px) {
  :root:root:root .subhead, .mf-line { max-width: 52ch; }
}

/* ---- backdrop: fixed, behind the page, static ---- */
#mf-bg {
  position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden;
  background: linear-gradient(180deg, #07100c 0%, #0b1c14 55%, #07130d 100%);
}
#mf-bg > * { position: absolute; }
.mf-moon {
  right: 9%; top: 5%; width: 210px; height: 210px; border-radius: 50%;
  background: radial-gradient(circle, rgba(245, 241, 210, .45), rgba(245, 241, 210, .12) 45%, rgba(245, 241, 210, 0) 70%);
}
.mf-trees { left: 0; bottom: 0; width: 100%; height: 46%; }
.mf-trees svg { display: block; width: 100%; height: 100%; }
.mf-ground {
  left: 0; right: 0; bottom: 0; height: 22%;
  background: linear-gradient(180deg, rgba(4, 9, 6, 0), #040906 70%);
}
.mf-mote {
  width: 5px; height: 5px; border-radius: 50%; background: #ffd98a;
  box-shadow: 0 0 10px 4px rgba(255, 200, 90, .45); opacity: .55;
}
@media (prefers-reduced-motion: no-preference) {
  .mf-mote { animation: mf-twinkle 4s ease-in-out infinite; }
  @keyframes mf-twinkle { 50% { opacity: .12; } }
}

/* ---- manifestation layer: separate from data, always optional ---- */
.mf-tag {
  font-family: "IBM Plex Sans", system-ui, sans-serif; font-style: italic; font-size: 19px;
  color: var(--accent); margin: -4px 0 16px;
}
.mf-line {
  font-family: "IBM Plex Sans", system-ui, sans-serif; font-style: italic; font-size: 17px;
  color: var(--accent); margin: -10px 0 22px; max-width: 60ch; cursor: pointer;
}
.mf-line::before { content: "\2726"; font-style: normal; margin-right: 10px; opacity: .85; }
.mf-offer {
  position: absolute; right: 0; top: 0; width: 262px; padding: 14px 18px;
  background: linear-gradient(160deg, #17281d, #101d16); border: 1px solid #3a5a44; border-radius: 6px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, .45); transform: rotate(1.2deg);
}
.mf-offer .k {
  font-family: "IBM Plex Mono", monospace; font-size: 10.5px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--accent); font-weight: 600;
}
.mf-offer .t { font-style: italic; font-size: 12px; color: var(--accent); margin: 4px 0 8px; line-height: 1.3; }
.mf-offer .d { font-size: 13px; color: var(--ink-soft); margin-bottom: 2px; }
.mf-offer .r {
  font-family: "Big Shoulders Display", sans-serif; font-weight: 800; font-size: 26px; line-height: 1;
  margin: 6px 0 4px; color: var(--ink); text-transform: uppercase;
}
.mf-offer .p { font-size: 13px; color: var(--ink-soft); }
.mf-offer .p b { color: var(--accent); font-weight: 600; }
.mf-offer .seal {
  position: absolute; right: 12px; bottom: -18px; width: 46px; height: 46px; border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #ffd98a, #d99a3a 70%); color: #2a1a02;
  display: grid; place-items: center; font-weight: 700; font-size: 22px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, .5); transform: rotate(-8deg);
}
@media (max-width: 760px) {
  .mf-offer { position: static; width: auto; transform: none; margin: 0 0 18px; }
}
.mf-sub {
  font-family: "IBM Plex Sans", sans-serif; font-style: italic; font-size: 13px;
  color: var(--accent); margin-left: 12px; opacity: .9;
}
.mf-note { font-style: italic; color: var(--accent); }
.mf-foot { text-align: center; font-style: italic; color: var(--ink-soft); margin: 56px 0 0; font-size: 15px; }
.mf-foot::before { content: "\2726  "; color: var(--accent); }
.mf-foot::after { content: "  \2726"; color: var(--accent); }
#mf-toggle {
  position: fixed; left: 14px; bottom: 14px; z-index: 50; font: 500 12px "IBM Plex Mono", monospace;
  color: var(--ink-soft); background: rgba(8, 17, 13, .85); border: 1px solid var(--line);
  border-radius: 999px; padding: 7px 14px; cursor: pointer;
}
#mf-toggle b { color: var(--accent); font-weight: 500; }
#mf-preview {
  position: fixed; left: 50%; transform: translateX(-50%); bottom: 14px; z-index: 50;
  font: 500 12px "IBM Plex Mono", monospace; color: var(--ink-soft); background: rgba(8, 17, 13, .85);
  border: 1px dashed var(--line); border-radius: 999px; padding: 7px 14px; cursor: pointer;
}
html.mf-off .mf-line, html.mf-off .mf-tag, html.mf-off .mf-offer, html.mf-off .mf-sub,
html.mf-off .mf-note, html.mf-off .mf-foot, html.mf-off #mf-preview { display: none; }

/* ---- the moment ---- */
#mf-win {
  position: fixed; inset: 0; z-index: 100; display: none; align-items: center; justify-content: center;
  flex-direction: column; text-align: center; cursor: pointer;
  background: radial-gradient(circle, rgba(8, 17, 13, .78), rgba(8, 17, 13, .94));
}
#mf-win.on { display: flex; }
#mf-win h2 {
  font-family: "Big Shoulders Display", sans-serif; font-weight: 900; font-size: clamp(60px, 12vw, 150px);
  text-transform: uppercase; margin: 0; line-height: .9; color: var(--ink);
}
#mf-win h2 em { font-style: normal; color: var(--accent); }
#mf-win p { font-style: italic; font-size: 22px; color: var(--ink-soft); margin: 18px 0 0; }
.mf-spark {
  position: absolute; width: 8px; height: 8px; border-radius: 50%; background: #ffd98a;
  box-shadow: 0 0 14px 6px rgba(255, 200, 90, .6); bottom: -20px; opacity: 0;
}
@media (prefers-reduced-motion: no-preference) {
  #mf-win.on .mf-spark { animation: mf-rise 3.2s ease-out forwards; }
  @keyframes mf-rise {
    0% { opacity: 0; transform: translateY(0); }
    15% { opacity: 1; }
    100% { opacity: 0; transform: translateY(-110vh); }
  }
}
```

- [ ] **Step 4: Create `scripts/templates/themes/forest-dawn.css`**

```css
/* Forest theme (sunrise): layered after forest.css; overrides palette and swaps the moon for a
   sun. The text column sits on a dark scrim so no text renders over the bright horizon. */
:root:root:root {
  --paper: #150f26; --ink: #F7EFF1; --ink-soft: #D3C5DC; --surface: #1A142E; --line: #3D3360;
  --accent: #FFC46B; --accent-soft: #2B2348;
  --score-fair: #9FB4D6; --score-moderate: #56C2D6; --score-promising: #F0CB55;
  --score-strong: #F59A62; --score-exceptional: #7FE0A8;
  --status-good: #7FE0A8; --status-good-soft: #173629; --status-warning: #F0B450;
  --status-warning-soft: #3A2D12; --danger: #F07A70; --danger-soft: #40222A;
  --muted: #AD9EC2;
  --atmosphere-1: rgba(255, 160, 120, 0.10); --atmosphere-2: rgba(255, 196, 107, 0.12);
}
:root:root:root h1 { text-shadow: 0 2px 30px rgba(20, 10, 40, .7); }
:root:root:root .stats-new { border-color: #4a3f6e; }
:root:root:root main {
  background: linear-gradient(180deg, transparent 0, transparent 150px, rgba(18, 13, 34, .80) 460px, rgba(18, 13, 34, .86) 100%);
  -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px);
}
#mf-bg {
  background: linear-gradient(180deg, #171340 0%, #2c2158 22%, #6a3478 42%, #c4566a 60%, #f08a5c 72%, #ffb870 82%, #ffe3a0 92%, #fff0c4 100%);
}
.mf-stars {
  inset: 0 0 55% 0; opacity: .55;
  background-image:
    radial-gradient(1.4px 1.4px at 12% 18%, #fff, transparent), radial-gradient(1.2px 1.2px at 28% 8%, #fff, transparent),
    radial-gradient(1.6px 1.6px at 47% 22%, #fff, transparent), radial-gradient(1.2px 1.2px at 63% 10%, #fff, transparent),
    radial-gradient(1.4px 1.4px at 81% 16%, #fff, transparent), radial-gradient(1.2px 1.2px at 92% 6%, #fff, transparent);
}
.mf-skyglow {
  left: 50%; bottom: -6%; width: 150%; height: 70%; transform: translateX(-50%);
  background: radial-gradient(ellipse at 88% 58%, rgba(255, 214, 140, .75), rgba(255, 160, 100, .35) 38%, rgba(255, 120, 110, 0) 70%);
}
.mf-sun {
  left: 90%; bottom: 33%; width: 170px; height: 170px; margin-left: -85px; border-radius: 50%;
  background: radial-gradient(circle at 50% 50%, #fffbe8 0%, #ffe9a8 38%, #ffc66b 70%, rgba(255, 170, 90, 0) 72%);
  box-shadow: 0 0 90px 40px rgba(255, 200, 110, .55);
}
.mf-rays {
  left: 90%; bottom: 33%; width: 900px; height: 900px; margin: 0 0 -450px -450px;
  background: repeating-conic-gradient(from 0deg at 50% 50%, rgba(255, 226, 150, .10) 0deg 4deg, transparent 4deg 14deg);
  -webkit-mask-image: radial-gradient(circle, #000 0%, transparent 62%);
  mask-image: radial-gradient(circle, #000 0%, transparent 62%);
}
.mf-ground { background: linear-gradient(180deg, rgba(12, 8, 22, 0), #0c0816 62%); }
.mf-mote { background: #fff1c0; box-shadow: 0 0 10px 4px rgba(255, 210, 120, .5); }
@media (prefers-reduced-motion: no-preference) {
  .mf-sun, .mf-rays { animation: mf-sunrise 3.4s cubic-bezier(.2, .7, .2, 1) both; }
  .mf-skyglow { animation: mf-glowup 3.4s ease-out both; }
  @keyframes mf-sunrise { from { transform: translateY(120px); opacity: .2; } to { transform: none; opacity: 1; } }
  @keyframes mf-glowup { from { opacity: .25; } to { opacity: 1; } }
}
.mf-offer { background: linear-gradient(160deg, #2c2352, #1d1739); border-color: #6a58a0; }
.mf-offer .seal {
  background: radial-gradient(circle at 35% 30%, #fff1b8, #f2a93f 70%);
  box-shadow: 0 0 22px 6px rgba(255, 200, 110, .55), 0 4px 12px rgba(0, 0, 0, .5);
}
#mf-toggle, #mf-preview { background: rgba(21, 15, 38, .88); }
#mf-win { background: radial-gradient(circle at 50% 88%, rgba(255, 196, 107, .45), rgba(21, 15, 38, .94) 62%); }
@media (max-width: 760px) {
  .mf-sun { left: 70%; bottom: 34%; width: 130px; height: 130px; margin-left: -65px; }
  .mf-rays { left: 70%; bottom: 34%; }
}
```

- [ ] **Step 5: Create `scripts/templates/themes/offer.js`**

```js
// "Offer Season" layer for the forest themes. Injected after the page's own scripts by
// src/job_hunter/theme.py; reads <script type="application/json" id="mf-config">.
// Every dynamic string is written with textContent: the config is user-controlled text.
(function () {
  'use strict';
  var cfgEl = document.getElementById('mf-config');
  if (!cfgEl) return;
  var cfg;
  try { cfg = JSON.parse(cfgEl.textContent); } catch (e) { return; }
  var name = cfg.firstName || '';
  var DAWN = cfg.theme === 'forest-dawn';
  var root = document.documentElement;
  var KEY = 'job-hunter-offer-mode';
  var isRadar = !!document.querySelector('.masthead');

  root.setAttribute('data-theme', cfg.theme);
  try { if (localStorage.getItem(KEY) === 'off') root.classList.add('mf-off'); } catch (e) { /* storage blocked: default on */ }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function rnd(i, s) { var v = Math.sin(i * 12.9898 + s * 78.233) * 43758.5453; return v - Math.floor(v); }
  function isOff() { return root.classList.contains('mf-off'); }

  // ---- backdrop: static pines; moon (night) or sun, rays and stars (dawn); a few motes ----
  function pine(x, base, h, w, fill) {
    var p = [[x, base - h], [x + w * 0.45, base - h * 0.62], [x + w * 0.2, base - h * 0.62],
      [x + w * 0.6, base - h * 0.32], [x + w * 0.25, base - h * 0.32], [x + w * 0.8, base],
      [x - w * 0.8, base], [x - w * 0.25, base - h * 0.32], [x - w * 0.6, base - h * 0.32],
      [x - w * 0.2, base - h * 0.62], [x - w * 0.45, base - h * 0.62]];
    return '<polygon points="' + p.map(function (q) { return q[0].toFixed(0) + ',' + q[1].toFixed(0); }).join(' ') +
      '" fill="' + fill + '"/>';
  }
  function buildBackdrop() {
    var bg = el('div'); bg.id = 'mf-bg'; bg.setAttribute('aria-hidden', 'true');
    var fills = DAWN ? ['#1b1236', '#120b25', '#0a0614'] : ['#0d1c15', '#08130e', '#040906'];
    var W = 1600, H = 500, s = '', i;
    for (i = 0; i < 22; i++) s += pine(i * 76 + rnd(i, 1) * 30, H * 0.86, H * (0.35 + rnd(i, 2) * 0.3), 62 + rnd(i, 3) * 30, fills[0]);
    for (i = 0; i < 13; i++) s += pine(i * 128 + rnd(i, 4) * 50, H * 0.98, H * (0.5 + rnd(i, 5) * 0.4), 100 + rnd(i, 6) * 40, fills[1]);
    for (i = 0; i < 4; i++) {
      s += pine(i * 130 + 30, H * 1.02, H * (0.8 + rnd(i, 7) * 0.2), 150, fills[2]);
      s += pine(W - i * 130 - 30, H * 1.02, H * (0.8 + rnd(i, 8) * 0.2), 150, fills[2]);
    }
    (DAWN ? ['mf-stars', 'mf-skyglow', 'mf-rays', 'mf-sun'] : ['mf-moon']).forEach(function (c) { bg.appendChild(el('div', c)); });
    var trees = el('div', 'mf-trees');
    trees.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMax slice">' + s + '</svg>';
    bg.appendChild(trees);
    bg.appendChild(el('div', 'mf-ground'));
    for (i = 0; i < 16; i++) {
      var m = el('div', 'mf-mote');
      m.style.left = (rnd(i, 11) * 100) + '%'; m.style.top = (30 + rnd(i, 12) * 55) + '%';
      m.style.animationDelay = (rnd(i, 13) * 4) + 's';
      bg.appendChild(m);
    }
    document.body.insertBefore(bg, document.body.firstChild);
  }

  // ---- affirmations: [with first name, without]; the first shown line is a time-aware greeting ----
  var hr = new Date().getHours();
  var GREET = hr < 5 ? 'Early start' : hr < 12 ? 'Good morning' : hr < 17 ? 'Good afternoon' : 'Good evening';
  var LINES = [
    [GREET + ', {n}. The offer is already on its way.', GREET + '. The offer is already on its way.'],
    ['{n}, the offer is already yours. You\'re just finding the envelope.', 'The offer is already yours. You\'re just finding the envelope.'],
    ['A better, higher-paying role is already on its way to you, {n}.', 'A better, higher-paying role is already on its way to you.'],
    ['Somewhere in here, a hiring manager is already hoping you apply, {n}.', 'Somewhere in here, a hiring manager is already hoping you apply.'],
    ['You\'ve got the offer, {n}. This is just the paperwork part.', 'You\'ve got the offer. This is just the paperwork part.'],
    ['Future {n} is already settled in at the new desk. Say hi.', 'Future you is already settled in at the new desk. Say hi.'],
    ['Today\'s hunt, {n}: find the role that\'s already saying yes.', 'Today\'s hunt: find the role that\'s already saying yes.'],
    ['The right team is already looking for someone exactly like {n}.', 'The right team is already looking for someone exactly like you.'],
    ['{n}\'s new salary has already been decided. Go collect it.', 'Your new salary has already been decided. Go collect it.'],
    ['Every scroll is a step toward \u201cWelcome to the team, {n}.\u201d', 'Every scroll is a step toward \u201cWelcome to the team.\u201d'],
    ['You\'re not looking for a job, {n}. You\'re choosing between offers.', 'You\'re not looking for a job. You\'re choosing between offers.'],
    ['The hardest part is already over, {n}: you started.', 'The hardest part is already over: you started.'],
    ['Congratulations on the new role, {n}. Let\'s find out which one.', 'Congratulations on the new role. Let\'s find out which one.'],
    ['Offer in hand, {n}, feet up. The hunt is just the victory lap.', 'Offer in hand, feet up. The hunt is just the victory lap.'],
    ['Quiet forest, sharp instincts, signed offer letter. Well done, {n}.', 'Quiet forest, sharp instincts, signed offer letter.']
  ];
  var idx = 0;
  function lineText(i) {
    var pair = LINES[i];
    return name ? pair[0].split('{n}').join(name) : pair[1];
  }

  // ---- radar-only decoration: three names, three jobs ----
  function swapText(node, next) {
    node.setAttribute('data-mf-orig', node.textContent);
    node.setAttribute('data-mf-new', next);
    node.textContent = next;
  }
  function decorateRadar() {
    var head = document.querySelector('.masthead');
    var sub = head.querySelector('.subhead');
    var h1 = head.querySelector('h1');
    if (h1) {
      swapText(h1, 'Offer Season');
      h1.insertAdjacentElement('afterend', el('p', 'mf-tag', 'Open season on the right role.'));
    }
    if (sub) {
      var line = el('p', 'mf-line', lineText(idx));
      line.title = 'click for another';
      line.addEventListener('click', function () { idx = (idx + 1) % LINES.length; line.textContent = lineText(idx); });
      sub.insertAdjacentElement('afterend', line);
    }
    var card = el('aside', 'mf-offer');
    card.setAttribute('aria-label', 'Vision card');
    card.appendChild(el('div', 'k', 'Signed & Sealed'));
    card.appendChild(el('div', 't', 'Your next role, already in the envelope.'));
    if (name) card.appendChild(el('div', 'd', 'Dear ' + name + ','));
    card.appendChild(el('div', 'r', cfg.role));
    var pay = el('div', 'p');
    pay.appendChild(el('b', null, cfg.pay));
    pay.appendChild(document.createElement('br'));
    pay.appendChild(document.createTextNode(cfg.when));
    card.appendChild(pay);
    card.appendChild(el('div', 'seal', '\u2713'));
    head.insertBefore(card, head.firstChild);

    var subs = {
      'Strong matches': 'Somewhere in here, it\'s already a yes.',
      'For review': 'it only takes one yes',
      'Below 50': 'warm-ups, in case you\'re curious'
    };
    Array.prototype.forEach.call(document.querySelectorAll('summary.group-head h2'), function (h) {
      var original = h.textContent.trim();
      if (original === 'Strong matches') swapText(h, 'The Yes List');
      if (subs[original]) h.insertAdjacentElement('afterend', el('span', 'mf-sub', subs[original]));
    });
    Array.prototype.forEach.call(document.querySelectorAll('.empty-state'), function (e) {
      e.appendChild(document.createTextNode(' '));
      e.appendChild(el('span', 'mf-note', 'All clear \u2014 nothing standing between you and the offer.'));
    });
    var main = document.querySelector('main');
    if (main) main.appendChild(el('p', 'mf-foot', 'Close the tabs. The offer is already in motion.'));
  }

  // ---- offer mode switch: off restores every swapped label and hides the manifestation text ----
  function relabel(off) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-mf-orig]'), function (n) {
      n.textContent = off ? n.getAttribute('data-mf-orig') : n.getAttribute('data-mf-new');
    });
  }
  function buildControls() {
    var tg = el('button'); tg.id = 'mf-toggle'; tg.type = 'button';
    function paint() {
      tg.textContent = '\u2726 offer mode: ';
      tg.appendChild(el('b', null, isOff() ? 'off' : 'on'));
    }
    paint();
    tg.addEventListener('click', function () {
      root.classList.toggle('mf-off');
      relabel(isOff());
      paint();
      try { localStorage.setItem(KEY, isOff() ? 'off' : 'on'); } catch (e) { /* storage blocked */ }
    });
    document.body.appendChild(tg);
    var pv = el('button', null, 'preview: the moment'); pv.id = 'mf-preview'; pv.type = 'button';
    pv.addEventListener('click', celebrate);
    document.body.appendChild(pv);
  }

  // ---- the moment ----
  var win = el('div'); win.id = 'mf-win'; win.setAttribute('role', 'dialog');
  win.setAttribute('aria-label', 'You got the offer');
  var h2 = el('h2');
  h2.appendChild(document.createTextNode('You got the '));
  h2.appendChild(el('em', null, 'offer' + (name ? ', ' + name : '') + '.'));
  win.appendChild(h2);
  win.appendChild(el('p', null, 'Of course you did.'));
  (function () {
    for (var i = 0; i < 26; i++) {
      var sp = el('div', 'mf-spark');
      sp.style.left = (rnd(i, 21) * 100) + '%'; sp.style.animationDelay = (rnd(i, 22) * 1.4) + 's';
      win.appendChild(sp);
    }
  })();
  var hideTimer = null;
  function hide() { win.classList.remove('on'); }
  win.addEventListener('click', hide);
  function celebrate() {
    win.classList.add('on');
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 5200);
  }
  window.addEventListener('jobhunter:application-status', function (evt) {
    var d = evt && evt.detail;
    if (d && d.status === 'offer' && !isOff()) celebrate();
  });
  window.JobHunterTheme = { celebrate: celebrate };

  buildBackdrop();
  if (isRadar) decorateRadar();
  document.body.appendChild(win);
  buildControls();
  if (isOff()) relabel(true);
})();
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_theme_assets.py tests/test_theme.py -q`
Expected: PASS (`test_js_parses` runs only if `node` exists).

- [ ] **Step 7: Commit**

```bash
git add scripts/templates/themes tests/test_theme_assets.py
git commit -m "feat(theme): add forest, forest-dawn CSS and offer.js assets

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Wire the theme into the static and live radar

**Files:**
- Modify: `scripts/render_radar.py` (imports; `render()` param + token appends; `add_selection_arguments`; new `resolve_page_theme`; `selection_render_kwargs`)
- Test: `tests/test_theme_render.py` (create)

**Interfaces:**
- Consumes: `THEMES`, `EMPTY`, `ResolvedTheme`, `load_theme`, `resolve_theme_name` (Task 2); `Settings.radar` (Task 1); assets (Task 3).
- Produces: `render(..., theme: ResolvedTheme | None = None)`; `resolve_page_theme(args, settings, profile) -> ResolvedTheme`; `--theme {auto,forest,forest-dawn}` on `add_selection_arguments`; `selection_render_kwargs(...)["theme"]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_theme_render.py`:

```python
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

from job_hunter.config import CandidateProfile, ContactInfo, RadarConfig, Settings, VisionConfig  # noqa: E402
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
    hostile = '</script><img src=x onerror=alert(1)>&"\u2028'
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
    assert kwargs["theme"].script_html.count("mf-config") == 1


def test_an_unknown_cli_theme_is_rejected():
    with pytest.raises(SystemExit):
        _args("--theme", "sunset")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_theme_render.py -q`
Expected: FAIL (`ImportError: cannot import name 'resolve_page_theme'`).

- [ ] **Step 3: Implement** — in `scripts/render_radar.py`:

(a) Add after `from job_hunter.storage import Storage` (keeps isort order):

```python
from job_hunter.theme import EMPTY, THEMES, ResolvedTheme, load_theme, resolve_theme_name
```

(b) Extend the `render(` signature — replace

```python
    live_state: LiveState | None = None,
) -> tuple[str, dict[str, Any]]:
```
with
```python
    live_state: LiveState | None = None,
    theme: ResolvedTheme | None = None,
) -> tuple[str, dict[str, Any]]:
```

(c) Immediately before the line `    # Single pass: substituted text is never rescanned, so data containing a token is inert.` insert:

```python
    # Theme CSS/JS ride on token values the template already has, so `auto` (EMPTY) adds nothing
    # and the default page stays byte-identical.
    active_theme = theme or EMPTY
    tokens["__LIVE_STYLE__"] += active_theme.css
    tokens["__LIVE_SCRIPT__"] += active_theme.script_html
```

(d) In `add_selection_arguments`, after the `--no-collection-fallback` argument (its closing `    )` just before `def selection_render_kwargs(`), add:

```python
    parser.add_argument(
        "--theme", choices=THEMES, default=None,
        help=(
            "page theme for this run (default: settings.yaml's radar.theme; "
            "auto = the plain light/dark page)"
        ),
    )
```

(e) Add directly above `def selection_render_kwargs(`:

```python
def resolve_page_theme(
    args: argparse.Namespace, settings: Any, profile: CandidateProfile | None
) -> ResolvedTheme:
    """`--theme` wins over settings.yaml's radar.theme; `auto` resolves to the empty theme."""
    name = resolve_theme_name(getattr(args, "theme", None), settings.radar.theme)
    return load_theme(name, profile, templates_dir=_TEMPLATE_DIR / "themes")


```

(f) In `selection_render_kwargs`, add one entry to the returned `dict(...)` after `collection_fallback=not args.no_collection_fallback,`:

```python
        theme=resolve_page_theme(args, settings, profile),
```

- [ ] **Step 4: Run tests (new + the golden must stay green)**

Run: `uv run pytest tests/test_theme_render.py tests/test_render_radar.py -q`
Expected: PASS, including `test_static_radar_matches_golden` unmodified.

- [ ] **Step 5: Commit**

```bash
git add scripts/render_radar.py tests/test_theme_render.py
git commit -m "feat(theme): render forest themes on the static and live radar

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Applications page theming

**Files:**
- Modify: `scripts/templates/applications_template.html` (one token)
- Modify: `scripts/render_applications.py` (theme param)
- Modify: `scripts/serve_radar.py:315-319` (pass the theme)
- Create: `tests/fixtures/applications_static_golden.html`
- Test: `tests/test_render_applications.py` (append)

**Interfaces:**
- Consumes: `ResolvedTheme`, `EMPTY` (Task 2), `resolve_page_theme` (Task 4).
- Produces: `render_applications_page(..., theme: ResolvedTheme | None = None)`; token `__THEME_CSS__` directly before `</style>` in `applications_template.html`.

- [ ] **Step 1: Capture the baseline BEFORE editing any renderer** — append to `tests/test_render_applications.py` (add `import os` and `import pytest` to its imports if missing, and `from job_hunter.theme import EMPTY, load_theme`):

```python
_APPS_GOLDEN = Path(__file__).parent / "fixtures" / "applications_static_golden.html"


def _golden_apps():
    return [
        _app(job_id="1", status="offer", applied_at="2026-09-10", title="Perception Engineer"),
        _app(job_id="2", status="applied", notes="Sent a note"),
    ]


def test_applications_page_matches_golden():
    page = _page(_golden_apps(), states={"acme|1": "active", "acme|2": "closed"})
    if os.environ.get("UPDATE_APPLICATIONS_GOLDEN") == "1":
        _APPS_GOLDEN.write_text(page, encoding="utf-8")
        pytest.skip("golden regenerated")
    assert page == _APPS_GOLDEN.read_text(encoding="utf-8")
```

Run once to write the baseline from the **unchanged** code:

Run: `UPDATE_APPLICATIONS_GOLDEN=1 uv run pytest tests/test_render_applications.py::test_applications_page_matches_golden -q`
Expected: 1 skipped ("golden regenerated"), file `tests/fixtures/applications_static_golden.html` created.

Run: `uv run pytest tests/test_render_applications.py -q`
Expected: PASS.

- [ ] **Step 2: Write the failing theme tests** — append to `tests/test_render_applications.py`:

```python
_THEMES_DIR = Path(__file__).parents[1] / "scripts" / "templates" / "themes"


def _themed_page(name):
    theme = load_theme(
        name,
        CandidateProfile(contact=ContactInfo(name="Jane Doe", email="jane@real-mail.test")),
        templates_dir=_THEMES_DIR,
    )
    return render_applications_page(
        applications=_golden_apps(), job_states={}, versions=VERSIONS, today=TODAY,
        archive_name="a.json", theme=theme,
    )


def test_auto_theme_leaves_the_applications_page_unchanged():
    plain = _page(_golden_apps(), states={"acme|1": "active", "acme|2": "closed"})
    again = render_applications_page(
        applications=_golden_apps(), job_states={"acme|1": "active", "acme|2": "closed"},
        versions=VERSIONS, today=TODAY, archive_name="default_2026-09-26.json", theme=EMPTY,
    )
    assert again == plain
    assert "mf-config" not in plain and "__THEME_CSS__" not in plain


def test_forest_theme_themes_the_applications_page():
    page = _themed_page("forest-dawn")
    assert ":root:root:root" in page and ".mf-sun" in page
    assert page.count('id="mf-config"') == 1
    assert page.rstrip().endswith("</script>")
    assert "__THEME_CSS__" not in page
```

Also add `from job_hunter.config import CandidateProfile, ContactInfo` to the imports of that file.

Run: `uv run pytest tests/test_render_applications.py -q`
Expected: FAIL (`TypeError: render_applications_page() got an unexpected keyword argument 'theme'`).

- [ ] **Step 3: Implement** —

(a) `scripts/templates/applications_template.html`: change the single line `</style>` (line 49) to:

```
__THEME_CSS__</style>
```

(b) `scripts/render_applications.py`: add `from job_hunter.theme import EMPTY, ResolvedTheme` after the `from render_radar import ...` line; extend the signature

```python
def render_applications_page(
    *, applications: list[dict[str, Any]], job_states: dict[str, str], versions: dict[str, str | None],
    today: date, archive_name: str | None = None, theme: ResolvedTheme | None = None,
) -> str:
```

and, after the `for name in _SCRIPTS:` loop (before `page = _TEMPLATE_PATH.read_text(...)`):

```python
    active_theme = theme or EMPTY
    if active_theme.script_html:
        scripts.append(active_theme.script_html)
```

and add to the `tokens = {...}` dict:

```python
        "__THEME_CSS__": active_theme.css,
```

(c) `scripts/serve_radar.py`, in `render_applications_html`, change the call to:

```python
    return render_applications.render_applications_page(
        applications=rows, job_states=states,
        versions=_versions(cfg, feedback_rows, None, rows) | {"archive": None},
        today=date.today(), archive_name=archive_name,
        theme=render_radar.resolve_page_theme(cfg.args, cfg.settings, cfg.profile_loader()),
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_render_applications.py tests/test_serve_radar.py tests/test_render_radar.py tests/test_theme_render.py -q`
Expected: PASS (golden for the Applications page and the radar both unchanged).

- [ ] **Step 5: Commit**

```bash
git add scripts/templates/applications_template.html scripts/render_applications.py scripts/serve_radar.py tests/test_render_applications.py tests/fixtures/applications_static_golden.html
git commit -m "feat(theme): theme the Applications page and pass the theme from serve_radar

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Live "newly reached offer" event

**Files:**
- Modify: `scripts/templates/radar_live_core.js` (add + export `isNewOffer`)
- Modify: `scripts/templates/radar_live_ui.js` (`saveApp`, `onOk`, helper)
- Modify: `scripts/templates/applications_ui.js` (`saveRow`, `onOk`, helper)
- Test: `tests/js/radar_live_core.test.js` (append); `tests/test_theme_assets.py` (append guard)

**Interfaces:**
- Consumes: `L`/`core` = `window.RadarLive` in both UIs; queue items are plain objects, so an extra `celebrate` field survives `enqueue` and `localStorage`.
- Produces: `RadarLive.isNewOffer(prevStatus, nextStatus) -> boolean`; window event `jobhunter:application-status` with `detail: {status: "offer", key: "<source>|<job>"}`, dispatched only after a server ack of a save flagged `celebrate`.

- [ ] **Step 1: Write the failing node test** — append to `tests/js/radar_live_core.test.js`:

```js
test('isNewOffer fires only when a status newly becomes offer', () => {
  assert.equal(core.isNewOffer('', 'offer'), true);
  assert.equal(core.isNewOffer(undefined, 'offer'), true);
  assert.equal(core.isNewOffer('interviewing', 'offer'), true);
  assert.equal(core.isNewOffer('offer', 'offer'), false); // re-saving notes on an existing offer
  assert.equal(core.isNewOffer('applied', 'applied'), false);
  assert.equal(core.isNewOffer('offer', 'rejected'), false);
  assert.equal(core.isNewOffer('offer', null), false); // stop tracking
});
```

Append to `tests/test_theme_assets.py`:

```python
def test_the_live_pages_announce_new_offers():
    templates = DEFAULT_TEMPLATES_DIR.parent
    for name in ("radar_live_ui.js", "applications_ui.js"):
        source = (templates / name).read_text(encoding="utf-8")
        assert "jobhunter:application-status" in source, name
        assert "isNewOffer" in source and "celebrate" in source, name
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test tests/js/radar_live_core.test.js` and `uv run pytest tests/test_theme_assets.py -q`
Expected: FAIL (`core.isNewOffer is not a function`; assertion on the UI files).

- [ ] **Step 3: Implement** —

(a) `radar_live_core.js`: add above `var api = {` :

```js
  // A save that newly reaches "offer" is worth celebrating; re-saving notes on an existing
  // offer, or any other transition, is not.
  function isNewOffer(prevStatus, nextStatus) { return nextStatus === 'offer' && prevStatus !== 'offer'; }

```

and add `isNewOffer: isNewOffer,` to the `api` object (after `shutdownNotice: shutdownNotice`, add a comma first):

```js
    reconcile: reconcile, reconcileApps: reconcileApps, resumeNotice: resumeNotice, shutdownNotice: shutdownNotice,
    isNewOffer: isNewOffer
```

(b) `radar_live_ui.js`: add this helper directly above `function saveApp(row) {`:

```js
  // Fired only after the server acknowledged a save that newly reached "offer"; the forest
  // themes listen for it. With no listener (default theme) nothing happens.
  function announceOffer(key, app) {
    try {
      window.dispatchEvent(new CustomEvent('jobhunter:application-status', {
        detail: { status: app.status, key: key }
      }));
    } catch (e) { /* CustomEvent unavailable: the save already succeeded, skip the flourish */ }
  }

```

In `saveApp`, replace

```js
    setApp(key, appFromPayload(key, payload));
    sync.queue({ kind: 'application', key: key, payload: payload });
```
with
```js
    var celebrate = payload.status !== null && L.isNewOffer(apps[key] ? apps[key].status : '', payload.status);
    setApp(key, appFromPayload(key, payload));
    sync.queue({ kind: 'application', key: key, payload: payload, celebrate: celebrate });
```

In the `onOk` callback's application branch, replace

```js
          setApp(item.key, it ? pick(it) : null);
          applyFilters();
```
with
```js
          setApp(item.key, it ? pick(it) : null);
          applyFilters();
          if (item.celebrate && it && it.status === 'offer') announceOffer(item.key, it);
```

(`announceOffer` is a hoisted function declaration, so defining it below `onOk` is fine.)

(c) `applications_ui.js`: add this helper directly above `function saveRow(row) {`:

```js
  // Fired only after the server acknowledged a save that newly reached "offer".
  function announceOffer(key, app) {
    try {
      window.dispatchEvent(new CustomEvent('jobhunter:application-status', {
        detail: { status: app.status, key: key }
      }));
    } catch (e) { /* CustomEvent unavailable: the save already succeeded, skip the flourish */ }
  }

```

In `saveRow`, replace

```js
    if (status !== 'saved') payload.applied_at = date || todayIso();
    refreshRow(row, {
```
with
```js
    if (status !== 'saved') payload.applied_at = date || todayIso();
    var celebrate = L.isNewOffer(row.dataset.status, status);
    refreshRow(row, {
```
and replace
```js
    sync.queue({ kind: 'application', key: keyOf(row), payload: payload });
  }

  container.addEventListener('change'
```
with
```js
    sync.queue({ kind: 'application', key: keyOf(row), payload: payload, celebrate: celebrate });
  }

  container.addEventListener('change'
```

In `onOk`, replace

```js
        if (it) refreshRow(row, it); else { row.remove(); updateCounts(); }
      }
```
with
```js
        if (it) refreshRow(row, it); else { row.remove(); updateCounts(); }
        if (it && item.celebrate && it.status === 'offer') announceOffer(item.key, it);
      }
```

- [ ] **Step 4: Run tests**

Run: `node --test tests/js/` then `uv run pytest tests/test_radar_live_js.py tests/test_theme_assets.py tests/test_serve_radar.py tests/test_render_radar.py tests/test_render_applications.py -q`
Expected: PASS (both goldens unchanged: the JS edits only change inlined script text, so first confirm: if a golden fails because it embeds live scripts, regenerate **only after** reviewing the diff is limited to the three JS edits above).

> Note: the static radar golden does not embed live scripts (they are empty in static mode) and the Applications golden embeds `applications_ui.js`. If `test_applications_page_matches_golden` fails, run `UPDATE_APPLICATIONS_GOLDEN=1 uv run pytest tests/test_render_applications.py::test_applications_page_matches_golden -q`, then `git diff tests/fixtures/applications_static_golden.html` and confirm the only differences are the `announceOffer` helper, the `celebrate` variable and the two `onOk`/`queue` edits from this task.

- [ ] **Step 5: Commit**

```bash
git add scripts/templates/radar_live_core.js scripts/templates/radar_live_ui.js scripts/templates/applications_ui.js tests/js/radar_live_core.test.js tests/test_theme_assets.py tests/fixtures/applications_static_golden.html
git commit -m "feat(theme): announce newly reached offers from the live pages

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Browser smoke test (Playwright, skipped without Chromium)

**Files:**
- Test: `tests/test_forest_theme_browser.py` (create)

**Interfaces:**
- Consumes: everything above. Uses `render_radar.build`, `load_theme`, and the page contract in Task 3.

- [ ] **Step 1: Write the test** — `tests/test_forest_theme_browser.py`:

```python
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import render_radar  # noqa: E402

from job_hunter.config import CandidateProfile, ContactInfo, VisionConfig  # noqa: E402
from job_hunter.theme import load_theme  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")

THEMES_DIR = render_radar._TEMPLATE_DIR / "themes"
FOREST_PAPER = "#08110d"


def _page_file(tmp_path, theme_name, name="Jane Doe", **vision):
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
    profile = CandidateProfile(
        contact=ContactInfo(name=name, email="jane@real-mail.test"), vision=VisionConfig(**vision)
    )
    out = tmp_path / f"{theme_name}.html"
    render_radar.build(
        output_path=out, search_path=search, assessments_path=assess, title="Test Radar",
        keyword_label=None, new_days=10, now=datetime(2026, 8, 31, tzinfo=UTC),
        theme=load_theme(theme_name, profile, templates_dir=THEMES_DIR),
    )
    return out


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # chromium not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield instance
        instance.close()


def _open(browser, path, color_scheme="light", init_script=None):
    context = browser.new_context(color_scheme=color_scheme)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" and "Failed to load resource" not in m.text else None)
    page.route("**/fonts.googleapis.com/**", lambda route: route.abort())
    page.route("**/fonts.gstatic.com/**", lambda route: route.abort())
    if init_script:
        page.add_init_script(init_script)
    page.goto(path.as_uri(), wait_until="domcontentloaded")
    page.wait_for_selector("#mf-bg", state="attached")
    return context, page, errors


@pytest.mark.parametrize("theme", ["forest", "forest-dawn"])
def test_titles_swap_restore_and_celebrate(browser, tmp_path, theme):
    context, page, errors = _open(browser, _page_file(tmp_path, theme))
    try:
        assert page.inner_text("h1") == "Offer Season"
        assert "Dear Jane," in page.inner_text(".mf-offer")
        assert "The Yes List" in page.inner_text("summary.group-head >> nth=1")
        assert page.get_attribute("html", "data-theme") == theme
        page.evaluate("window.JobHunterTheme.celebrate()")
        assert "on" in page.get_attribute("#mf-win", "class")
        assert "jane" in page.inner_text("#mf-win h2").lower()  # CSS upper-cases the headline
        page.click("#mf-toggle")
        assert page.inner_text("h1") == "Candidate Radar"
        assert "Strong matches" in page.inner_text("summary.group-head >> nth=1")
        assert not page.is_visible(".mf-line")
        assert errors == []
    finally:
        context.close()


def test_celebration_rules_follow_offer_mode(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        fire = ("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
                "{detail:{status:'offer'}}))")
        page.evaluate(fire)
        assert "on" in page.get_attribute("#mf-win", "class")
        page.click("#mf-win")
        page.click("#mf-toggle")
        page.evaluate(fire)
        assert "on" not in (page.get_attribute("#mf-win", "class") or "")
        page.evaluate("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
                      "{detail:{status:'applied'}}))")
        assert "on" not in (page.get_attribute("#mf-win", "class") or "")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_forest_palette_wins_over_the_os_color_scheme(browser, tmp_path, scheme):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"), color_scheme=scheme)
    try:
        paper = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--paper').trim().toLowerCase()"
        )
        assert paper == FOREST_PAPER
    finally:
        context.close()


def test_hostile_vision_text_stays_text(browser, tmp_path):
    hostile = "<img src=x onerror=window.__pwned=1>"
    path = _page_file(tmp_path, "forest", name="Jane Doe", role=hostile, pay_note=hostile, start_note=hostile)
    context, page, errors = _open(browser, path)
    try:
        assert page.evaluate("document.querySelectorAll('.mf-offer img').length") == 0
        assert page.evaluate("window.__pwned === undefined")
        assert hostile in page.inner_text(".mf-offer")
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("name", ["Your Name", ""])
def test_no_usable_name_never_prints_junk(browser, tmp_path, name):
    context, page, errors = _open(browser, _page_file(tmp_path, "forest-dawn", name=name))
    try:
        text = page.inner_text("body")
        assert "Dear" not in text
        assert "undefined" not in text and "null" not in text
        page.evaluate("window.JobHunterTheme.celebrate()")
        assert page.inner_text("#mf-win h2").strip().lower() == "you got the offer."
        assert errors == []
    finally:
        context.close()


def test_a_throwing_localstorage_does_not_break_the_page(browser, tmp_path):
    blocker = (
        "Object.defineProperty(window, 'localStorage', {get(){ throw new Error('blocked'); }});"
    )
    context, page, errors = _open(browser, _page_file(tmp_path, "forest"), init_script=blocker)
    try:
        assert page.inner_text("h1") == "Offer Season"
        page.click("#mf-toggle")
        assert page.inner_text("h1") == "Candidate Radar"
    finally:
        context.close()
```

> The last test deliberately does not assert on console errors: the template's own feedback script also touches `localStorage` (inside try/catch). What matters is that the theme still renders and toggles.

- [ ] **Step 2: Run the browser tests**

Run: `uv run pytest tests/test_forest_theme_browser.py -q`
Expected: PASS where Playwright + Chromium exist (`uv run playwright install chromium` once, per CLAUDE.md); otherwise `skipped`. Fix any real failure in the assets (Task 3) rather than loosening the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_forest_theme_browser.py
git commit -m "test(theme): browser smoke tests for both forest themes

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Docs, screenshots, final verification

**Files:**
- Modify: `README.md`, `docs/USAGE.md`, `docs/SPEC.md`, `CLAUDE.md`
- Create: `docs/img/theme-forest.png`, `docs/img/theme-forest-dawn.png`

**Interfaces:**
- Consumes: all prior tasks. Produces: user-facing docs and a green full suite.

- [ ] **Step 1: Capture real screenshots from the implemented code** (fictional data, name "Jane") — save as a throwaway script outside the repo, e.g. `/tmp/shoot_themes.py`, then run it with `uv run python /tmp/shoot_themes.py`:

```python
import json, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "scripts")
import render_radar
from playwright.sync_api import sync_playwright

from job_hunter.config import CandidateProfile, ContactInfo
from job_hunter.theme import load_theme

out = Path("/tmp/theme-shots"); out.mkdir(exist_ok=True)
now = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
rows = [("Example Motors", "Senior Perception Engineer", "Remote (U.S.)", "remote", "available", "$185,000 - $215,000", 94, 2),
        ("Sample Robotics", "Validation Lead, Driver Assistance", "Detroit, MI", "hybrid", "unmentioned", "$165,000 - $190,000", 88, 1),
        ("Placeholder Dynamics", "Staff Sensor Fusion Engineer", "Austin, TX", "hybrid", "available", None, 86, 3),
        ("Acme Autonomy", "Simulation Architect", "San Jose, CA", "onsite", "not_available", "$175,000 - $205,000", 81, 6),
        ("Northwind Vehicles", "ADAS Systems Engineer", "Remote (U.S.)", "remote", "unmentioned", None, 72, 2),
        ("Contoso Cars", "Test Automation Engineer", "Seattle, WA", "hybrid", "unmentioned", None, 58, 9)]
cands, assess = [], []
for i, (co, title, loc, arr, spon, sal, score, age) in enumerate(rows, 1):
    cands.append({"source_key": "demo", "job_id": str(i), "posted_at": (now - timedelta(days=age)).isoformat(),
                  "first_seen_at": (now - timedelta(days=min(age, 3))).isoformat(), "location_raw": loc,
                  "visa_sponsorship": spon, "sponsorship_evidence": None, "salary_evidence": sal,
                  "work_arrangement": arr, "company": co, "title": title, "url": f"https://example.com/{i}"})
    assess.append({"source_key": "demo", "job_id": str(i), "score": score, "company": co, "title": title,
                   "url": f"https://example.com/{i}", "matches": ["m"], "gaps": ["g"]})
(out / "s.json").write_text(json.dumps({"summary": {"sources_succeeded": 83, "sources_attempted": 84},
                                        "candidates": cands, "source_health": []}))
(out / "a.json").write_text(json.dumps(assess))
profile = CandidateProfile(contact=ContactInfo(name="Jane Doe", email="jane@real-mail.test"))
with sync_playwright() as p:
    browser = p.chromium.launch()
    for name in ("forest", "forest-dawn"):
        html = out / f"{name}.html"
        render_radar.build(output_path=html, search_path=out / "s.json", assessments_path=out / "a.json",
                           title="Candidate Radar", keyword_label=None, new_days=10, now=now,
                           theme=load_theme(name, profile))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(html.as_uri()); page.wait_for_timeout(4000)
        page.screenshot(path=str(Path("docs/img") / f"theme-{name}.png"))
    browser.close()
print("done")
```

Run: `uv run python /tmp/shoot_themes.py`
Expected: `done`; `docs/img/theme-forest.png` and `docs/img/theme-forest-dawn.png` exist. Open both and confirm the title reads "Offer Season", the offer card shows "Dear Jane,", and rows are readable.

- [ ] **Step 2: README** — add a section after "Run it your way" (before "Company coverage"):

````markdown
## Make the hunt feel good (optional themes)

The radar can wear a forest: **`forest`** (night) or **`forest-dawn`** (sunrise), with a light
"Offer Season" layer: a daily affirmation, a *Signed & Sealed* vision card, and a *You got the
offer* moment the moment you set an application to **Offer**. It is cosmetic only: scores,
filters and data never change, and **offer mode** (bottom-left) switches the words off and
restores the plain labels.

```yaml
# config/settings.yaml
radar:
  theme: forest-dawn   # auto (default) | forest | forest-dawn
```

One-off: `uv run python scripts/render_radar.py --theme forest`.
Personalise (all optional, in your git-ignored `config/candidate_profile.yaml`): your first name
comes from `contact.name`; set `vision:` for the role and notes, or `use_first_name: false` to keep
your name out of the pages.

<p align="center"><img src="docs/img/theme-forest.png" alt="Forest (night) theme" width="400"> <img src="docs/img/theme-forest-dawn.png" alt="Forest dawn (sunrise) theme" width="400"></p>
````

- [ ] **Step 3: `docs/USAGE.md`** — append a section:

````markdown
## Themes (forest, forest-dawn)

`config/settings.yaml` → `radar.theme`: `auto` (default, the plain light/dark page), `forest`
(night) or `forest-dawn` (sunrise). `render_radar.py --theme NAME` overrides it for one run;
`serve_radar.py` and `job-hunter pipeline` read the setting. The theme applies to the static radar,
the live radar and the Applications page.

Optional `vision:` block in `config/candidate_profile.yaml` (display text only):

```yaml
vision:
  role: "Staff Perception Engineer"   # shown on the Signed & Sealed card
  pay_note: "Better. Higher paying."
  start_note: "Start date: soon"
  use_first_name: true                 # false keeps your name out of the generated pages
```

The first name is `contact.name` without a leading honorific; it is omitted if missing, still the
example placeholder, or `use_first_name: false`. The page's **offer mode** switch (bottom-left,
remembered in the browser) hides every manifestation line and restores the original labels.
Rendered pages contain your first name, so mind screenshots you share.
````

- [ ] **Step 4: `docs/SPEC.md`** — append under the radar rendering section (find with `grep -n "render_radar" docs/SPEC.md`; if no suitable heading, append at the end):

```markdown
### Page themes (`radar.theme`)

`src/job_hunter/theme.py` resolves `--theme` > `settings.radar.theme` > `auto` and packages
`scripts/templates/themes/{forest.css,forest-dawn.css,offer.js}` into a `ResolvedTheme(name, css,
script_html)`. The radar appends `css`/`script_html` to its `__LIVE_STYLE__`/`__LIVE_SCRIPT__` token
values; the Applications page uses a `__THEME_CSS__` token before `</style>` and appends the script
to `__SCRIPTS__`. `auto` is empty, so default output is byte-identical (golden tests). The config
block is `<script type="application/json" id="mf-config">` with keys `theme`, `firstName`, `role`,
`pay`, `when`, escaped with `json_for_script`; `offer.js` writes every dynamic string with
`textContent`. The live pages dispatch `window` event `jobhunter:application-status`
(`detail: {status: "offer", key}`) after the server acknowledges a save that newly reached `offer`
(`RadarLive.isNewOffer`); the theme listens and shows the celebration unless offer mode is off.
```

- [ ] **Step 5: `CLAUDE.md`** — in the Architecture list, after the `scripts/serve_radar.py` bullet, add:

```markdown
- **`src/job_hunter/theme.py`** (+ `scripts/templates/themes/`) — opt-in `forest`/`forest-dawn` page
  themes chosen by `settings.radar.theme` (`render_radar.py --theme` overrides). Pure resolver: `auto`
  returns empty strings so default pages stay byte-identical (the radar and Applications goldens
  guard this). CSS rides on existing token values (`__LIVE_STYLE__`/`__LIVE_SCRIPT__`, plus
  `__THEME_CSS__` on the Applications template) because tokens are substituted in one regex pass —
  adjacent tokens would mis-tokenise. Theme CSS uses `:root:root:root` to beat the page's dark-mode
  block. The first name is `contact.first_name` minus honorifics, dropped for placeholders or
  `vision.use_first_name: false`; all dynamic text is written with `textContent`. Live pages dispatch
  `jobhunter:application-status` only for a save that *newly* reaches `offer`.
```

- [ ] **Step 6: Full verification**

Run each and expect success:
- `uv run pytest -q` — full suite green (browser tests skip if Chromium is absent).
- `node --test tests/js/` — green.
- `uv run ruff check .` — no findings.
- `git diff --stat dev...HEAD` — only the files listed in this plan.

Open `/tmp/theme-shots/forest.html` and `forest-dawn.html` once in a real browser; verify: switch offer mode off/on, click the affirmation, press **preview: the moment**.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/USAGE.md docs/SPEC.md CLAUDE.md docs/img/theme-forest.png docs/img/theme-forest-dawn.png
git commit -m "docs(theme): document forest themes, vision block and event contract

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Addendum: Task 9 — email-style offer card, banner-then-moment, no-overlap (2026-10-03)

Spec: section 11 of the design doc. Branch state: Tasks 1-8 are implemented; this is a follow-up that
touches only `offer.js`, the two CSS files, three test files and docs. **Global Constraints and
Review Focus above still apply** (ASCII assets, `textContent` only, golden tests untouched,
fake names only). Extra Review Focus lines for this task, each pinned by a test below:

6. **Expanded card must not overlap the stats block** at desktop width, and must reserve nothing on a
   narrow (static) layout. *(Step 1, `test_expanded_mail_card_clears_the_stats_block`)*
7. **Banner timing:** an offer event shows the banner first and the celebration after it; offer mode
   off shows neither; **Accept offer** celebrates at once with no banner. *(Step 1)*

### Task 9: Mail card, sliding banner, spacing fix

**Files:**
- Modify: `scripts/templates/themes/offer.js` (replace the card builder; add `announce`, `syncGap`)
- Modify: `scripts/templates/themes/forest.css` (replace the `.mf-offer` letter styles; add banner + gap)
- Modify: `scripts/templates/themes/forest-dawn.css` (drop the card/seal overrides)
- Test: `tests/test_forest_theme_browser.py`, `tests/test_theme_assets.py`
- Docs: `README.md`, `docs/USAGE.md`, `docs/SPEC.md`, `CLAUDE.md`, `docs/img/theme-*.png`

**Interfaces:**
- Produces: DOM `aside.mf-offer.mf-mail` (`role=button`, `aria-expanded`, class `open` when expanded) with
  children `.mm-top`, `.mm-row`, `.mm-body` (hidden until open), `.mm-accept`; `div.mf-mail-gap`
  before the first `.stats-group`; `#mf-toast.mf-toast.mf-mail` (class `in` while visible);
  `window.JobHunterTheme = { celebrate, announce }`.

- [ ] **Step 1: Write the failing tests.**

In `tests/test_forest_theme_browser.py`, change `_open` to accept a reduced-motion flag and viewport
(replace its first lines `def _open(...)` / `context = browser.new_context(...)`):

```python
def _open(browser, path, color_scheme="light", init_script=None, reduced_motion=None, viewport=None):
    options = {"color_scheme": color_scheme}
    if reduced_motion:
        options["reduced_motion"] = reduced_motion
    if viewport:
        options["viewport"] = viewport
    context = browser.new_context(**options)
```

Replace `test_celebration_rules_follow_offer_mode` with:

```python
FIRE = ("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
        "{detail:{status:'offer'}}))")


def _win_on(page):
    return "on" in (page.get_attribute("#mf-win", "class") or "")


def test_an_offer_event_shows_the_banner_then_the_moment(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        page.evaluate(FIRE)
        page.wait_for_selector("#mf-toast.in")
        assert not _win_on(page), "the banner comes first"
        page.wait_for_selector("#mf-win.on", timeout=6000)
        page.wait_for_selector("#mf-toast", state="detached", timeout=3000)
        page.click("#mf-win")
        page.evaluate("window.dispatchEvent(new CustomEvent('jobhunter:application-status',"
                      "{detail:{status:'applied'}}))")
        page.wait_for_timeout(300)
        assert page.locator("#mf-toast").count() == 0 and not _win_on(page)
    finally:
        context.close()


def test_offer_mode_off_shows_neither_banner_nor_moment(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        page.click("#mf-toggle")
        page.evaluate(FIRE)
        page.wait_for_timeout(2600)
        assert page.locator("#mf-toast").count() == 0 and not _win_on(page)
    finally:
        context.close()


def test_accept_offer_celebrates_at_once_without_a_banner(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest-dawn"))
    try:
        page.click(".mf-offer")
        page.click(".mm-accept")
        assert _win_on(page)
        assert page.locator("#mf-toast").count() == 0
    finally:
        context.close()


def test_the_mail_card_expands_and_collapses_by_click_and_keyboard(browser, tmp_path):
    context, page, _ = _open(browser, _page_file(tmp_path, "forest"))
    try:
        card = page.locator(".mf-offer")
        assert card.get_attribute("aria-expanded") == "false"
        assert not page.is_visible(".mm-body")
        assert "Hiring Team" in card.inner_text() and "Your future employer" in card.inner_text()
        card.click()
        assert card.get_attribute("aria-expanded") == "true" and page.is_visible(".mm-body")
        assert "Dear Jane," in page.inner_text(".mm-body")
        card.focus()
        page.keyboard.press("Enter")
        assert card.get_attribute("aria-expanded") == "false"
    finally:
        context.close()


def test_expanded_mail_card_clears_the_stats_block(browser, tmp_path):
    context, page, _ = _open(
        browser, _page_file(tmp_path, "forest-dawn"), reduced_motion="reduce",
        viewport={"width": 1440, "height": 900},
    )
    try:
        page.click(".mf-offer")
        card_bottom = page.evaluate("document.querySelector('.mf-offer').getBoundingClientRect().bottom")
        stats_top = page.evaluate("document.querySelector('.stats-group').getBoundingClientRect().top")
        assert stats_top - card_bottom >= 31, (card_bottom, stats_top)
        page.click(".mf-offer")
        assert page.evaluate("document.querySelector('.mf-mail-gap').offsetHeight") == 0
    finally:
        context.close()


def test_a_narrow_layout_reserves_no_gap(browser, tmp_path):
    context, page, _ = _open(
        browser, _page_file(tmp_path, "forest"), reduced_motion="reduce",
        viewport={"width": 430, "height": 900},
    )
    try:
        page.click(".mf-offer")
        assert page.evaluate("document.querySelector('.mf-mail-gap').offsetHeight") == 0
        assert page.evaluate("getComputedStyle(document.querySelector('.mf-offer')).position") == "static"
    finally:
        context.close()
```

In `test_hostile_vision_text_stays_text`, expand the card before reading text — replace its
`assert hostile in page.inner_text(".mf-offer")` with:

```python
        page.click(".mf-offer")
        assert hostile in page.inner_text(".mf-offer")
```

In `test_titles_swap_restore_and_celebrate` keep `"Dear Jane," in page.inner_text(".mf-offer")`
(the collapsed preview carries it) and change nothing else.

In `tests/test_theme_assets.py` extend the selector tuple in
`test_css_outranks_the_page_dark_mode_block_and_defines_the_components` to
`("#mf-bg", "#mf-win", "#mf-toggle", "#mf-preview", ".mf-offer", ".mf-mail", ".mf-toast", ".mf-mail-gap", ".mm-accept", ".mf-line")`
and add to `test_js_is_safe_by_construction`:

```python
    assert "announce" in js and "mf-toast" in js and "syncGap" in js
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_forest_theme_browser.py tests/test_theme_assets.py -q`
Expected: FAIL (no `#mf-toast`, no `.mm-body`, no `.mf-mail-gap`; asset selectors missing).

- [ ] **Step 3: Implement `offer.js`.** Add this block directly above `function decorateRadar() {`:

```js
  // ---- the offer email: one builder for the masthead card and the sliding banner ----
  var WHO = name ? 'Dear ' + name + ',' : 'Hello,';
  var MAIL_GAP_PX = 32; // matches the page's own vertical rhythm between blocks
  var mailCard = null, mailGap = null;
  function mailHead() {
    var frag = document.createDocumentFragment();
    var top = el('div', 'mm-top');
    var app = el('span', 'mm-app');
    app.appendChild(el('i', 'mm-icon'));
    app.appendChild(document.createTextNode('MAIL'));
    top.appendChild(app);
    top.appendChild(el('span', null, 'now'));
    frag.appendChild(top);
    var row = el('div', 'mm-row');
    row.appendChild(el('div', 'mm-av', 'H'));
    var txt = el('div', 'mm-txt');
    var from = el('div', 'mm-from', 'Hiring Team ');
    from.appendChild(el('small', null, '· Your future employer'));
    txt.appendChild(from);
    var subj = el('div', 'mm-subj');
    subj.appendChild(el('b', 'mm-dot'));
    subj.appendChild(document.createTextNode('Offer of employment: ' + cfg.role));
    txt.appendChild(subj);
    txt.appendChild(el('div', 'mm-prev',
      WHO + ' we’re delighted to offer you the position of ' + cfg.role + '. ' + cfg.pay + ' ' + cfg.when));
    row.appendChild(txt);
    frag.appendChild(row);
    return frag;
  }
  // Keep the expanded email from covering the stats block: reserve exactly the missing space.
  function syncGap() {
    if (!mailCard || !mailGap) return;
    var stats = mailGap.parentNode && mailGap.parentNode.querySelector('.stats-group');
    var need = 0;
    if (stats && mailCard.classList.contains('open') && getComputedStyle(mailCard).position === 'absolute') {
      var resting = stats.getBoundingClientRect().top - mailGap.offsetHeight;
      need = Math.max(0, Math.ceil(mailCard.getBoundingClientRect().bottom + MAIL_GAP_PX - resting));
    }
    mailGap.style.height = need + 'px';
  }
  function buildMailCard() {
    var card = el('aside', 'mf-offer mf-mail');
    card.setAttribute('aria-label', 'Offer email');
    card.setAttribute('role', 'button');
    card.setAttribute('aria-expanded', 'false');
    card.tabIndex = 0;
    card.appendChild(mailHead());
    var body = el('div', 'mm-body');
    body.appendChild(el('p', null, WHO));
    var p2 = el('p');
    p2.appendChild(document.createTextNode('We’re delighted to offer you the position of '));
    p2.appendChild(el('b', null, cfg.role));
    p2.appendChild(document.createTextNode('. '));
    p2.appendChild(el('span', 'hl', cfg.pay));
    body.appendChild(p2);
    body.appendChild(el('p', null, cfg.when + '. Welcome to the team.'));
    var sig = el('p');
    sig.appendChild(document.createTextNode('Warm regards,'));
    sig.appendChild(document.createElement('br'));
    sig.appendChild(document.createTextNode('The Hiring Team'));
    body.appendChild(sig);
    var foot = el('div', 'mm-foot');
    foot.appendChild(el('span', 'mm-att', 'Offer_Letter.pdf · 1 page'));
    var accept = el('button', 'mm-accept', 'Accept offer ✓');
    accept.type = 'button';
    foot.appendChild(accept);
    body.appendChild(foot);
    card.appendChild(body);
    function toggle() {
      var open = card.classList.toggle('open');
      card.setAttribute('aria-expanded', open ? 'true' : 'false');
      syncGap();
    }
    card.addEventListener('click', function (evt) {
      if (evt.target.closest('.mm-accept')) { celebrate(); return; }
      toggle();
    });
    card.addEventListener('keydown', function (evt) {
      if ((evt.key === 'Enter' || evt.key === ' ') && evt.target === card) { evt.preventDefault(); toggle(); }
    });
    return card;
  }

```

Inside `decorateRadar`, replace the whole old card block (from `var card = el('aside', 'mf-offer');`
through `head.insertBefore(card, head.firstChild);`) with:

```js
    mailGap = el('div', 'mf-mail-gap');
    mailGap.setAttribute('aria-hidden', 'true');
    var firstStats = head.querySelector('.stats-group');
    if (firstStats) head.insertBefore(mailGap, firstStats); else head.appendChild(mailGap);
    mailCard = buildMailCard();
    head.insertBefore(mailCard, head.firstChild);
    window.addEventListener('resize', syncGap);
```

In `buildControls`, change the preview wiring `pv.addEventListener('click', celebrate);` to
`pv.addEventListener('click', announce);`.

Replace the block from `window.addEventListener('jobhunter:application-status', ...` through
`window.JobHunterTheme = { celebrate: celebrate };` with:

```js
  // The banner slides in first, then the full-screen moment; both stay silent in offer mode off.
  var TOAST_MS = 1800;
  var announcing = false;
  function announce() {
    if (announcing || isOff()) return;
    announcing = true;
    var toast = el('div', 'mf-toast mf-mail');
    toast.id = 'mf-toast';
    toast.setAttribute('role', 'status');
    toast.appendChild(mailHead());
    document.body.appendChild(toast);
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () { toast.classList.add('in'); });
    });
    setTimeout(function () {
      toast.classList.remove('in');
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
        announcing = false;
      }, 450);
      if (!isOff()) celebrate();
    }, TOAST_MS);
  }
  window.addEventListener('jobhunter:application-status', function (evt) {
    var d = evt && evt.detail;
    if (d && d.status === 'offer') announce();
  });
  window.JobHunterTheme = { celebrate: celebrate, announce: announce };
```

- [ ] **Step 4: Implement the CSS.** In `forest.css`, delete the old rules from `.mf-offer {` through the
`@media (max-width: 760px) { .mf-offer { ... } }` block (the card, `.k`, `.t`, `.d`, `.r`, `.p`,
`.seal` and the narrow-screen rule) and put this in their place:

```css
/* the offer email: a mail notification that opens into the letter */
.mf-mail {
  width: 348px; padding: 12px 14px 14px; text-align: left; cursor: pointer;
  background: var(--surface);
  background: color-mix(in srgb, var(--surface) 90%, transparent);
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
  border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 12px 34px rgba(0, 0, 0, .5);
  font-family: "IBM Plex Sans", system-ui, sans-serif;
}
.mf-offer.mf-mail { position: absolute; right: 0; top: 0; z-index: 20; }
.mf-mail:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.mf-mail .mm-top {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;
  font: 600 10.5px "IBM Plex Mono", monospace; letter-spacing: .1em; color: var(--muted);
}
.mf-mail .mm-app { display: inline-flex; align-items: center; gap: 7px; }
.mf-mail .mm-icon { display: inline-block; position: relative; width: 16px; height: 12px; border-radius: 3px; background: var(--accent); }
.mf-mail .mm-icon::after {
  content: ""; position: absolute; left: 50%; top: 2px; margin-left: -6px; width: 0; height: 0;
  border-left: 6px solid transparent; border-right: 6px solid transparent; border-top: 5px solid rgba(0, 0, 0, .45);
}
.mf-mail .mm-row { display: flex; gap: 11px; }
.mf-mail .mm-av {
  flex: none; width: 38px; height: 38px; border-radius: 50%; background: var(--accent); color: #1a1205;
  display: grid; place-items: center; font: 700 17px "IBM Plex Sans", sans-serif;
}
.mf-mail .mm-txt { min-width: 0; }
.mf-mail .mm-from { font-size: 14px; font-weight: 600; color: var(--ink); }
.mf-mail .mm-from small { font-weight: 400; color: var(--ink-soft); font-size: 12.5px; }
.mf-mail .mm-subj { display: flex; align-items: center; gap: 7px; margin-top: 1px; font-size: 14px; font-weight: 600; color: var(--ink); }
.mf-mail .mm-dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.mf-mail .mm-prev {
  margin-top: 2px; font-size: 13px; line-height: 1.35; color: var(--ink-soft);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.mf-mail .mm-body { display: none; margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--line); font-size: 13.5px; line-height: 1.5; color: var(--ink-soft); }
.mf-mail.open .mm-body { display: block; }
.mf-mail.open .mm-prev { display: none; }
.mf-mail .mm-body p { margin: 0 0 9px; }
.mf-mail .mm-body b { color: var(--ink); }
.mf-mail .mm-body .hl { color: var(--accent); font-weight: 600; }
.mf-mail .mm-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; }
.mf-mail .mm-att { font-size: 12px; color: var(--ink-soft); border: 1px solid var(--line); border-radius: 8px; padding: 5px 9px; }
.mf-mail .mm-att::before {
  content: ""; display: inline-block; width: 9px; height: 11px; margin-right: 7px; vertical-align: -1px;
  border: 1.5px solid var(--accent); border-radius: 2px;
}
.mf-mail .mm-accept {
  font: 600 13px "IBM Plex Sans", sans-serif; color: #1a1205; background: var(--accent);
  border: 0; border-radius: 999px; padding: 7px 14px; cursor: pointer;
}
.mf-mail-gap { height: 0; }
.mf-toast { position: fixed; top: 16px; right: 16px; z-index: 120; cursor: default; pointer-events: none; opacity: 0; transform: translateY(-140%); }
.mf-toast.in { opacity: 1; transform: none; }
@media (prefers-reduced-motion: no-preference) {
  .mf-mail-gap { transition: height .25s ease; }
  .mf-toast { transition: transform .5s cubic-bezier(.2, .8, .2, 1), opacity .3s ease; }
}
@media (max-width: 760px) {
  .mf-offer.mf-mail { position: static; width: auto; margin: 0 0 18px; }
  .mf-toast { left: 12px; right: 12px; width: auto; }
}
```

In the offer-mode-off rule list in `forest.css`, add `html.mf-off .mf-mail-gap` to the selectors that get
`display: none` (keep `.mf-offer` there).

In `forest-dawn.css` delete the `.mf-offer { background: ...; border-color: ...; }` rule and the
`.mf-offer .seal { ... }` rule (lines 52-56); the card now takes its colours from the theme variables.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_forest_theme_browser.py tests/test_theme_assets.py tests/test_theme.py tests/test_theme_render.py tests/test_render_radar.py tests/test_render_applications.py -q`
Expected: PASS (both goldens unchanged: no Python or template change).

- [ ] **Step 6: Docs and screenshots.** Update wording: README ("a *Signed & Sealed* vision card" ->
"an **offer email** that opens like a message, with a sliding mail banner before the celebration"),
`docs/USAGE.md` ("shown on the Signed & Sealed card" -> "used in the offer email card"),
`docs/SPEC.md` and `CLAUDE.md` (one sentence each: card is a mail notification, `announce()` shows the
banner then the moment, `.mf-mail-gap` prevents overlap). Re-shoot `docs/img/theme-forest.png` and
`docs/img/theme-forest-dawn.png` with the Task 8 script (the card now shows the mail banner).

- [ ] **Step 7: Verify and commit**

Run: `uv run pytest -q`, `node --test tests/js/`, `uv run ruff check .` — all green.

```bash
git add scripts/templates/themes tests README.md docs/USAGE.md docs/SPEC.md CLAUDE.md docs/img
git commit -m "feat(theme): email-style offer card, banner-then-moment, no-overlap spacing

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
