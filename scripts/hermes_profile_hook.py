#!/usr/bin/env python3
"""Hermes post_tool_call adapter for the Claude candidate-profile diff hook."""

import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path


def run(payload: dict, repo_root: Path) -> None:
    if payload.get("hook_event_name") != "post_tool_call":
        return
    if payload.get("tool_name") not in {"write_file", "patch"}:
        return
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return
    edited = Path(path).expanduser()
    if not edited.is_absolute():
        edited = Path(payload.get("cwd") or Path.cwd()) / edited
    if edited.resolve() != (repo_root / "config/candidate_profile.yaml").resolve():
        return
    if (payload.get("extra") or {}).get("status") in {"error", "blocked"}:
        return
    # Preserve the original hook's best-effort behavior; stdout belongs to Hermes.
    subprocess.run(
        ["uv", "run", "python", "scripts/diff_profile.py"],
        cwd=repo_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=55, check=False,
    )


if __name__ == "__main__":
    with suppress(OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        run(json.load(sys.stdin), Path(sys.argv[1]).resolve())
    print("{}")
