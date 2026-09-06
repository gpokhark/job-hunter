---
name: job-feedback
version: 1.0.0
description: Turn radar feedback clicks and any candidate_profile.yaml change — a manual edit or a previously-suggested one — into a reviewed, confirmed profile update, showing exactly which jobs it gains/loses. Nothing is ever written to candidate_profile.yaml, or accepted as the new baseline, without your explicit yes.
---

# Job Feedback

Use this skill to close the loop after tagging jobs 👍/🟢/👎 in a radar report *or* a
profile-diff report (both render the identical feedback buttons and export the identical
`radar-feedback-*.json` shape — this skill never knows or needs to know which report a label
came from), or any time you want to know "what changed because of my last edit to
candidate_profile.yaml" — including an edit you made by hand outside of any tool. It replaces
running `apply_radar_feedback.py`, `suggest_exclusions.py`, and `diff_profile.py` as three
separate manual commands with one conversational pass over the same three deterministic scripts.

**Nothing here is an LLM judgment call.** Every number this skill reports comes from a
deterministic script — no job is scored, no term is invented, no exclusion is suggested by this
agent. The only two things that require you specifically: approving which suggested terms (if any)
get added to `candidate_profile.yaml`, and confirming that a shown diff should become the new
baseline. Both are explicit stop-and-ask points, never inferred from silence or batch-applied.

## Examples

- `/job-feedback` — after tagging jobs in a radar report and exporting: ingest the tags, see
  suggestions, review the resulting profile diff
- `/job-feedback` — you just hand-edited `candidate_profile.yaml` in your editor and want to know
  what that changed, with no feedback file involved at all
- `/job-feedback` — routine check-in with no new feedback and no edits: reports "nothing changed"
  quickly and stops

## Procedure

1. Work from the project directory containing `pyproject.toml`.
2. Ingest any new radar feedback — safe to always run, idempotent, does nothing if there's
   nothing to ingest:
   ```bash
   uv run python scripts/apply_radar_feedback.py
   ```
   Auto-resolves the newest `radar-feedback-*.json` in `~/Downloads` and reports which file it
   used and its timestamp — relay that so the user can catch a stale pick (e.g. they tagged jobs
   today but forgot to click Export, and this is picking up an old file). Report the new/changed/
   unchanged/invalid counts. If it says nothing was found, that's a normal outcome, not an error —
   continue to the next step regardless.
3. Generate suggestions from every feedback label recorded so far (not only what step 2 just
   ingested) — every job with feedback is re-evaluated against the CURRENT profile, so this
   spans all six filtering fields, not just `soft_exclude_terms`:
   ```bash
   uv run python scripts/suggest_exclusions.py
   ```
   Relay each bucket the script prints, verbatim, in order — don't skip a bucket just because
   it's empty ("(none)" is itself information):
   - **soft_exclude_terms** (add) — irrelevant-tagged jobs currently passing the filter.
   - **strong_relevance_terms** (add) — relevant/okay-tagged jobs currently soft-excluded with
     no rescue.
   - **target_domains / target_title_terms** (add) — relevant/okay-tagged jobs currently
     matching no positive term at all.
   - **exclude_title_terms / exclude_terms** (**remove**) — relevant/okay-tagged jobs currently
     hard-blocked. Flag this bucket loudly: these two fields have no rescue mechanism at all, so
     a hit here is a real false negative already happening, not a hypothetical one.
   - **strong_relevance_terms CAUTION** — irrelevant-tagged jobs currently rescued by an
     existing `strong_relevance_terms` word. No suggested edit comes with this one (narrowing or
     removing that word is too blunt to propose blind) — just relay it so the user can judge for
     themselves.
   - **Not fixable via profile terms** — relevant/okay-tagged jobs failing on U.S.-eligibility;
     nothing here is a `candidate_profile.yaml` edit.

   For every add/remove candidate, relay the term, how many titles it matched, example titles,
   and the script's own live preview (`Preview vs. every stored job: retained=... gained=...
   lost=...`, plus up to 5 example jobs on each side) — this preview is a real `evaluate_prefilter`
   sweep via `diff_profile.py`'s own `compute_diff`, not an approximation. Also mention the
   below-confidence (single-occurrence) list exists, without pushing the user toward it. Don't
   editorialize about whether a suggestion looks safe beyond what the script itself reports — its
   zero-collision-with-the-opposing-label property is the safety guarantee, not your judgment.
   If there's no feedback at all yet, the script says so — skip straight to step 5.
4. **Stop and ask which suggestions, if any, to apply — never assume, never batch-apply.** Accept
   a plain-language answer ("add the first one," "none of these," "add 'platform architecture' to
   soft_exclude_terms," "remove 'intern' from exclude_title_terms"). For each one the user
   approves:
   - Make a minimal, targeted edit to `config/candidate_profile.yaml` — insert (or, for an
     exclude_title_terms/exclude_terms suggestion, delete) one list item in the field the
     suggestion named. Never regenerate or re-serialize the whole file: a surgical text edit is
     what keeps the file's existing comments and formatting intact, which is why this is safe for
     an agent to do directly (unlike a script doing `yaml.dump()` after a full parse, which would
     destroy them — see `docs/profile-diff-plan.md` section 7).
   - If the user approves none, or there were no suggestions, proceed to step 5 anyway — a manual
     edit made outside this conversation is exactly as valid a reason to run it.
5. Check what actually changed in the profile, regardless of source (step 4's edits, an earlier
   manual edit in your editor, or nothing at all):
   ```bash
   uv run python scripts/diff_profile.py
   ```
   This is check mode: it diffs the current on-disk profile against the tracked baseline
   (`data/candidate_profile.snapshot.yaml` — the profile as of the last time a baseline was
   accepted) and reports exactly which jobs would gain/lose candidacy and which profile terms
   changed (+added/-removed), independent of whether the change came from this conversation or
   was already sitting in the file before you ever ran this skill. **It never touches the
   baseline itself** — re-running it any number of times shows the same thing safely.
   - "No prior baseline found" means this is the very first time check mode has run — there's
     nothing to compare against yet; say so and stop, nothing else to do this round.
6. Present the result: the Retained/Still-excluded/Gained/Lost counts, the Profile terms +/- list,
   and the HTML report path. **Call out loudly** any Lost job flagged `TAGGED RELEVANT`/`TAGGED
   OKAY` — that means a human already confirmed this posting was worth keeping, and the pending
   change would now exclude it; this is exactly the failure mode the whole mechanism exists to
   catch. If Retained/Gained/Lost are all zero and the term diff is empty, just say plainly that
   nothing changed since the last accepted baseline — don't manufacture a longer report.

   **If your runtime can publish artifacts, publish this diff report — every time, without being
   asked.** This is a standard step of running this skill, the same way job-radar always publishes
   its own report (see that skill's step 8) — never something to wait for an explicit request for.
   A fresh diff (a new `--add`/`--remove`/`--before`/`--after` comparison, or a genuinely new check
   mode result since the last one you published) gets its own new link; re-publishing the exact
   same comparison updates the same link rather than creating a duplicate. If nothing changed
   (step 6's "nothing changed" case), there's nothing new to publish — don't publish a no-op diff.
   If your runtime has no such capability, give the user the local file path instead.
7. **Stop and ask before accepting the baseline — a separate confirmation from step 4's.** The
   diff in step 6 can reflect a change from entirely outside this conversation, so "the user
   approved a suggestion in step 4" is never sufficient grounds to accept it on their behalf here.
   Only on an explicit yes:
   ```bash
   uv run python scripts/diff_profile.py --accept-baseline
   ```
   If the user says the diff looks wrong (e.g. that `TAGGED RELEVANT` job), do not accept —
   help them revise `candidate_profile.yaml` instead (with the same step-4 discipline: minimal,
   approved, targeted edits only) and re-run step 5 to see the corrected diff.
8. If a baseline was accepted in error on a prior run, `--rollback-baseline` swaps it back to what
   it replaced (one level of history only — a second rollback undoes the rollback, it doesn't go
   back further). Only run it on an explicit request to undo the last accepted baseline.
9. This skill never renders a report or touches a search archive. If the user wants the updated
   candidate list reflected in an actual radar page, point them at `job-radar` (optionally with
   `--refilter` first via `scripts/refilter_archive.py`, which re-applies the now-current profile
   to already-collected jobs with no new network calls).
