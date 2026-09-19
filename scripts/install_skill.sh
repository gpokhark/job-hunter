#!/bin/sh
set -eu

SKILL_NAMES="job-hunter job-scout job-reviewer job-radar job-feedback onboard-source"

usage() {
  cat >&2 <<EOF
usage: install_skill.sh [--copy|--link] [--update] [--uninstall] [--force] [--dry-run]
                         [--hermes] [--claude-global] [--claude-local] [--opencode] [--all]

Installs all six job-hunter skills ($SKILL_NAMES) — the job-hunter orchestrator, the
independently-invocable job-scout/job-reviewer/job-radar stages (see docs/skill-split-plan.md),
job-feedback for turning radar feedback/profile edits into a confirmed profile update, and
onboard-source (a repo-maintenance skill for extending job-hunter itself, installed for every
target exactly like the other five, Hermes included).

With no target flags, prompts interactively for which runtime(s) to install into.
Pass one or more target flags to install non-interactively (e.g. for scripting).

  --hermes         ~/.hermes/skills/<name> plus the candidate-profile diff hook
  --claude-global   ~/.claude/skills/<name>
  --claude-local    <this repo>/.claude/skills/<name>
  --opencode        ~/.config/opencode/skills/<name>
  --all             all four of the above

  --copy            copy each skill directory instead of symlinking it
  --link            symlink each skill directory (the default when --copy is not given;
                    named explicitly so both modes are real, documented flags)
  --update          replace an existing install that no longer matches the current source
                    (a stale symlink target, e.g. after the repo or a skill moved, or a
                    --copy install whose content has since diverged) instead of leaving it
                    alone; an install that's already correct is reported as up to date either way
  --uninstall       remove a previously-installed skill/hook for the selected target(s)
                    instead of installing — for --hermes, also unregisters the profile-diff
                    hook entry install_hermes_hook.py added to config.yaml
  --force           --uninstall/--update normally refuse to touch a destination this tool has
                    no record of installing (see the ownership marker note below); --force
                    removes/replaces it anyway. Has no effect on a destination this tool did
                    install — that path is unaffected either way.
  --dry-run         print what each target would do (OK/LINK/COPY/UPDATE/STALE/UNINSTALL/...)
                    without touching the filesystem at all; combine with any of the above

Each install/update writes the installed basename to a plain-text ownership marker,
<destination's parent dir>/.job-hunter-installed — one name per line. --uninstall/--update
consult it (falling back to "does this destination still look like something we'd install" for
an install made before this marker existed, so an already-live pre-marker install keeps working
without needing --force) before ever removing an existing destination, and refuse a destination
with neither signal rather than silently deleting something this tool didn't create.
EOF
}

mode=link
want_hermes=0
want_claude_global=0
want_claude_local=0
want_opencode=0
any_target=0
want_update=0
want_uninstall=0
want_force=0
dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    --copy) mode=copy ;;
    --link) mode=link ;;
    --update) want_update=1 ;;
    --uninstall) want_uninstall=1 ;;
    --force) want_force=1 ;;
    --dry-run) dry_run=1 ;;
    --hermes) want_hermes=1; any_target=1 ;;
    --claude-global) want_claude_global=1; any_target=1 ;;
    --claude-local) want_claude_local=1; any_target=1 ;;
    --opencode) want_opencode=1; any_target=1 ;;
    --all)
      want_hermes=1
      want_claude_global=1
      want_claude_local=1
      want_opencode=1
      any_target=1
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

if [ "$any_target" -eq 0 ]; then
  if [ ! -t 0 ]; then
    echo "no target flags given and not running interactively" >&2
    usage
    exit 2
  fi
  echo "Install the job-hunter skills ($SKILL_NAMES) for which runtime(s)?"
  echo "  1) Hermes             (~/.hermes/skills/<name>)"
  echo "  2) Claude - global    (~/.claude/skills/<name>, all projects)"
  echo "  3) Claude - local     (this repo only, ./.claude/skills/<name>)"
  echo "  4) OpenCode           (~/.config/opencode/skills/<name>)"
  printf 'Enter one or more numbers separated by spaces (or "all"): '
  read -r selection
  for choice in $selection; do
    case "$choice" in
      1) want_hermes=1 ;;
      2) want_claude_global=1 ;;
      3) want_claude_local=1 ;;
      4) want_opencode=1 ;;
      all) want_hermes=1; want_claude_global=1; want_claude_local=1; want_opencode=1 ;;
      *) echo "ignoring unrecognized choice: $choice" >&2 ;;
    esac
  done
  if [ "$want_hermes$want_claude_global$want_claude_local$want_opencode" = "0000" ]; then
    echo "nothing selected; exiting" >&2
    exit 1
  fi
fi

# manifest_path: the ownership-marker file for $1's parent directory — one installed basename per
# line, plain text (not JSON/YAML — this script has no parser dependency beyond grep/mv). One
# marker file covers every skill/hook this tool has ever installed under that same parent dir.
manifest_path() {
  printf '%s/.job-hunter-installed' "$(dirname -- "$1")"
}

# manifest_has: true if $1's basename is recorded as installed by this tool.
manifest_has() {
  mf=$(manifest_path "$1")
  [ -f "$mf" ] && grep -qxF "$(basename -- "$1")" "$mf" 2>/dev/null
}

# manifest_add: idempotently record $1's basename as installed. Called after every real (non-dry-
# run) install/update, so a destination this tool creates is always provably owned from then on.
manifest_add() {
  mf=$(manifest_path "$1")
  mkdir -p "$(dirname -- "$mf")"
  if ! manifest_has "$1"; then
    basename -- "$1" >>"$mf"
  fi
}

# manifest_remove: the inverse, called after a real uninstall. A no-op if there's no marker file
# yet (e.g. an ownership-by-heuristic destination that was never actually recorded).
manifest_remove() {
  mf=$(manifest_path "$1")
  [ -f "$mf" ] || return 0
  tmp="$mf.tmp.$$"
  grep -vxF "$(basename -- "$1")" "$mf" >"$tmp" 2>/dev/null || true
  mv "$tmp" "$mf"
}

# install_one is the one place every target/skill combination (and the Hermes hook's own
# symlinked entry point) routes through, so stale-link detection and every action flag
# (--update/--uninstall/--force/--dry-run) only need to be handled here, not once per call site.
#
# destination: where the skill (or hook script) should live for this target.
# link_source: the value to pass to `ln -s` — relative for --claude-local (matching the
#   repo's existing ../../skills/<name> convention so a moved *clone* of the whole repo still
#   resolves correctly), absolute for every other target.
# abs_source: the real absolute path being installed from, used for --copy and for comparing
#   an existing copy's content against the current source.
install_one() {
  destination=$1
  link_source=$2
  abs_source=$3

  state=absent
  current_target=""
  if [ -L "$destination" ]; then
    state=symlink
    current_target=$(readlink "$destination")
  elif [ -e "$destination" ]; then
    state=present
  fi

  up_to_date=0
  if [ "$mode" = link ] && [ "$state" = symlink ] && [ "$current_target" = "$link_source" ]; then
    up_to_date=1
  elif [ "$mode" = copy ] && [ "$state" = present ] && diff -rq "$abs_source" "$destination" >/dev/null 2>&1; then
    up_to_date=1
  fi

  # owned: may this tool remove/replace whatever is currently at $destination? True when a
  # marker records it (the durable signal every real install/update writes going forward), or,
  # for a destination installed before the marker existed, when it still looks like something
  # this tool put there — either exactly up to date, or a stale symlink whose target still ends
  # in this same basename (a moved-repo/moved-skill scenario, not a foreign symlink) — so an
  # already-live pre-marker install (e.g. this repo's own real .claude/skills/onboard-source)
  # keeps working under --update/--uninstall without needing --force. Anything else (a real
  # directory/file/symlink with neither signal) is not owned.
  owned=0
  if manifest_has "$destination"; then
    owned=1
  elif [ "$up_to_date" -eq 1 ]; then
    owned=1
  elif [ "$state" = symlink ] && [ "$(basename -- "$current_target")" = "$(basename -- "$destination")" ]; then
    owned=1
  fi

  if [ "$want_uninstall" -eq 1 ]; then
    if [ "$state" = absent ]; then
      echo "SKIP $destination (not installed)"
      return
    fi
    if [ "$owned" -ne 1 ] && [ "$want_force" -ne 1 ]; then
      if [ "$dry_run" -eq 1 ]; then
        echo "REFUSED $destination (not installed by this tool; re-run with --force to remove anyway) [dry-run]"
      else
        echo "REFUSED $destination (not installed by this tool; re-run with --force to remove anyway)"
      fi
      return
    fi
    if [ "$dry_run" -eq 1 ]; then
      echo "UNINSTALL $destination [dry-run]"
      return
    fi
    rm -rf "$destination"
    manifest_remove "$destination"
    echo "REMOVED $destination"
    return
  fi

  if [ "$up_to_date" -eq 1 ]; then
    # Backfill the marker for a pre-marker legacy install that's already correct -- but never
    # under --dry-run, which must never touch the filesystem (confirmed live: an earlier version
    # of this branch wrote the marker unconditionally here, and a plain --dry-run --update run
    # against this repo's own real .claude/skills/ actually created it).
    [ "$dry_run" -eq 1 ] || manifest_add "$destination"
    echo "OK $destination (already up to date)"
    return
  fi

  if [ "$state" != absent ]; then
    if [ "$want_update" -ne 1 ]; then
      if [ "$state" = symlink ]; then
        echo "STALE $destination -> $current_target (does not match $link_source; re-run with --update to fix)"
      else
        echo "SKIP $destination (already exists, differs from source; re-run with --update to refresh)"
      fi
      return
    fi
    if [ "$owned" -ne 1 ] && [ "$want_force" -ne 1 ]; then
      if [ "$dry_run" -eq 1 ]; then
        echo "REFUSED $destination (not installed by this tool; re-run with --force to replace anyway) [dry-run]"
      else
        echo "REFUSED $destination (not installed by this tool; re-run with --force to replace anyway)"
      fi
      return
    fi
    if [ "$dry_run" -eq 1 ]; then
      echo "UPDATE $destination [dry-run]"
      return
    fi
    rm -rf "$destination"
  elif [ "$dry_run" -eq 1 ]; then
    if [ "$mode" = copy ]; then
      echo "COPY $abs_source -> $destination [dry-run]"
    else
      echo "LINK $link_source -> $destination [dry-run]"
    fi
    return
  fi

  mkdir -p "$(dirname -- "$destination")"
  if [ "$mode" = copy ]; then
    cp -R "$abs_source" "$destination"
    echo "COPY $abs_source -> $destination"
  else
    ln -s "$link_source" "$destination"
    echo "LINK $link_source -> $destination"
  fi
  manifest_add "$destination"
}

for name in $SKILL_NAMES; do
  abs_source=$(CDPATH= cd -- "$script_dir/../skills/$name" && pwd)
  [ "$want_hermes" -eq 1 ] && install_one "${HERMES_HOME:-$HOME/.hermes}/skills/$name" "$abs_source" "$abs_source"
  [ "$want_claude_global" -eq 1 ] && install_one "$HOME/.claude/skills/$name" "$abs_source" "$abs_source"
  [ "$want_claude_local" -eq 1 ] && install_one "$repo_root/.claude/skills/$name" "../../skills/$name" "$abs_source"
  [ "$want_opencode" -eq 1 ] && install_one "$HOME/.config/opencode/skills/$name" "$abs_source" "$abs_source"
done

if [ "$want_hermes" -eq 1 ]; then
  hermes_home=${HERMES_HOME:-"$HOME/.hermes"}
  hook_source="$repo_root/scripts/hermes_profile_hook.py"
  install_one "$hermes_home/agent-hooks/job-hunter-profile.py" "$hook_source" "$hook_source"
  if [ "$want_uninstall" -eq 1 ]; then
    if [ "$dry_run" -eq 1 ]; then
      echo "UNINSTALL $hermes_home/config.yaml (profile hook registration) [dry-run]"
    else
      uv run --project "$repo_root" python "$script_dir/install_hermes_hook.py" --uninstall "$hermes_home" "$repo_root"
    fi
  elif [ "$dry_run" -eq 1 ]; then
    echo "REGISTER $hermes_home/config.yaml (profile hook registration) [dry-run]"
  else
    uv run --project "$repo_root" python "$script_dir/install_hermes_hook.py" "$hermes_home" "$repo_root"
  fi
fi

exit 0
