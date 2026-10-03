# Operations notes

Behavior you only need when running Job Hunter long-term, on several machines, or inside an agent runtime. Basics live in [USAGE.md](USAGE.md).

## `--project`, timeouts and locking

Every command above (and every *operational* `scripts/*.py` entry point — not the diagnostic/
prototype/converter utilities or the hook/installer scripts, which take the project root
positionally via their own runtime's calling convention instead; see `docs/SPEC.md`'s `--project`
section for the exact list) also takes `--project <path>`, falling back to `$JOB_HUNTER_ROOT`,
then the current directory — run job-hunter from any directory, including from inside an agent
whose working directory isn't this checkout, without `cd`-ing in first (works whether the flag
comes before or after the subcommand).

`job-hunter pipeline` bounds each stage (search/review/refilter/radar) to
`pipeline.stage_timeout_seconds` in `config/settings.yaml` (8 hours by default, sized to comfortably
exceed a large sequential local-model review, not as a tight SLA) — a stage that hangs is killed
(its whole process tree, not just the immediate subprocess) and the run finalizes as `timed_out`
rather than hanging indefinitely; set it to `null`/comment it out for no timeout. Two overlapping
pipeline runs (or a `pipeline` run and a `cleanup --apply`/standalone review/refilter) never race
each other's writes to SQLite/the archive/`assessments.json` — the second one gets a clear
`lock_held` status naming the first run's PID and command instead of corrupting shared state.

## Retention and cleanup

Nothing is ever deleted automatically — `data/jobs.sqlite3` and `data/profile-diff/`/`data/radar/`
only ever grow. `job-hunter cleanup` (dry-run by default, `--apply` to commit, writing an export of
exactly what it's about to remove first) deletes jobs closed longer than
`retention.closed_job_after_days` and old generated reports past `retention.report_after_days` —
keeping the latest `retention.keep_latest_reports_per_slug` of each regardless of age. All three
are configurable in `config/settings.yaml`; see `docs/SPEC.md` §8.6 and
`docs/retention-cleanup-plan.md` for the full design.

## Archive resolution

"Newest archive" means the run date in the filename (mtime only breaks a same-date tie), so a
refilter re-stamping its target can't make a small archive look newer than a large one. Two
same-date archives with different keywords still fall back to mtime. To skip resolution, pass
`--search <path>`; to sanity-check what a resolution will pick, `job-hunter resolve-search
[--keyword "..."]` prints the path on stdout and a scope summary on stderr (`scope: N sources
attempted (...)`) — a "default" archive that only queried one company is a sign something's off.

## Skill installer and Hermes hook

`--update`/`--uninstall` only ever touch a destination this installer can prove it owns — a
plain-text marker (`.job-hunter-installed`, next to each install) recorded on every real install,
falling back to "does this destination still look like what we'd install" for one made before the
marker existed, so an already-live install keeps working without extra steps. A destination that
matches neither (someone else's directory sitting at the same path) is refused with a clear
message instead of being silently deleted; pass `--force` if you're sure and want to remove/replace
it anyway.

For Hermes, `sh scripts/install_skill.sh --hermes` also installs the candidate-profile diff hook under
`~/.hermes/agent-hooks/` and registers it in `~/.hermes/config.yaml` (`HERMES_HOME` overrides the
location for hooks and skills). The installer needs `uv` and the project's dependencies, preserves
existing config and hooks, saves the original as `config.yaml.job-hunter.bak` before rewriting (comments/
formatting may change), and skips an identical registration on re-runs. The entry is identified by the
hook script's repo-independent path, so re-running after moving the repo replaces the old registration
instead of adding a second; `--uninstall --hermes` removes exactly the entry it added. `--copy` copies
the hook too, but it still points to this checkout for the profile, database and diff script. Restart
Hermes afterwards (its first-use hook approval still applies); `hermes hooks list` shows the
registration, and `job-hunter doctor` flags one whose script no longer exists.

The hook uses the [Hermes shell hook format](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#shell-hooks)
(`post_tool_call`, `write_file|patch`, `tool_input.path`). Claude Code's `PostToolUse` hook
(`.claude/settings.json` → `scripts/run_profile_hook.sh`, a portable launcher that finds `uv` itself and
falls back to `python3`) shares the same adapter (`src/job_hunter/hook_adapter.py`). An edit to this
checkout's `config/candidate_profile.yaml` runs a best-effort `diff_profile.py` check and writes reports
to `data/profile-diff/`; other profiles are ignored, relative paths resolve against the event's `cwd`,
and terminal-based edits aren't covered. A burst of edits collapses into one run (short lock plus a 5 s
debounce). Every skip or failure (uv missing, non-zero exit, timeout) is logged to `logs/profile-hook.log`.
