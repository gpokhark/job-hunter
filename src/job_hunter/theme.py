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
