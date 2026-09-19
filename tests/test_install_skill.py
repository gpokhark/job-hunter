"""Tests for `scripts/install_skill.sh`'s `--dry-run`/`--update`/`--uninstall`/`--link` flags and
its stale-symlink detection (docs/skill-frontmatter-and-hook-plan.md sections 4.5/5). Runs the
real script as a subprocess against a fixture directory tree — never the real `~/.claude` or
`~/.hermes` — using the same `HOME`/`HERMES_HOME` overrides the installer already supports for
exactly this purpose. `--claude-local` is deliberately never exercised here: it always targets
*this* checkout's own `.claude/skills` (there's no env-var override for it), and this repo's real
onboard-source symlink lives there for real Claude Code use — a test that uninstalled or rewrote
it would corrupt the actual working tree, not a fixture."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_skill.sh"
SIX_SKILLS = {"job-hunter", "job-scout", "job-reviewer", "job-radar", "job-feedback", "onboard-source"}


def _run(args, *, home, hermes_home=None, timeout=60):
    env = dict(os.environ)
    env["HOME"] = str(home)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    else:
        env.pop("HERMES_HOME", None)
    return subprocess.run(
        ["sh", str(INSTALLER), *args], env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_dry_run_creates_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(["--dry-run", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert not (home / ".claude" / "skills").exists()
    for name in SIX_SKILLS:
        assert f"-> {home}/.claude/skills/{name} [dry-run]" in proc.stdout


def test_dry_run_installs_all_six_skills_including_onboard_source(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(["--dry-run", "--claude-global"], home=home)
    for name in SIX_SKILLS:
        assert name in proc.stdout


def test_link_is_the_default_and_link_flag_is_an_explicit_synonym(tmp_path):
    home_default = tmp_path / "home_default"
    home_default.mkdir()
    home_explicit = tmp_path / "home_explicit"
    home_explicit.mkdir()

    proc_default = _run(["--claude-global"], home=home_default)
    proc_explicit = _run(["--link", "--claude-global"], home=home_explicit)
    assert proc_default.returncode == 0
    assert proc_explicit.returncode == 0
    for name in SIX_SKILLS:
        dest_default = home_default / ".claude" / "skills" / name
        dest_explicit = home_explicit / ".claude" / "skills" / name
        assert dest_default.is_symlink()
        assert dest_explicit.is_symlink()
        assert os.readlink(dest_default) == os.readlink(dest_explicit)


def test_rerun_reports_ok_not_a_reinstall(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = _run(["--claude-global"], home=home)
    assert first.returncode == 0
    second = _run(["--claude-global"], home=home)
    assert second.returncode == 0
    for name in SIX_SKILLS:
        assert f"OK {home}/.claude/skills/{name} (already up to date)" in second.stdout
    assert "LINK" not in second.stdout


def test_stale_symlink_is_reported_and_left_alone_without_update(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    dest = home / ".claude" / "skills" / "job-hunter"
    stale_target = tmp_path / "moved-repo" / "skills" / "job-hunter"  # never created — simulates
    # a repo (or, per this same change, a skill) that moved out from under an existing install.
    dest.unlink()
    dest.symlink_to(stale_target)

    proc = _run(["--claude-global"], home=home)
    assert proc.returncode == 0
    assert f"STALE {dest} -> {stale_target}" in proc.stdout
    assert "re-run with --update to fix" in proc.stdout
    # Left untouched — still pointing at the stale target, not silently replaced or removed.
    assert os.readlink(dest) == str(stale_target)


def test_update_replaces_a_stale_symlink_with_the_current_source(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    dest = home / ".claude" / "skills" / "job-hunter"
    stale_target = tmp_path / "moved-repo" / "skills" / "job-hunter"
    dest.unlink()
    dest.symlink_to(stale_target)

    proc = _run(["--update", "--claude-global"], home=home)
    assert proc.returncode == 0
    correct_source = str(REPO_ROOT / "skills" / "job-hunter")
    assert os.readlink(dest) == correct_source
    assert f"LINK {correct_source} -> {dest}" in proc.stdout

    # A plain re-run now reports it as already up to date, not stale.
    rerun = _run(["--claude-global"], home=home)
    assert f"OK {dest} (already up to date)" in rerun.stdout
    assert "STALE" not in rerun.stdout


def test_update_dry_run_reports_without_touching_the_stale_link(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    dest = home / ".claude" / "skills" / "job-hunter"
    stale_target = tmp_path / "moved-repo" / "skills" / "job-hunter"
    dest.unlink()
    dest.symlink_to(stale_target)

    proc = _run(["--update", "--dry-run", "--claude-global"], home=home)
    assert f"UPDATE {dest} [dry-run]" in proc.stdout
    assert os.readlink(dest) == str(stale_target)


def test_uninstall_removes_installed_skills(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    proc = _run(["--uninstall", "--claude-global"], home=home)
    assert proc.returncode == 0
    for name in SIX_SKILLS:
        dest = home / ".claude" / "skills" / name
        assert not dest.exists() and not dest.is_symlink()
        assert f"REMOVED {dest}" in proc.stdout

    # Re-running uninstall on an already-clean target is a safe no-op, not an error.
    again = _run(["--uninstall", "--claude-global"], home=home)
    assert again.returncode == 0
    assert "(not installed)" in again.stdout


def test_uninstall_dry_run_touches_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    proc = _run(["--uninstall", "--dry-run", "--claude-global"], home=home)
    assert proc.returncode == 0
    for name in SIX_SKILLS:
        dest = home / ".claude" / "skills" / name
        assert dest.is_symlink()
        assert f"UNINSTALL {dest} [dry-run]" in proc.stdout


def test_copy_mode_reports_stale_when_source_changes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(["--copy", "--claude-global"], home=home)
    assert proc.returncode == 0
    dest = home / ".claude" / "skills" / "job-hunter"
    assert dest.is_dir() and not dest.is_symlink()

    # Simulate content drift (as if the source skill had changed since this copy was made).
    (dest / "SKILL.md").write_text((dest / "SKILL.md").read_text() + "\n<!-- drifted -->\n")

    rerun = _run(["--copy", "--claude-global"], home=home)
    assert f"SKIP {dest} (already exists, differs from source; re-run with --update to refresh)" in rerun.stdout

    updated = _run(["--copy", "--update", "--claude-global"], home=home)
    assert f"COPY {REPO_ROOT / 'skills' / 'job-hunter'} -> {dest}" in updated.stdout
    assert "<!-- drifted -->" not in (dest / "SKILL.md").read_text()


def test_manifest_is_written_on_fresh_install(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(["--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    manifest = home / ".claude" / "skills" / ".job-hunter-installed"
    assert manifest.exists()
    lines = manifest.read_text().splitlines()
    assert set(lines) == SIX_SKILLS


def test_uninstall_refuses_a_destination_this_tool_never_installed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dest = home / ".claude" / "skills" / "job-hunter"
    dest.mkdir(parents=True)
    (dest / "notes.txt").write_text("someone's own content, not ours")

    proc = _run(["--uninstall", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert f"REFUSED {dest} (not installed by this tool; re-run with --force to remove anyway)" in proc.stdout
    assert dest.is_dir()
    assert (dest / "notes.txt").exists()


def test_uninstall_force_removes_an_unowned_destination_anyway(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dest = home / ".claude" / "skills" / "job-hunter"
    dest.mkdir(parents=True)
    (dest / "notes.txt").write_text("someone's own content, not ours")

    proc = _run(["--uninstall", "--force", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert f"REMOVED {dest}" in proc.stdout
    assert not dest.exists()


def test_update_refuses_an_unowned_destination(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dest = home / ".claude" / "skills" / "job-hunter"
    dest.mkdir(parents=True)
    (dest / "notes.txt").write_text("someone's own content, not ours")

    proc = _run(["--update", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert f"REFUSED {dest} (not installed by this tool; re-run with --force to replace anyway)" in proc.stdout
    assert (dest / "notes.txt").exists()


def test_uninstall_removes_manifest_entry_too(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    manifest = home / ".claude" / "skills" / ".job-hunter-installed"
    assert manifest.exists()

    proc = _run(["--uninstall", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    # All six skills were the manifest's only entries -- removing all of them leaves it empty,
    # not stale with names for skills that no longer exist at their destinations.
    assert manifest.read_text().strip() == ""


def test_pre_existing_symlink_install_without_a_manifest_is_still_updatable_and_uninstallable(tmp_path):
    """The key backward-compatibility case: an install made by a version of this script before
    the ownership marker existed (simulated here by hand-symlinking, deliberately bypassing the
    part of the script that would normally write the marker) must not suddenly require --force."""
    home = tmp_path / "home"
    home.mkdir()
    dest_dir = home / ".claude" / "skills"
    dest_dir.mkdir(parents=True)
    correct_source = REPO_ROOT / "skills" / "job-hunter"
    dest = dest_dir / "job-hunter"
    dest.symlink_to(correct_source)
    assert not (dest_dir / ".job-hunter-installed").exists()

    # A plain re-run recognizes it as already up to date (no marker needed) and backfills the
    # marker for next time.
    proc = _run(["--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert f"OK {dest} (already up to date)" in proc.stdout
    manifest = dest_dir / ".job-hunter-installed"
    assert manifest.exists()
    assert "job-hunter" in manifest.read_text().splitlines()

    # And --uninstall succeeds on it without --force.
    uninstall = _run(["--uninstall", "--claude-global"], home=home)
    assert f"REMOVED {dest}" in uninstall.stdout
    assert not dest.exists()


def test_dry_run_against_an_already_up_to_date_legacy_install_writes_nothing(tmp_path):
    """Regression test: an earlier version of the up-to-date branch called manifest_add()
    unconditionally, so a plain --dry-run re-run against a pre-marker install that's already
    correct (this repo's own real .claude/skills/* symlinks, concretely) silently created the
    marker file despite --dry-run promising to touch nothing. --dry-run must stay a true no-op
    even on the backfill path."""
    home = tmp_path / "home"
    home.mkdir()
    dest_dir = home / ".claude" / "skills"
    dest_dir.mkdir(parents=True)
    correct_source = REPO_ROOT / "skills" / "job-hunter"
    dest = dest_dir / "job-hunter"
    dest.symlink_to(correct_source)

    proc = _run(["--dry-run", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert f"OK {dest} (already up to date)" in proc.stdout
    assert not (dest_dir / ".job-hunter-installed").exists()


def test_pre_existing_stale_symlink_by_basename_is_still_owned_without_force(tmp_path):
    """A stale symlink (e.g. from a moved repo) with no manifest entry is still recognized as
    ours by basename shape, so --update can fix it without requiring --force."""
    home = tmp_path / "home"
    home.mkdir()
    _run(["--claude-global"], home=home)
    dest = home / ".claude" / "skills" / "job-hunter"
    manifest = home / ".claude" / "skills" / ".job-hunter-installed"
    manifest.unlink()  # simulate a pre-marker install: no record at all
    stale_target = tmp_path / "moved-repo" / "skills" / "job-hunter"
    dest.unlink()
    dest.symlink_to(stale_target)

    proc = _run(["--update", "--claude-global"], home=home)
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED" not in proc.stdout
    correct_source = str(REPO_ROOT / "skills" / "job-hunter")
    assert os.readlink(dest) == correct_source


def test_hermes_install_and_uninstall_round_trip(tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    proc = _run(["--hermes"], home=home, hermes_home=hermes_home, timeout=180)
    assert proc.returncode == 0, proc.stderr
    for name in SIX_SKILLS:
        assert (hermes_home / "skills" / name).is_symlink()
    assert (hermes_home / "agent-hooks" / "job-hunter-profile.py").is_symlink()
    config = hermes_home / "config.yaml"
    assert config.exists()
    assert "job-hunter-profile.py" in config.read_text()

    # The hook script symlink routes through the same install_one() as every skill, so it gets
    # the identical ownership-marker protection with no separate code path.
    skills_manifest = hermes_home / "skills" / ".job-hunter-installed"
    assert set(skills_manifest.read_text().splitlines()) == SIX_SKILLS
    hooks_manifest = hermes_home / "agent-hooks" / ".job-hunter-installed"
    assert hooks_manifest.read_text().splitlines() == ["job-hunter-profile.py"]

    uninstall = _run(["--uninstall", "--hermes"], home=home, hermes_home=hermes_home, timeout=180)
    assert uninstall.returncode == 0, uninstall.stderr
    for name in SIX_SKILLS:
        assert not (hermes_home / "skills" / name).exists()
    assert "job-hunter-profile.py" not in config.read_text()
