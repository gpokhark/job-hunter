#!/bin/sh
set -eu

# Portable launcher for the candidate-profile diff hook, invoked by .claude/settings.json's
# PostToolUse command as `run_profile_hook.sh "$CLAUDE_PROJECT_DIR"`.
#
# docs/agent-runtime-audit.md's "Claude hook coverage" finding: the previous direct
# `uv run python scripts/claude_profile_hook.py ...` command fails at the shell level if `uv`
# isn't resolvable on the *invoking process's* PATH -- before scripts/claude_profile_hook.py (or
# src/job_hunter/hook_adapter.py's own shutil.which("uv") check, which exists specifically to
# produce a clear diagnostic for the *second*, inner `uv run` call that runs diff_profile.py) ever
# gets a chance to run at all. This wrapper finds `uv` itself and falls back to a clear message
# instead of a bare shell "command not found" -- and, same as every hook in this pipeline, never
# fails loudly: a hook is advisory by design (see claude_profile_hook.py's own docstring), so this
# always exits 0 regardless of which path it took.

project_dir=${1:-.}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v uv >/dev/null 2>&1; then
  uv run python "$script_dir/claude_profile_hook.py" "$project_dir"
  exit 0
fi

if command -v python3 >/dev/null 2>&1; then
  echo "job-hunter profile hook: uv not found on PATH, falling back to bare python3 " \
       "(job_hunter must already be importable for this to actually run the diff)" >&2
  python3 "$script_dir/claude_profile_hook.py" "$project_dir" || true
  exit 0
fi

echo "job-hunter profile hook: neither uv nor python3 found on PATH -- skipping" >&2
exit 0
