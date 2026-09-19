"""Real stdin-payload subprocess tests for `scripts/claude_profile_hook.py` — the Claude Code
`PostToolUse` adapter for the candidate-profile diff hook. Mirrors the equivalent coverage in
`tests/test_hermes_hook.py` for the Hermes adapter (see docs/skill-frontmatter-and-hook-plan.md
section 5): both scripts share `job_hunter.hook_adapter`'s path-matching/run-diff logic (already
unit-tested directly in `tests/test_hook_adapter.py`), so what's worth testing here is the
runtime-specific plumbing — the actual `json.load(sys.stdin)` / `sys.argv[1]` entry point, the
`PostToolUse`/`tool_input.file_path` payload shape, and that the script always exits 0
(PostToolUse is advisory-only; see the module's own docstring)."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _install_fake_uv(bin_dir: Path) -> None:
    """See tests/test_hermes_hook.py's identical helper for why this stands in for a real `uv`:
    it lets `hook_adapter.run_diff`'s genuine `shutil.which("uv")` + `subprocess.run([...])`
    path run end to end without depending on `uv`/LM Studio/a real SQLite pool being available
    wherever this test suite runs."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "sys.exit(subprocess.call([sys.executable] + sys.argv[3:]))\n"
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_stdin_payload_triggers_the_diff_script(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    marker = tmp_path / "diff_ran.marker"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "diff_profile.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    _install_fake_uv(tmp_path / "bin")

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"

    payload = {
        "hook_event_name": "PostToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": "config/candidate_profile.yaml"}, "cwd": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "claude_profile_hook.py"), str(tmp_path)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0
    assert marker.exists()


def test_stdin_payload_for_an_unrelated_file_does_nothing_and_exits_zero():
    payload = {
        "hook_event_name": "PostToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": "README.md"}, "cwd": "/tmp",
    }
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "claude_profile_hook.py"), str(repo_root)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0


def test_wrong_hook_event_name_does_nothing():
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": "config/candidate_profile.yaml"}, "cwd": "/tmp",
    }
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "claude_profile_hook.py"), str(repo_root)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0


def test_malformed_stdin_json_does_not_crash_the_hook():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "claude_profile_hook.py"), str(repo_root)],
        input="not valid json", capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0


def test_missing_project_root_argv_does_not_crash_the_hook():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "claude_profile_hook.py")],
        input="{}", capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0


# --- scripts/run_profile_hook.sh -- the portable launcher .claude/settings.json actually invokes
# (docs/agent-runtime-audit.md's "Claude hook coverage" finding: the previous bare `uv run
# python ...` command in settings.json failed at the shell level, before claude_profile_hook.py
# or hook_adapter.py's own uv-missing diagnostic ever got a chance to run, if `uv` wasn't on the
# invoking process's PATH). Real subprocess tests, not mocks -- this is shell script behavior
# under different PATH contents, exactly the kind of thing a mock can't meaningfully stand in for.


def test_wrapper_delegates_through_a_real_uv_on_path(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    marker = tmp_path / "diff_ran.marker"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "diff_profile.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    _install_fake_uv(tmp_path / "bin")

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"

    payload = {
        "hook_event_name": "PostToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": "config/candidate_profile.yaml"}, "cwd": str(tmp_path),
    }
    proc = subprocess.run(
        [str(SCRIPTS / "run_profile_hook.sh"), str(tmp_path)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0
    assert marker.exists()


def test_wrapper_falls_back_to_python3_and_exits_zero_when_uv_is_missing(tmp_path):
    """No `uv` anywhere on PATH -- the wrapper must fall back to bare `python3` (a best-effort
    path: `job_hunter` genuinely may not be importable outside `uv run`, which is fine -- the
    wrapper's whole job is to never let this failure mode propagate as a shell-level crash) and
    still exit 0, matching every other failure mode in this hook chain being advisory-only."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_python3 = sys.executable
    fake_python3 = bin_dir / "python3"
    fake_python3.write_text(f"#!/bin/sh\nexec {real_python3} \"$@\"\n")
    fake_python3.chmod(fake_python3.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # Also need dirname/cd/pwd/echo/command for the wrapper's own script_dir resolution --
    # exercising "no uv" specifically, not "no shell utilities at all" (a scenario already
    # confirmed live, manually, during this fix's own development).
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}

    proc = subprocess.run(
        [str(SCRIPTS / "run_profile_hook.sh"), str(tmp_path)],
        input="{}", capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0
    assert "uv not found on PATH, falling back to bare python3" in proc.stderr


def test_wrapper_exits_zero_when_neither_uv_nor_python3_is_on_path(tmp_path):
    env = {"PATH": "/dev/null"}  # a directory-less PATH entry -- resolves nothing at all

    proc = subprocess.run(
        [str(SCRIPTS / "run_profile_hook.sh"), str(tmp_path)],
        input="{}", capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0
    assert "neither uv nor python3 found on PATH" in proc.stderr
