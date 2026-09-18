#!/usr/bin/env python3
"""Claude Code `PostToolUse` adapter for the candidate-profile diff hook.

Reads Claude Code's `PostToolUse` JSON payload from stdin — confirmed live against
https://code.claude.com/docs/en/hooks (see `docs/skill-frontmatter-and-hook-plan.md` section
4.4): a payload shaped like `{"hook_event_name": "PostToolUse", "tool_name": "Edit",
"tool_input": {"file_path": "..."}, "cwd": "...", ...}`. `PostToolUse` only ever fires after a
tool call has already succeeded and is advisory by design — Claude Code writes this hook's
stdout to its own debug log only (`PostToolUse` is not one of the handful of event types whose
stdout becomes visible context the way `UserPromptSubmit`'s does), and there is no exit-code-2
*blocking* behavior the way there is for `PreToolUse` (a `PostToolUse`/`PostToolUseFailure` hook
can still exit 2 solely to surface its own stderr back to Claude, since the tool already ran
either way — nothing here needs that; a quietly-refreshed diff report has no reason to interrupt
the conversation). This script therefore always exits 0 — every real failure (`uv` missing,
`diff_profile.py` erroring, a timeout) is `job_hunter.hook_adapter`'s job to log to
`logs/profile-hook.log`, never this script's job to surface via exit code.

Takes the project root as `sys.argv[1]`, passed by `.claude/settings.json`'s hook command as
`${CLAUDE_PROJECT_DIR}` — a value Claude Code itself substitutes before running the command (and
also exports as the `CLAUDE_PROJECT_DIR` environment variable on the spawned process), so this
script never needs to guess or depend on whatever directory the calling process happened to
start in. No `jq`, no shell `case` logic — real Python JSON parsing throughout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# This project's editable install (`uv sync`) already makes `job_hunter` importable from any
# `scripts/*.py` entry point without a `sys.path.insert` hack — see how `scripts/render_radar.py`
# imports `job_hunter.config` the same plain way.
from job_hunter import hook_adapter


def main(argv: list[str]) -> int:
    if not argv:
        return 0
    repo_root = Path(argv[0]).resolve()

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") != "PostToolUse":
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0

    cwd = payload.get("cwd")
    cwd_path = Path(cwd) if isinstance(cwd, str) and cwd else None

    if hook_adapter.should_run_diff(file_path, repo_root, cwd=cwd_path):
        hook_adapter.run_diff(repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
