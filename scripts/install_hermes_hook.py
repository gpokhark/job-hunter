"""Merge the profile shell hook into Hermes config, preserving unrelated settings."""

import shlex
import shutil
import sys
from pathlib import Path

import yaml


def _hook_script_path(hermes_home: Path) -> Path:
    """The one, stable, repo-independent path Hermes always invokes for this hook —
    `install_one` (`scripts/install_skill.sh`) keeps this pointed (symlink or copy) at whichever
    checkout's `scripts/hermes_profile_hook.py` last ran `--hermes`, so this path itself never
    changes across a repo move/re-clone even though the `repo_root` argument passed alongside it
    does."""
    return hermes_home / "agent-hooks" / "job-hunter-profile.py"


def _is_job_hunter_hook_entry(entry: object, hermes_home: Path) -> bool:
    """True if `entry` registers *this* hook, regardless of which `repo_root` it was registered
    with — matching on the stable hook-script path (`_hook_script_path`) rather than the exact
    full command string is what makes `install()`/`uninstall()` relocation-safe
    (docs/agent-runtime-audit.md's "Hermes registration is not relocatable" finding): before this,
    a registration's identity was its *entire* command string including the embedded absolute
    `repo_root`, so re-running `install_skill.sh --hermes` after moving the checkout appended a
    second, different-path entry instead of replacing the stale one, and `--uninstall` run from
    the new location couldn't find the old entry to remove at all."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return str(_hook_script_path(hermes_home)) in args


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
    command = shlex.join(["python3", str(_hook_script_path(hermes_home)), str(repo_root)])
    entry = {"matcher": "^(write_file|patch)$", "command": command, "timeout": 60}
    if entry in entries:
        print(f"SKIP {config_path} (profile hook already registered)")
        return
    # Replace any entry for this same hook script, however it was registered before (a different
    # repo_root from an earlier checkout location, a stale/hand-edited command) — never
    # accumulate a second registration alongside it.
    stale_count = sum(1 for e in entries if _is_job_hunter_hook_entry(e, hermes_home))
    hooks["post_tool_call"] = [e for e in entries if not _is_job_hunter_hook_entry(e, hermes_home)]
    hooks["post_tool_call"].append(entry)
    # Keep the original bytes (including comments) before YAML serialization.
    if config_path.exists():
        backup = config_path.with_name("config.yaml.job-hunter.bak")
        if not backup.exists():
            shutil.copy2(config_path, backup)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    if stale_count:
        print(
            f"REGISTER profile hook in {config_path} "
            f"(replaced {stale_count} stale registration(s) pointing at a different checkout)"
        )
    else:
        print(f"REGISTER profile hook in {config_path}")


def uninstall(hermes_home: Path, repo_root: Path) -> None:
    """The inverse of `install()` — removes every entry that registers this hook script
    (`_is_job_hunter_hook_entry`, the same relocation-safe identity `install()` itself uses to
    decide replace-vs-append), regardless of which `repo_root` each one embeds, and leaves every
    other hook/setting in `config.yaml` untouched. `repo_root` is accepted but no longer consulted
    — kept as a parameter only so callers (`install_skill.sh --uninstall --hermes`, this module's
    own CLI entry point) don't need special-casing between install/uninstall's argument shapes.
    Used by `install_skill.sh --uninstall --hermes`. Never errors on "nothing to remove" — a
    config that doesn't exist, has no `hooks.post_tool_call` list, or simply doesn't contain this
    entry are all reported as a plain SKIP, the same tolerant shape `install()`'s own SKIP path
    already has."""
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
    remaining = [e for e in entries if not _is_job_hunter_hook_entry(e, hermes_home)]
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
