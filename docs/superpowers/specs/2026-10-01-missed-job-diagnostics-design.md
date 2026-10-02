# Missed-job diagnostics and near-miss discovery — design

Date: 2026-10-01. Status: awaiting review. Path: architectural (two new commands, a new report, new
modules, skill and doc changes; no change to the filtering gate itself).

## 1. Problem

A job the user wanted was missing from the radar: Ford "Vehicle Calibration & Test Supervisor"
(Job ID 71202, posted 2026-09-30). Traced stage by stage on 2026-10-01:

| Stage | Outcome |
|---|---|
| Collected (Ford Oracle HCM, `ok`, 842 jobs) | yes, 9,281-char description |
| U.S.-eligible, recent | yes (Dearborn MI, structured country, posted 2026-09-30) |
| Title+department positive gate | **rejected, rule `no_positive_match`** |
| Archive candidates, review, radar | never reached |

Why the gate rejected it: terms match as exact phrases against title + department only. The title
contains none of the 21 `target_title_terms`/`target_domains` (`vehicle test` does not match
"Vehicle Calibration & **Test**"), and Ford's department is empty. Description matching was removed
on purpose (boilerplate false positives; CLAUDE.md, `docs/SPEC.md` §7.3), so nothing else sees the
job. The gate is working as designed; the failure is that **a miss is invisible and there is no
cheap way to ask "why wasn't X here" or to notice a new vocabulary gap before it costs a job.**

Measured on the live DB (13,948 active, U.S.-eligible, recent jobs):

- 733 pass the gate; 12,160 are rejected for `no_positive_match`.
- 61% of all eligible jobs (13,472 of 21,886) have an **empty department**; 42 sources are 100%
  empty (Apple 3,813, Ford 814, Magna 769, Nvidia 721, GM 497, ...). For these the title is the
  only gate signal.
- Ford's feed never fills any department-like field (`Department`, `JobFamily`, `JobFunction`,
  `Organization`, `BusinessUnit`, ... all null on all 854 records) — **mapping one is a verified dead
  end, not a task.**
- Of the 12,160 rejects, 760 contain >= 2 and 284 contain >= 3 distinct strong-relevance terms
  (excluding generic ones) in their description. The Ford job is *not* one of them (its description
  has none of the user's autonomy/ADAS terms): a description-signal list alone would not have caught
  it, which is why vocabulary maintenance and an on-demand "why" tool are both needed.
- Adding one precise title term shows the size of the class: `calibration` would admit 25 more jobs
  (including Apple "Camera Calibration and Test Engineer", Ford "Calibration Engineer");
  `test supervisor` would admit 2 (this Ford job and an ABB role).

## 2. Goals and success criteria

- **A.** The user can add title/domain vocabulary with a preview of exactly which jobs each term
  admits, and nothing is written without confirmation (existing `job-feedback` flow).
- **B.** `job-hunter why-missed <url | job id | source:id | title>` explains, stage by stage, why a
  stored job is or is not in the candidates/radar, and for a gate rejection proposes the smallest
  title terms that would admit it with their blast radius. Deterministic, read-only, no LLM.
- **C.** `job-hunter near-misses` produces a periodic, human-only report of jobs the gate rejected
  that look relevant from their description, new since the last scan, plus ranked vocabulary hints
  and a department-coverage table, so gaps are noticed before a job is missed.
- Re-running the Ford case through B reproduces the finding above and suggests `test supervisor`
  and `calibration` with correct gain counts.

Non-goals: no change to `prefilter.py`'s gate semantics or to what enters `candidates`; near-misses
are never LLM-scored, never added to the archive, never merged into the radar; no automatic profile
edits; no adapter/department mapping work (the coverage table only tells us where to investigate);
no pipeline stage in this iteration; no embeddings/TF-IDF (the existing `docs/broad-match-plan.md`
concluded similarity ranking is unreliable as a filter and good only for keyword discovery, which is
what C does with plain term counts).

## 3. Constraints carried in (CLAUDE.md)

- CLAUDE.md's rule: a wrongly *rejected* relevant job is the costly error, because it is invisible
  in every report. These tools make rejections visible and explainable without loosening the gate.
- Python owns retrieval/filtering/persistence; the skill owns judgment. New logic is deterministic
  Python; skills only orchestrate and gate on user confirmation.
- SQLite is opened genuinely read-only (as `diff_profile.py` does), never via `Storage`.
- `--project`/`add_project_argument` on every new command (and a parser test that it works after the
  subcommand — a real past bug). Reports written atomically (`atomic_write_text`), local-timezone
  timestamps, never into `data/searches/` or `data/radar/`.
- Any content change to a `skills/*/SKILL.md` bumps its `version`; skills stay generic (no employer
  names or personal data).

## 4. A — Vocabulary (human-gated, no new code)

Uses the existing machinery: `scripts/diff_profile.py --add target_title_terms=<term>` previews the
retained/still-excluded/gained/lost jobs without writing; `skills/job-feedback` writes only after
the user's confirmation and accepts the baseline only after a second one. The plan's first
operational task runs this with the user for `calibration` and `test supervisor`, reviews the gained
jobs together, and applies only the terms the user confirms. B and C feed this loop with
evidence-backed suggestions.

## 5. B — `job-hunter why-missed`

**Interface.** `job-hunter why-missed REF [--search PATH] [--keyword K] [--json] [--project P]`.
`REF` is resolved read-only against `jobs`: exact `canonical_url`; a numeric/ID token found in the
URL; `source_key:job_id`; a bare `job_id`; else a case-insensitive title substring. Several matches
-> a numbered list and exit 2 (user re-runs with `source:id`). No match -> "never collected": print
the likely source's last health row and the `source-test` command, exit 2.

**Stages reported** (each with the evidence value): (1) collected: row status, first/last seen,
missing_count, source health; (2) U.S.-eligible (+ evidence); (3) recency (posted_at vs
`max_posting_age_days`); (4) prefilter via `evaluate_prefilter` with the current profile (rule,
term, rescued_by; honors `--keyword`); (5) present in the resolved archive's `candidates`
(`resolve_search_path`, or `--search`) and its stored assessment (score, or "not assessed", or
"stale: content changed"); (6) summary line naming the first failing stage.

**Suggestions** (only for `no_positive_match`): candidate phrases = contiguous 1-3-word n-grams of
the title (lower-cased, punctuation and `&` removed) that are not made only of generic tokens
(senior, staff, principal, lead, manager, engineer, specialist, director, sr, jr, ii/iii, and, of,
the, for, ...). Each candidate's **gain** = number of jobs in the rejected `no_positive_match` pool
admitted by adding it to `target_title_terms`, computed with the real `evaluate_prefilter` on a
profile copy (same matching semantics as production), with 3 sample titles. Ranked by smallest gain
first (most precise), then longer phrase; a gain above a threshold (default 40) is flagged "broad".
Output ends with the exact preview command (`diff_profile.py --add ...`) and a pointer to the
`job-feedback` skill. Never edits the profile.

**Placement.** `src/job_hunter/why_missed.py` (pure: `parse_ref`, `find_jobs` via a new public
`active_pool.find_jobs(db, ref)`, `explain(...) -> WhyMissed` dataclass, `suggest_terms(...)`);
cli.py wiring; text and `--json` renderers.

## 6. C — `job-hunter near-misses` (periodic discovery)

**Pool.** Active + U.S.-eligible + recency-passing jobs whose prefilter rule is `no_positive_match`
(not `exclude_title_terms`/`soft_excluded`, which are deliberate rejections).

**Score.** Distinct `strong_relevance_terms` found (word-boundary, case-insensitive) in the job's
description after per-source boilerplate-paragraph stripping (paragraphs recurring in >= 30% of that
source's postings are removed), minus a default generic set (vehicle, behavior, behaviour, driving,
chassis, camera, radar; overridable with `--ignore-term`). Default threshold `--min-terms 3`; ties
broken by total occurrences.

**Periodicity.** `data/near-miss/state.json` records `last_scan_at`; by default only jobs first seen
after it are listed (`--all` lists everything, `--limit N` caps by score; first run with no state
behaves like `--all --limit 100`). State is advanced only after the report is written.

**Output** (`data/near-miss/<local-timestamp>.html` + `.csv`, atomic): (1) header with counts and
the **department-coverage table** (overall % empty; top 10 sources by empty-department job count,
labelled "title is the only gate signal here"); (2) near-miss rows: rank, source, title, score,
matched terms, posted/first-seen, link; (3) **vocabulary hints**: frequent title n-grams across the
near-misses that are not already profile terms, each with its pool gain, sample titles and the
`diff_profile.py --add` preview command.

## 7. Data flow and error handling

Read-only SQLite -> pure functions -> text/JSON (B) or HTML/CSV (C). A missing/unreadable DB or
profile fails loudly with the existing messages (exit 2). C never writes state on failure. Both
commands are safe to run any time, including while a collector or the live radar runs (WAL readers).

## 8. Testing (test-first)

Temp SQLite fixtures with a Ford-shaped job (title, empty department, long description) and
look-alikes. B: ref parsing (url/id/`source:id`/title, ambiguity, not found), each stage verdict,
the no-positive-match suggestion list and gain counts, `--keyword`, archive membership and
assessment states, JSON shape. C: scoring and the generic-term filter, boilerplate stripping, state
file semantics (`--since-last`, `--all`, first run, no advance on failure), HTML escaping, CSV
shape, coverage table, hints. CLI parser tests including `--project` after the subcommand. A
regression test pins the Ford example end to end (reports `no_positive_match`; suggests `test
supervisor`).

## 9. Docs and skills

README commands; CLAUDE.md commands block + one architecture bullet for each new module;
`docs/SPEC.md` new subsection (missed-job diagnostics, the coverage and Ford findings, the gate's
known limits); `skills/job-feedback` (when the user reports a missed job, run `why-missed`; vocabulary
loop) and `skills/job-scout` (optional `near-misses` after a scrape) with `version` bumps and generic
wording.

## 10. Risks and open decisions

- Suggested terms can be broad; mitigated by showing the exact gain and a "broad" flag, and by the
  human gate before any write.
- Boilerplate stripping can over-strip; thresholds are flags, defaults conservative.
- The first near-miss run lists up to 100 jobs; afterwards only new ones.
- **Decisions (defaults proposed, tell me to change):** (1) near-misses are never sent to the LLM
  reviewer (human-only list; the broad-match plan's open question, answered "no" for now); (2) both
  commands are standalone for this iteration, no `pipeline` stage; (3) work happens on a new branch
  `feature/missed-job-diagnostics` created after the pending changes on the current branch are
  committed.
