"""Shared LM Studio reachability probe.

Used by three callers that each need to answer the same question ("is the configured LM
Studio server actually up right now?") and must never disagree about it: `job-hunter doctor`'s
environment check, `scripts/check_lm_studio.py`'s standalone connectivity test, and
`scripts/review_with_lm_studio.py`'s own pre-flight check before it starts sending real review
requests. One implementation, one config loader -- not three independent httpx calls that could
drift (a different timeout, a different endpoint, a different verdict for the same server state).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import yaml


def load_lm_studio_config(path: Path) -> dict[str, Any]:
    """Load `config/lm_studio.yaml`, falling back to `lm_studio.example.yaml` (whose placeholder
    base_url will never actually connect) -- the same fallback `review_with_lm_studio.py` has
    always used, now shared so every caller resolves the same file the same way."""
    if not path.exists():
        example = path.parent / "lm_studio.example.yaml"
        if not example.exists():
            raise FileNotFoundError(
                f"{path} not found and no {example} to fall back to -- copy {example} to "
                f"{path} and edit its base_url."
            )
        print(
            f"job-hunter: {path} not found, falling back to {example} -- its placeholder "
            "base_url will not work; copy it to config/lm_studio.yaml and edit it.",
            file=sys.stderr,
        )
        path = example
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def check_lm_studio(config: dict[str, Any], *, timeout: float = 5.0) -> tuple[bool, str]:
    """Hits LM Studio's OpenAI-compatible `/models` endpoint. Returns (reachable, detail)
    rather than raising, so a caller can report it alongside other pass/fail checks (doctor's
    check list) or print it standalone. `raise_for_status()` matters here, not just a bare
    connection attempt -- a stale base_url pointing at some other, unrelated service on that
    port would otherwise look "reachable" on a plain TCP connect alone."""
    base_url = str(config.get("base_url") or "").rstrip("/")
    if not base_url:
        return False, "no base_url configured"
    try:
        response = httpx.get(f"{base_url}/models", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return False, (
            f"can't reach LM Studio at {base_url} ({exc}). Is the server running (LM Studio > "
            "Developer > Start Server) and is config/lm_studio.yaml's base_url correct and "
            "reachable from this process (not just from your own machine's browser)?"
        )
    try:
        models = [m.get("id", "?") for m in response.json().get("data", [])]
    except (ValueError, AttributeError):
        return True, f"{base_url} -- reachable (response not in the expected models-list shape)"
    return True, f"{base_url} -- {', '.join(models) if models else 'reachable, but no model loaded'}"
