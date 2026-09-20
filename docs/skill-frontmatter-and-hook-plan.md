# Skill frontmatter, onboard-source, and hook unification — Plan

Status: **implemented and reviewed, 2026-09-18.** Drafted continuing
`docs/agent-runtime-audit.md`'s remaining items (skill frontmatter/argument contract,
`onboard-source` placement, the Claude/Hermes hook rewrite, installer flags). Verified against the
actual current Claude Code and Hermes frontmatter specs (fetched live, not assumed from the
audit's prose) and the actual current state of every file involved, not just the audit's
description of them. All three open questions in section 3 were resolved by the user (see each
subsection), then implemented by one agent and independently reviewed/tested by a second — see
section 7 for the outcome. No code or skill changes were made before that beyond the standalone
version-bump fix in section 3.4 below, applied immediately since it was a correction to
the *previous* round, not part of this plan's own scope.

## 1. Problem / requested scope

1. Standardize all `SKILL.md` frontmatter to a shared, portable header (`name`, `description`,
   `license`, `compatibility`, `metadata`), with Hermes-specific data under `metadata.hermes`, and
   add an explicit input/output argument contract to each skill.
2. Move `onboard-source` into `skills/onboard-source` (out of `.claude/skills`-only) and add
   Hermes support for it.
3. Replace the fragile Claude (`.claude/settings.json`) and Hermes
   (`scripts/hermes_profile_hook.py`) profile-diff hooks with one shared Python adapter, and add
   `install --update/--uninstall/--dry-run/--copy/--link` to the installer with stale-link
   detection.

## 2. Confirmed current state — corrections to the pasted audit text

The audit text this was drafted from is accurate in places but stale or imprecise in others.
Verified directly against the real spec and the real files before proposing anything:

### 2.1 The real Claude Code frontmatter spec (fetched from code.claude.com/docs/en/skills)

Confirmed: for a **local** skill (`.claude/skills/...`, this project's actual distribution
mechanism), **every** field is accepted, no restrictions — `version` (what all six skills
currently use) causes no problem locally. The **strict** 6-field allowlist (`name`, `description`,
`license`, `compatibility`, `metadata`, `allowed-tools`) only applies to a completely different
distribution path this project has never used: uploading to claude.ai, the Skills API, or running
`package_skill.py`. Outside that path, a top-level `version` field is not an error today. The
audit's ask to adopt the strict header anyway is a reasonable forward-compatibility move (in case
this project ever does pursue that distribution path) but it is not fixing a live bug the way the
`--project` skill-example bug from the previous round was.

### 2.2 The real Hermes frontmatter spec (fetched from hermes-agent.nousresearch.com)

Confirmed: Hermes's spec **mandates** `name`, `description`, and a top-level **`version`** field
— not nested under `metadata.hermes`. This directly conflicts with the strict Claude-packaging
allowlist above, which does **not** include `version` at all. Since this project distributes the
exact same `SKILL.md` file to every runtime via symlink (or `--copy`, byte-identical either way —
see `scripts/install_skill.sh`), one physical file cannot simultaneously satisfy "no top-level
`version`" and "top-level `version` required." See section 3.1 for the resolution this needs
before any frontmatter gets rewritten.

### 2.3 The Hermes hook is already a Python adapter, not a shell one-liner

The audit text describes the Hermes hook as following "the current shell-hook shape." That's
stale — `scripts/hermes_profile_hook.py` is already a real Python script (`stdin` JSON in,
`{}` on stdout, a `run(payload, repo_root)` function), and `scripts/install_hermes_hook.py`
already merges into Hermes's `config.yaml` with a genuine round-trip-preserving backup
(`config.yaml.job-hunter.bak`, written once, before the first rewrite). Re-checked each specific
claimed weakness against the actual code:

| Claimed weakness | Still true? |
|---|---|
| Hardcodes `python3` | **Yes** — `install_hermes_hook.py`'s registered command is `["python3", <hook path>, <repo_root>]`, no interpreter discovery. |
| Hardcodes the original checkout path | **Yes** — `repo_root` is baked into the registered command string at install time; moving the repo breaks it silently. |
| Uses `uv` inside the hook without verifying its location | **Yes** — `hermes_profile_hook.py`'s `run()` calls `subprocess.run(["uv", "run", ...])` with no location check. |
| Rewrites YAML and loses comments/formatting | **Partially** — this is Hermes's own `config.yaml` (the hook *registration*, a one-time install-time operation with an automatic pre-write backup already in place), not the user's `candidate_profile.yaml` (which this hook only ever reads a path from, never rewrites). Real, but narrower and already partially mitigated than the framing suggests. |
| No clean uninstall/upgrade path | **Yes** — re-running only skips if the exact same command string is already present (`if entry in entries: SKIP`); no `--uninstall`, and a changed repo path or hook script path silently adds a second, stale entry instead of replacing the first. |
| Errors intentionally swallowed, not logged | **Yes** — `with suppress(OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired)`, no logging anywhere. |
| No test exercises an actual stdin payload | **Yes** — `tests/test_hermes_hook.py` calls `hook.run(payload, tmp_path)` directly, bypassing the `if __name__ == "__main__":` block entirely (the actual `json.load(sys.stdin)` / `sys.argv[1]` / final `print("{}")` are all untested). |

### 2.4 No LICENSE file exists

Confirmed via `ls LICENSE*` (no match) and a grep across `CLAUDE.md`/`README.md`/`pyproject.toml`
(no license declared anywhere). The pasted recommended header hardcodes `license: MIT` — that
can't be written into six files as if it were already decided; see section 3.2.

### 2.5 `onboard-source`'s actual current state

Lives at `.claude/skills/onboard-source/SKILL.md` (Claude-only, confirmed — not under `skills/`,
not in `install_skill.sh`'s `SKILL_NAMES`). Its own frontmatter today is just `name`/`description`
(no `version` at all, unlike the other five skills) — so it isn't even consistent with the rest of
the set yet, separate from the runtime-placement question. It's explicitly a **repo-maintenance**
skill ("onboard a new employer career site into `job-hunter`... this is a repo-maintenance skill
for `job-hunter` itself, not the end-user job-search skill") — used when developing/extending this
project, not when actually job-hunting. See section 3.3 for why this matters for the "and Hermes
should support it too" part of the ask.

## 3. Decisions needed before implementing

**Resolved by the user 2026-09-18:** (3.1) keep top-level `version`; (3.2) leave the project
unlicensed — no `license:` field is added anywhere; (3.3) yes, install `onboard-source` for Hermes
too, same as the other five skills, no separate flag/allowlist needed. The options considered for
each are kept below for the record.

### 3.1 The `version` field conflict (blocking) — resolved: keep top-level `version`

Three options, not a style preference:

- **(a) Keep top-level `version` as-is.** Satisfies Hermes (which requires it there) and local
  Claude Code (which accepts anything). Breaks compliance with the hypothetical future
  claude.ai-upload/Skills-API/`package_skill.py` path, which this project has never used. If that
  path is ever pursued, a separate packaging step would need to strip/relocate `version` at
  package time — not something the source `SKILL.md` files need to carry today.
- **(b) Drop top-level `version` everywhere, move it under `metadata.job_hunter.version`.**
  Satisfies the strict claude.ai/API allowlist. **Breaks Hermes today** — its spec mandates a
  top-level `version`, and Hermes has no equivalent of "look inside `metadata.job_hunter`
  instead." This is a real regression, not a neutral tradeoff, for a runtime this project actively
  installs into right now.
- **(c) Keep top-level `version` for the shared/symlinked file, and only strip it in a dedicated
  packaging step if/when claude.ai distribution is ever actually pursued.** Same practical effect
  as (a), stated as a deliberate future-proofing note rather than "we decided not to care."

My recommendation is **(a)/(c)** — they're the same outcome, just framed differently — since (b)
actively breaks a runtime this project supports today to satisfy a distribution path it has never
used. Flagging this explicitly rather than silently picking one, since it directly contradicts the
pasted recommended header's literal example (which omits `version` from the top level).

### 3.2 License (blocking for the `license:` field specifically) — resolved: leave unlicensed

There is no chosen license for this project, and the user's decision keeps it that way — no
`license:` field goes into any `SKILL.md`. Options that were on the table (kept for the record):

- Add a real `LICENSE` file (MIT, Apache-2.0, or otherwise) and reference it from `license:`.
- Explicitly mark the project unlicensed/proprietary (`license: "UNLICENSED"` or omit the field
  entirely) if it isn't meant to be redistributed. **This is the option chosen** — omit the field
  entirely, don't write a placeholder string either.

### 3.3 Does `onboard-source` actually need Hermes support? — resolved: yes

It's a repo-maintenance skill for extending `job-hunter`'s own adapter code (Python, `companies.yaml`,
`docs/SPEC.md`) — a different audience/use than the five end-user job-search skills — but the user
does do this kind of repo-maintenance work through Hermes too. Moving it to `skills/onboard-source`
(so it's installable at all outside `.claude/skills`) is unambiguous and proposed below regardless.
It's added to `install_skill.sh`'s regular `SKILL_NAMES`, installed for every target exactly like
the other five — no separate `REPO_SKILL_NAMES`/`--include-repo-skills` carve-out needed (section
4.3.1 below is updated accordingly).

### 3.4 Version-bump fix applied immediately (not part of this plan's own scope)

While preparing this plan, the previous round (`docs/pipeline-refilter-stale-source-plan.md`'s
implementation) turned out to have rewritten five `SKILL.md` files substantially — `job-hunter`
switching to the `pipeline` command, `job-radar` documenting `--no-scrape` and the stale-source
note, all five gaining `--project` — without bumping any of their `version` fields. Caught and
fixed directly, since it's a correction to already-shipped work, not something this plan's own
implementation needs to redo: `job-hunter`/`job-radar` → `1.1.0` (real procedure/capability
changes), `job-scout`/`job-reviewer`/`job-feedback` → `1.0.1` (wording/robustness only, no behavior
change). Documented as a standing rule in `CLAUDE.md`'s "Working in this repo" list so it isn't
missed a second time — this plan's own frontmatter/contract work (section 4.1/4.2) is itself
exactly the kind of change that rule requires a bump for, applied at implementation time.

## 4. Proposed design

### 4.1 Shared frontmatter

```yaml
---
name: job-reviewer
description: Score job-hunter's archived candidates against the user's resume using a local LLM (LM Studio) — resumes automatically from wherever a prior run left off, never spends cloud/agent tokens.
version: 1.1.0
compatibility: Requires uv and Python 3.11+; LM Studio required for review.
metadata:
  job_hunter:
    stage: review
  hermes:
    tags: [jobs, resume, review]
---
```

Kept: `name`, `description` (unchanged content, just confirming these already comply — no field
here is Claude-Code-only). Added: `compatibility` (new, per-skill — e.g. `job-scout`/`job-radar`
don't need LM Studio, `job-reviewer`/`job-hunter` do), `metadata.hermes.tags` (new). Kept, not
moved, per 3.1's resolution: top-level `version`. No `license:` field, per 3.2's resolution — the
project stays unlicensed and this frontmatter doesn't claim otherwise. Not adopted: `allowed-tools`
— none of these skills currently need pre-authorized tool access, and adding it speculatively
would be scope no one asked for.

`version` shown above as `1.1.0`, not `1.0.0` — every skill's version must bump when this section's
changes actually land, per the rule now in `CLAUDE.md`'s "Working in this repo" list (added after
this exact thing was missed once already: the previous round rewrote five `SKILL.md` files for
`job-hunter pipeline` support without bumping any of their versions, caught and fixed after the
fact rather than during). Adding a `## Contract` section (4.2) plus this frontmatter is itself a
real, user-visible change to what each skill documents, so it's a minor bump (new documented
capability/contract), not a patch, on top of whatever the two already-bumped versions currently
are (`job-hunter`/`job-radar` at `1.1.0`, `job-scout`/`job-reviewer`/`job-feedback` at `1.0.1` as
of the previous round) — i.e. this round moves every skill to at least `1.1.0`/`1.2.0`
respectively, computed at implementation time from whatever the version actually is then, not
hardcoded here in advance.

### 4.2 Input/output contract, per skill

Added as a new `## Contract` section in each `SKILL.md`, stated once per skill rather than
re-explained in prose scattered through the procedure (continuing the same de-duplication
discipline from `docs/pipeline-refilter-stale-source-plan.md` section 9). Example for
`job-reviewer`:

```markdown
## Contract

Input:
- project path (`--project`, defaults to `$JOB_HUNTER_ROOT`/cwd)
- keyword or archive path (`--keyword` / `--input`, defaults to the newest archive overall)
- optional limit (`--limit`, defaults to unlimited)

Output:
- status (reviewed count, skipped-cached count, or a clean error)
- archive/report path (unchanged — this skill doesn't render one)
- counts (reviewed vs. cached vs. total eligible)
- failures (per-job "skipped: <reason>" lines, never abort the whole run)
- next command (`job-radar` with the same keyword, to render results)
```

Each skill's contract reflects what it actually takes/returns today (verified against the real
CLI flags each script accepts, not invented) — `job-hunter`/`job-scout`/`job-radar`/`job-feedback`
get their own versions matching their real inputs/outputs.

### 4.3 Move `onboard-source`

`.claude/skills/onboard-source/` → `skills/onboard-source/` (matching where the other five already
live). `.claude/skills/onboard-source` becomes a symlink to it (or is removed and reinstalled via
`install_skill.sh`, once that script knows about it — see 4.3.1), so existing Claude installs don't
silently break. Frontmatter brought in line with the other five (adds `version`, `compatibility`,
`metadata` per 4.1) — it currently has neither.

#### 4.3.1 Installer awareness

Add `onboard-source` to `install_skill.sh`'s `SKILL_NAMES` directly, installed for every target
(Hermes included, per 3.3's resolution) exactly like the other five — no separate variable or
opt-in flag needed.

### 4.4 Shared hook adapter

New `src/job_hunter/hook_adapter.py` (installed package, not `scripts/` — this is small, pure
logic with no argparse CLI of its own, directly unit-testable, mirroring where `rootutil.py`/
`atomic.py`/`runlock.py` already live): one `should_run_diff(tool_input_path: str, repo_root: Path) -> bool`
function plus a `run_diff(repo_root: Path) -> None` (the actual `uv run python scripts/diff_profile.py`
subprocess call, replacing the ad-hoc one in `hermes_profile_hook.py` today). Runtime-specific
adapters become thin:

```text
scripts/claude_profile_hook.py   — reads stdin (Claude's PostToolUse JSON: tool_input.file_path),
                                    calls hook_adapter.should_run_diff/run_diff
scripts/hermes_profile_hook.py   — reads stdin (Hermes's post_tool_call JSON: tool_input.path),
                                    calls the same two functions
```

Both replace `jq`/inline shell logic with real Python JSON parsing, use `--project`/`$CLAUDE_PROJECT_DIR`
(Claude) or the repo-root argument Hermes's installer already threads through (unchanged from
today) instead of assuming CWD, discover `uv` via `shutil.which` rather than assuming PATH (fail
loudly, log, and exit cleanly if not found, rather than silently doing nothing), and log every
failure to `logs/profile-hook.log` (new — `mkdir -p` on first write) instead of swallowing it.
`.claude/settings.json`'s `PostToolUse` command becomes:

```json
"command": "uv run python \"${CLAUDE_PROJECT_DIR}/scripts/claude_profile_hook.py\" \"${CLAUDE_PROJECT_DIR}\""
```

No `jq` dependency, no POSIX-shell-specific `case` syntax, no relative path.

### 4.5 Installer flags

`install_skill.sh` gains `--update` (replace an existing symlink/copy if the source changed,
instead of always `SKIP`), `--uninstall` (remove a previously-installed skill/hook), `--dry-run`
(print what would happen, touch nothing). `--copy`/`--link` already exist as `mode=copy`/default —
made explicit, documented flags rather than only `--copy` existing with link as the unnamed
default. Stale-link detection: before `SKIP`, check whether an existing symlink's target still
matches the current source path (a moved/renamed repo leaves a dangling or wrong-target symlink)
and offer to replace it under `--update` rather than treating "something's there" as "already
correctly installed."

## 5. Testing plan

- Round-trip tests for the new `hook_adapter.py` functions (in `tests/`, not a `scripts/`-only
  test) — path matching, repo-root resolution, `uv`-not-found handling.
- A real stdin-payload test for both `claude_profile_hook.py` and `hermes_profile_hook.py` (piping
  actual JSON through `subprocess.run([sys.executable, script, ...], input=json_str)`), closing
  the gap section 2.3 confirmed exists today.
- Installer tests for `--update`/`--uninstall`/`--dry-run` against a fixture directory tree, and a
  stale-symlink-detection test (point a symlink at a since-renamed path, confirm `--update`
  replaces it, confirm a plain re-run without `--update` still just reports it rather than
  guessing).
- No live Hermes/Claude Code install exercised beyond what the existing test suite already covers
  — this project has no way to drive a real Hermes session from here.

## 6. Open questions

None remaining — all three (section 3.1–3.3) were resolved by the user 2026-09-18. Implementation
can proceed.

## 7. Implementation and review (2026-09-18)

Built by one agent, independently reviewed and tested by a second, following the same
implement-then-independently-verify pattern as `docs/pipeline-refilter-stale-source-plan.md`'s
round. Delivered exactly as designed: all six skills (`job-hunter`, `job-scout`, `job-reviewer`,
`job-radar`, `job-feedback`, `onboard-source`) gained `compatibility`/`metadata.job_hunter.stage`/
`metadata.hermes.tags` and a `## Contract` section, no `license:` field anywhere, top-level
`version` untouched in position (`job-hunter`/`job-radar` → `1.2.0`, `job-scout`/`job-reviewer`/
`job-feedback` → `1.1.0`, `onboard-source` → its first-ever `1.0.0`); `onboard-source` genuinely
moved to `skills/onboard-source` with `.claude/skills/onboard-source` now a real relative symlink,
not a duplicate; `src/job_hunter/hook_adapter.py` is the real shared logic behind both rewritten
runtime hooks (`uv` resolved via `shutil.which`, never assumed on PATH; every failure mode logged
to `logs/profile-hook.log`, never swallowed); `install_skill.sh` gained working
`--update`/`--uninstall`/`--dry-run`/`--link` with genuine stale-symlink detection.

The reviewer's first and highest-priority task was a direct safety check on `.claude/settings.json`
— the harness had flagged the implementer's own report as matching an "instruction-shaped pattern"
tied to that file. Confirmed a false positive (the flag was tripped by the report simply quoting
the new one-line hook command as text): the actual diff is exactly the single `PostToolUse`
command-string change the plan describes, nothing else added or weakened. Beyond that check,
independent verification found no real problems — every claim in the implementer's self-report
held up against the actual files, with one inconsequential inaccuracy (it reported 21 tests in
`tests/test_hook_adapter.py`; the real, independently-collected count is 15 — the coverage itself,
not just the number, was independently confirmed to include all the edge cases the plan asked for).

`uv run pytest -q` → 313 passed; `uv run ruff check .` → clean, confirmed independently by both
agents. `src/job_hunter/collector.py` untouched. No git commit created — everything is sitting in
the working tree for the user's own review before committing. (The working tree also still carries
the unrelated, already-in-progress `docs/pipeline-refilter-stale-source-plan.md` changes from the
previous round — confirmed by the reviewer to be pre-existing and unrelated to this task, not
scope creep introduced here.)
