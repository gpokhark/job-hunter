"""Unit tests for `job_hunter.hook_adapter` — the shared logic behind both runtime-specific
candidate-profile diff hooks (`scripts/claude_profile_hook.py`, `scripts/hermes_profile_hook.py`).
Deliberately calls the two functions directly rather than going through either script's stdin
entry point — that real stdin-payload coverage lives in `tests/test_claude_hook.py` and
`tests/test_hermes_hook.py` instead, per docs/skill-frontmatter-and-hook-plan.md section 5."""

import subprocess
import time

import pytest

from job_hunter import hook_adapter
from job_hunter.runlock import run_lock

# --- should_run_diff --------------------------------------------------------------------------


def test_absolute_path_matching_the_profile_is_a_match(tmp_path):
    profile = tmp_path / "config" / "candidate_profile.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("target_domains: []\n")
    assert hook_adapter.should_run_diff(str(profile), tmp_path) is True


def test_absolute_path_to_a_different_file_is_not_a_match(tmp_path):
    other = tmp_path / "config" / "settings.yaml"
    other.parent.mkdir(parents=True)
    other.write_text("x: 1\n")
    assert hook_adapter.should_run_diff(str(other), tmp_path) is False


def test_relative_path_resolves_against_explicit_cwd(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    assert hook_adapter.should_run_diff("config/candidate_profile.yaml", tmp_path, cwd=tmp_path) is True


def test_relative_path_resolved_against_wrong_cwd_is_not_a_match(tmp_path, tmp_path_factory):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert hook_adapter.should_run_diff("config/candidate_profile.yaml", tmp_path, cwd=elsewhere) is False


def test_relative_path_with_no_cwd_falls_back_to_process_cwd(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    monkeypatch.chdir(tmp_path)
    assert hook_adapter.should_run_diff("config/candidate_profile.yaml", tmp_path) is True


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / "repo" / "config").mkdir(parents=True)
    (fake_home / "repo" / "config" / "candidate_profile.yaml").write_text("x: 1\n")
    monkeypatch.setenv("HOME", str(fake_home))
    assert hook_adapter.should_run_diff("~/repo/config/candidate_profile.yaml", fake_home / "repo") is True


@pytest.mark.parametrize("bad_path", ["", None])
def test_empty_or_missing_path_is_never_a_match(tmp_path, bad_path):
    assert hook_adapter.should_run_diff(bad_path, tmp_path) is False


def test_a_sibling_file_with_a_similar_name_is_not_a_match(tmp_path):
    (tmp_path / "config").mkdir()
    decoy = tmp_path / "config" / "candidate_profile.yaml.bak"
    decoy.write_text("x: 1\n")
    assert hook_adapter.should_run_diff(str(decoy), tmp_path) is False


# --- run_diff ----------------------------------------------------------------------------------


def _log_text(repo_root):
    log_path = repo_root / hook_adapter.LOG_RELATIVE_PATH
    return log_path.read_text() if log_path.exists() else ""


def test_run_diff_returns_false_and_logs_when_uv_is_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: None)
    called = False

    def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(hook_adapter.subprocess, "run", _fail_if_called)

    assert hook_adapter.run_diff(tmp_path) is False
    assert called is False
    assert "uv not found" in _log_text(tmp_path)


def test_run_diff_returns_true_on_a_clean_zero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: "/usr/bin/uv")

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == "/usr/bin/uv"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hook_adapter.subprocess, "run", _fake_run)

    assert hook_adapter.run_diff(tmp_path) is True
    assert _log_text(tmp_path) == ""


def test_run_diff_logs_a_non_zero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: "/usr/bin/uv")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom\n")

    monkeypatch.setattr(hook_adapter.subprocess, "run", _fake_run)

    assert hook_adapter.run_diff(tmp_path) is False
    log_text = _log_text(tmp_path)
    assert "exited 1" in log_text
    assert "boom" in log_text


def test_run_diff_logs_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: "/usr/bin/uv")

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 55))

    monkeypatch.setattr(hook_adapter.subprocess, "run", _fake_run)

    assert hook_adapter.run_diff(tmp_path) is False
    assert "timed out" in _log_text(tmp_path)


def test_run_diff_logs_an_oserror_starting_the_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: "/usr/bin/uv")

    def _fake_run(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(hook_adapter.subprocess, "run", _fake_run)

    assert hook_adapter.run_diff(tmp_path) is False
    assert "failed to start" in _log_text(tmp_path)


def test_run_diff_creates_the_logs_directory_on_first_write(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: None)
    assert not (tmp_path / "logs").exists()
    hook_adapter.run_diff(tmp_path)
    assert (tmp_path / "logs" / "profile-hook.log").exists()


# --- debounce / serialization (docs/agent-runtime-audit.md's "no debounce/serialization" finding)


def _fake_run_counting_calls(monkeypatch, calls: list):
    monkeypatch.setattr(hook_adapter.shutil, "which", lambda _name: "/usr/bin/uv")

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hook_adapter.subprocess, "run", _fake_run)


def test_second_run_within_the_debounce_window_is_skipped(tmp_path, monkeypatch):
    calls: list = []
    _fake_run_counting_calls(monkeypatch, calls)

    assert hook_adapter.run_diff(tmp_path) is True
    assert hook_adapter.run_diff(tmp_path) is False

    assert len(calls) == 1
    assert "debounce" in _log_text(tmp_path)


def test_run_after_the_debounce_window_elapses_is_not_skipped(tmp_path, monkeypatch):
    calls: list = []
    _fake_run_counting_calls(monkeypatch, calls)

    assert hook_adapter.run_diff(tmp_path) is True
    # Backdate the debounce marker instead of a real sleep -- exercises the same comparison
    # _recently_run makes without slowing the test suite down by DEBOUNCE_SECONDS.
    marker = tmp_path / hook_adapter.DEBOUNCE_MARKER_RELATIVE_PATH
    marker.write_text(str(time.time() - hook_adapter.DEBOUNCE_SECONDS - 1))

    assert hook_adapter.run_diff(tmp_path) is True
    assert len(calls) == 2


def test_run_skipped_while_another_hook_run_holds_the_lock(tmp_path, monkeypatch):
    """Simulates a genuinely concurrent second hook invocation (e.g. PostToolUse and FileChanged
    both firing for the same edit) rather than a rapid-succession one -- the lock, not the
    debounce window, is what must catch this, since both events fire at effectively the same
    instant."""
    calls: list = []
    _fake_run_counting_calls(monkeypatch, calls)

    lock_dir = tmp_path / hook_adapter.HOOK_LOCK_DIR_RELATIVE_PATH
    with run_lock(hook_adapter.HOOK_LOCK_NAME, lock_dir=lock_dir):
        assert hook_adapter.run_diff(tmp_path) is False

    assert len(calls) == 0
    assert "already in progress" in _log_text(tmp_path)

    # Once the concurrent run releases the lock, a fresh call must go through normally.
    assert hook_adapter.run_diff(tmp_path) is True
    assert len(calls) == 1
