"""Merge the profile shell hook into Hermes config, preserving unrelated settings."""

import shlex
import shutil
import sys
from pathlib import Path

import yaml


def install(hermes_home: Path, repo_root: Path) -> None:
    config_path = hermes_home / "config.yaml"
    original = config_path.read_text() if config_path.exists() else ""
    config = yaml.safe_load(original)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("Hermes config must be a YAML mapping")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Hermes hooks must be a YAML mapping")
    entries = hooks.setdefault("post_tool_call", [])
    if not isinstance(entries, list):
        raise ValueError("Hermes hooks.post_tool_call must be a list")
    command = shlex.join([
        "python3", str(hermes_home / "agent-hooks/job-hunter-profile.py"), str(repo_root),
    ])
    entry = {"matcher": "^(write_file|patch)$", "command": command, "timeout": 60}
    if entry in entries:
        print(f"SKIP {config_path} (profile hook already registered)")
        return
    entries.append(entry)
    # Keep the original bytes (including comments) before YAML serialization.
    if config_path.exists():
        backup = config_path.with_name("config.yaml.job-hunter.bak")
        if not backup.exists():
            shutil.copy2(config_path, backup)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"REGISTER profile hook in {config_path}")


if __name__ == "__main__":
    install(Path(sys.argv[1]).expanduser().resolve(), Path(sys.argv[2]).resolve())
