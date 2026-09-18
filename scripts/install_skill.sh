#!/bin/sh
set -eu

SKILL_NAMES="job-hunter job-scout job-reviewer job-radar job-feedback onboard-source"

usage() {
  cat >&2 <<EOF
usage: install_skill.sh [--copy|--link] [--update] [--uninstall] [--dry-run]
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
  --dry-run         print what each target would do (OK/LINK/COPY/UPDATE/STALE/UNINSTALL/...)
                    without touching the filesystem at all; combine with any of the above
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
dry_run=0

while [ $# -gt 0 ]; do
  case "$1" in
    --copy) mode=copy ;;
    --link) mode=link ;;
    --update) want_update=1 ;;
    --uninstall) want_uninstall=1 ;;
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

# install_one is the one place every target/skill combination (and the Hermes hook's own
# symlinked entry point) routes through, so stale-link detection and every action flag
# (--update/--uninstall/--dry-run) only need to be handled here, not once per call site.
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

  if [ "$want_uninstall" -eq 1 ]; then
    if [ "$state" = absent ]; then
      echo "SKIP $destination (not installed)"
      return
    fi
    if [ "$dry_run" -eq 1 ]; then
      echo "UNINSTALL $destination [dry-run]"
      return
    fi
    rm -rf "$destination"
    echo "REMOVED $destination"
    return
  fi

  if [ "$up_to_date" -eq 1 ]; then
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
