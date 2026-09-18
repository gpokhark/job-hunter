import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- direct-call tests: fast, granular coverage of run()'s own scope/dispatch logic ------------
# (kept alongside the subprocess test below, not replaced by it — these don't pay for a process
# spawn per case, and assert exactly which branch of run() short-circuited).


def test_profile_scope_and_working_directory(tmp_path):
    hook = load("hermes_profile_hook")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    payload = {
        "hook_event_name": "post_tool_call", "tool_name": "patch",
        "tool_input": {"path": "config/candidate_profile.yaml"}, "cwd": str(tmp_path),
    }
    with patch.object(hook.hook_adapter, "run_diff") as run_diff:
        hook.run(payload, tmp_path)
        run_diff.assert_called_once_with(tmp_path)

        run_diff.reset_mock()
        hook.run(payload, tmp_path / "other")
        run_diff.assert_not_called()

        payload["extra"] = {"status": "error"}
        hook.run(payload, tmp_path)
        run_diff.assert_not_called()

        payload["tool_name"] = "read_file"
        hook.run(payload, tmp_path)
        run_diff.assert_not_called()


def test_run_does_not_raise_when_hook_adapter_itself_fails(tmp_path):
    """run_diff already logs its own failures and returns False rather than raising — run()
    should simply not care about the return value at all, success or failure."""
    hook = load("hermes_profile_hook")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    payload = {
        "hook_event_name": "post_tool_call", "tool_name": "write_file",
        "tool_input": {"path": "config/candidate_profile.yaml"}, "cwd": str(tmp_path),
    }
    with patch.object(hook.hook_adapter, "run_diff", return_value=False) as run_diff:
        hook.run(payload, tmp_path)
        run_diff.assert_called_once()


def test_install_preserves_config_and_is_idempotent(tmp_path):
    installer = load("install_hermes_hook")
    original = "# Keep backup\nmodel: example\nhooks:\n  post_tool_call:\n    - command: existing\n"
    config = tmp_path / "config.yaml"
    config.write_text(original)
    installer.install(tmp_path, tmp_path / "repo with spaces")
    first = config.read_text()
    installer.install(tmp_path, tmp_path / "repo with spaces")
    assert config.read_text() == first
    parsed = yaml.safe_load(first)
    assert parsed["model"] == "example"
    assert parsed["hooks"]["post_tool_call"][0] == {"command": "existing"}
    assert len(parsed["hooks"]["post_tool_call"]) == 2
    assert (tmp_path / "config.yaml.job-hunter.bak").read_text() == original


def test_uninstall_removes_only_the_matching_entry(tmp_path):
    installer = load("install_hermes_hook")
    config = tmp_path / "config.yaml"
    config.write_text("model: example\nhooks:\n  post_tool_call:\n    - command: existing\n")
    installer.install(tmp_path, tmp_path / "repo")
    installer.uninstall(tmp_path, tmp_path / "repo")
    parsed = yaml.safe_load(config.read_text())
    assert parsed["hooks"]["post_tool_call"] == [{"command": "existing"}]
    # A second uninstall is a no-op, not an error.
    installer.uninstall(tmp_path, tmp_path / "repo")
    parsed_again = yaml.safe_load(config.read_text())
    assert parsed_again["hooks"]["post_tool_call"] == [{"command": "existing"}]


def test_uninstall_with_no_config_does_not_raise(tmp_path):
    installer = load("install_hermes_hook")
    installer.uninstall(tmp_path, tmp_path / "repo")
    assert not (tmp_path / "config.yaml").exists()


# --- real stdin-payload subprocess test ---------------------------------------------------------
# Closes the gap docs/skill-frontmatter-and-hook-plan.md section 2.3 confirmed: the tests above
# call hook.run(...) directly and never exercise the actual `if __name__ == "__main__":` block —
# the real `json.load(sys.stdin)` / `sys.argv[1]` / final `print("{}")` — at all.


def _install_fake_uv(bin_dir: Path) -> None:
    """A stand-in `uv` that just drops the `run`/`python` arguments and execs the rest with the
    real interpreter — lets `hook_adapter.run_diff`'s real `shutil.which("uv")` +
    `subprocess.run([...])` path be exercised genuinely, end to end, without depending on `uv`
    (or LM Studio, or a real SQLite job pool) actually being set up in whatever environment runs
    this test suite."""
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
    # The real `scripts/diff_profile.py` needs a live SQLite pool/config this test has none of —
    # standing in for it here keeps this a genuine stdin-to-subprocess test of the *hook*, not a
    # re-test of diff_profile.py's own behavior (already covered by tests/test_diff_profile.py).
    (tmp_path / "scripts" / "diff_profile.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    _install_fake_uv(tmp_path / "bin")

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"

    payload = {
        "hook_event_name": "post_tool_call", "tool_name": "write_file",
        "tool_input": {"path": "config/candidate_profile.yaml"}, "cwd": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "hermes_profile_hook.py"), str(tmp_path)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert marker.exists()


def test_stdin_payload_for_an_unrelated_file_does_nothing():
    payload = {
        "hook_event_name": "post_tool_call", "tool_name": "write_file",
        "tool_input": {"path": "README.md"}, "cwd": "/tmp",
    }
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "hermes_profile_hook.py"), str(repo_root)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
