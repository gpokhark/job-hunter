#!/bin/sh
set -eu

SKILL_NAMES="job-hunter job-scout job-reviewer job-radar"

usage() {
  cat >&2 <<EOF
usage: install_skill.sh [--copy] [--hermes] [--claude-global] [--claude-local] [--opencode] [--all]

Installs all four job-hunter skills ($SKILL_NAMES) — the job-hunter orchestrator plus the
independently-invocable job-scout/job-reviewer/job-radar stages (see docs/skill-split-plan.md).

With no target flags, prompts interactively for which runtime(s) to install into.
Pass one or more target flags to install non-interactively (e.g. for scripting).

  --hermes         ~/.hermes/skills/<name>
  --claude-global   ~/.claude/skills/<name>
  --claude-local    <this repo>/.claude/skills/<name>
  --opencode        ~/.config/opencode/skills/<name>
  --all             all four of the above
  --copy            copy each skill directory instead of symlinking it
EOF
}

mode=link
want_hermes=0
want_claude_global=0
want_claude_local=0
want_opencode=0
any_target=0

while [ $# -gt 0 ]; do
  case "$1" in
    --copy) mode=copy ;;
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

install_one() {
  destination=$1
  link_source=$2
  abs_source=$3
  mkdir -p "$(dirname -- "$destination")"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "SKIP $destination (already exists)"
    return
  fi
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
  [ "$want_hermes" -eq 1 ] && install_one "$HOME/.hermes/skills/$name" "$abs_source" "$abs_source"
  [ "$want_claude_global" -eq 1 ] && install_one "$HOME/.claude/skills/$name" "$abs_source" "$abs_source"
  [ "$want_claude_local" -eq 1 ] && install_one "$repo_root/.claude/skills/$name" "../../skills/$name" "$abs_source"
  [ "$want_opencode" -eq 1 ] && install_one "$HOME/.config/opencode/skills/$name" "$abs_source" "$abs_source"
done

exit 0
