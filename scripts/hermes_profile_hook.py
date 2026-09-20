#!/usr/bin/env python3
"""Hermes `post_tool_call` adapter for the candidate-profile diff hook.

Thin runtime-specific shell around `job_hunter.hook_adapter`'s two shared functions (path
matching, and actually running `scripts/diff_profile.py`) — see that module's docstring for why
the logic is centralized there instead of duplicated between this script and its Claude Code
equivalent, `scripts/claude_profile_hook.py`. This script keeps only what's genuinely
Hermes-specific: its own stdin JSON wire shape (`tool_input.path`, not Claude's
`tool_input.file_path`), its own event/tool-name matching (`post_tool_call` /
`write_file`|`patch`, registered by `scripts/install_hermes_hook.py`'s `"^(write_file|patch)$"`
matcher), its own "skip if the edit itself failed" check (`extra.status in {"error",
"blocked"}`), and its own final `print("{}")` — Hermes's hook protocol expects a JSON object on
stdout regardless of what the hook actually did.
"""

import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from job_hunter import hook_adapter


def run(payload: dict, repo_root: Path) -> None:
    if payload.get("hook_event_name") != "post_tool_call":
        return
    if payload.get("tool_name") not in {"write_file", "patch"}:
        return
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return
    cwd = payload.get("cwd")
    cwd_path = Path(cwd) if isinstance(cwd, str) and cwd else None
    if not hook_adapter.should_run_diff(path, repo_root, cwd=cwd_path):
        return
    if (payload.get("extra") or {}).get("status") in {"error", "blocked"}:
        return
    hook_adapter.run_diff(repo_root)


if __name__ == "__main__":
    # Preserve the original hook's best-effort behavior: whatever goes wrong reading/parsing
    # stdin or resolving argv itself (as opposed to a failure *inside* run_diff, which
    # hook_adapter already logs rather than raises) is swallowed here, not surfaced — stdout
    # belongs entirely to Hermes's own `{}` acknowledgment, never to this hook's own errors.
    with suppress(OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        run(json.load(sys.stdin), Path(sys.argv[1]).resolve())
    print("{}")
