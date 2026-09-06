import importlib.util
from pathlib import Path
from unittest.mock import patch

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_scope_and_working_directory(tmp_path):
    hook = load("hermes_profile_hook")
    payload = {
        "hook_event_name": "post_tool_call", "tool_name": "patch",
        "tool_input": {"path": "config/candidate_profile.yaml"}, "cwd": str(tmp_path),
    }
    with patch.object(hook.subprocess, "run") as run:
        hook.run(payload, tmp_path)
        assert run.call_args.kwargs["cwd"] == tmp_path
        run.reset_mock()
        hook.run(payload, tmp_path / "other")
        payload["extra"] = {"status": "error"}
        hook.run(payload, tmp_path)
        payload["tool_name"] = "read_file"
        hook.run(payload, tmp_path)
        run.assert_not_called()


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
