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


def uninstall(hermes_home: Path, repo_root: Path) -> None:
    """The inverse of `install()` — removes the exact entry it added, matched by its exact
    `command` string (the same identity `install()` itself uses to decide SKIP vs. append), and
    leaves every other hook/setting in `config.yaml` untouched. Used by `install_skill.sh
    --uninstall --hermes`. Never errors on "nothing to remove" — a config that doesn't exist, has
    no `hooks.post_tool_call` list, or simply doesn't contain this entry are all reported as a
    plain SKIP, the same tolerant shape `install()`'s own SKIP path already has."""
    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        print(f"SKIP {config_path} (no Hermes config found)")
        return
    config = yaml.safe_load(config_path.read_text())
    if not isinstance(config, dict):
        print(f"SKIP {config_path} (not a YAML mapping)")
        return
    hooks = config.get("hooks")
    entries = hooks.get("post_tool_call") if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        print(f"SKIP {config_path} (profile hook not registered)")
        return
    command = shlex.join([
        "python3", str(hermes_home / "agent-hooks/job-hunter-profile.py"), str(repo_root),
    ])
    remaining = [e for e in entries if not (isinstance(e, dict) and e.get("command") == command)]
    if len(remaining) == len(entries):
        print(f"SKIP {config_path} (profile hook not registered)")
        return
    backup = config_path.with_name("config.yaml.job-hunter.bak")
    if not backup.exists():
        shutil.copy2(config_path, backup)
    hooks["post_tool_call"] = remaining
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"UNREGISTER profile hook from {config_path}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--uninstall":
        uninstall(Path(argv[1]).expanduser().resolve(), Path(argv[2]).resolve())
    else:
        install(Path(argv[0]).expanduser().resolve(), Path(argv[1]).resolve())
