"""Regression tests for the `--project` argument-order bug documented in
docs/agent-runtime-audit.md: `job-hunter --project X <command>` used to silently lose `X` because
the subparser re-registered `--project` with its own `default=None`, which argparse re-applies
after the root parser already set it. Fixed via `add_project_argument(..., suppress_default=True)`
on every subparser (see `rootutil.py`)."""

from pathlib import Path

import pytest

from job_hunter.cli import parser
from job_hunter.rootutil import resolve_project_root


def test_project_before_subcommand():
    args = parser().parse_args(["--project", "/tmp/x", "doctor"])
    assert args.project == Path("/tmp/x")


def test_project_after_subcommand():
    args = parser().parse_args(["doctor", "--project", "/tmp/x"])
    assert args.project == Path("/tmp/x")


def test_project_omitted_defaults_to_none():
    args = parser().parse_args(["doctor"])
    assert args.project is None


def test_project_before_subcommand_with_a_different_subcommand():
    # Guards against the fix only working for `doctor` specifically.
    args = parser().parse_args(["--project", "/tmp/x", "db-stats"])
    assert args.project == Path("/tmp/x")


def test_env_var_used_when_no_explicit_project(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("")
    assert resolve_project_root(None) == tmp_path.resolve()


def test_explicit_project_wins_over_env_var(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "pyproject.toml").write_text("")
    (explicit / "config").mkdir()
    (explicit / "config" / "settings.yaml").write_text("")

    env_only = tmp_path / "env-only"
    env_only.mkdir()
    (env_only / "pyproject.toml").write_text("")
    (env_only / "config").mkdir()
    (env_only / "config" / "settings.yaml").write_text("")

    monkeypatch.setenv("JOB_HUNTER_ROOT", str(env_only))
    assert resolve_project_root(str(explicit)) == explicit.resolve()


def test_resolve_project_root_rejects_non_checkout_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not look like a job-hunter checkout"):
        resolve_project_root(str(tmp_path))


def test_resolve_project_root_rejects_missing_directory(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError, match="does not exist or is not a directory"):
        resolve_project_root(str(missing))
