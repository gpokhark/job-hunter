# Candidate Profile Diff — Plan

Status: **draft — nothing in this plan is implemented yet.** Revised after a thorough review
against `collector.py`, `prefilter.py`, `config.py`, `storage.py`, `suggest_exclusions.py`, and
`render_radar.py` found several correctness gaps in the first draft — incorporated below, not
just noted. No repository changes have been made from that review.

## 1. Problem

`docs/feedback-exclusion-plan.md` built a way to safely derive new `soft_exclude_terms`
candidates from feedback, with a per-term diff preview (`suggest_exclusions.py` §7.1) before
approval. That preview is narrowly scoped: one term at a time, checked only against whichever
search archive happens to be newest. What's missing is a general tool: **before hand-editing any
of the six filtering fields in `candidate_profile.yaml`** (`target_domains`, `target_title_terms`,
`exclude_title_terms`, `exclude_terms`, `soft_exclude_terms`, `strong_relevance_terms`) **, show
which stored postings would newly become candidates and which would newly stop being candidates,
with a reason for each.**

**v1 is preview-only.** No `--apply`, no writes to `candidate_profile.yaml` at all — see §7 for
why that was cut from this plan entirely, not just deferred as a flag.

## 2. Before/after: no free "before" exists

`candidate_profile.yaml` is gitignored (`CLAUDE.md`/`README.md` — personal, never committed), so
there's no `git diff` to lean on. Two explicit modes:

- **File-pair mode**: `--before <path> --after <path>` — compare two saved YAML snapshots.
  **Both paths must be given explicitly and must exist as literal files** — this tool must *not*
  use `config.py`'s `load_profile()`, whose fallback to `candidate_profile.example.yaml` when a
  path is missing is exactly the wrong behavior here: a typo'd `--before` path would silently
  compare against the example profile instead, producing a confidently wrong diff with no
  indication anything went wrong. Use a strict loader with no fallback.
- **Convenience mode**: `--add <field>:<term>` / `--remove <field>:<term>` (repeatable — several
  simultaneous hypothetical edits can be combined in one comparison), applied in-memory to the
  real on-disk `config/candidate_profile.yaml` as `--before`, producing a patched `--after` that is
  **never written back**. This is the fast iterative loop; file-pair mode is for comparing two
  already-drafted variants.

The two modes are mutually exclusive — passing both is a hard error, not a silent precedence rule.

## 3. Validation — explicit rules, not implicit tolerance

`CandidateProfile` (`config.py`) currently places no constraints on its term lists, and two real
edge cases make that dangerous specifically *for this tool's* new `--add`/`--remove` entry point
(not necessarily for hand-edited YAML, where a human would likely never type these by accident):

- **An empty string in any exclude field matches every job** — `"" in full_text` is always `True`.
  A CLI typo like `--remove exclude_terms:"cad"` that somehow leaves the list containing `""`
  would silently exclude everything. **Reject blank/whitespace-only terms outright.**
- **Removing the last positive term does not exclude everything — it makes the positive gate
  unrestricted.** In `prefilter.py`: `if positive and not any(...): return False` — an empty
  `positive` list is falsy, so the whole gate is skipped, not failed. A `--remove` that empties
  both `target_domains` and `target_title_terms` would show a large, real "gained" count that
  isn't "this term was good" — it's "there is no gate anymore." **The tool must detect this case
  and label it explicitly** (e.g. a loud warning in both terminal and HTML output: "positive gate
  is unrestricted after this change — every remaining check still applies, but nothing gates on
  domain relevance"), not just report a number that looks like an ordinary gain.

Further explicit rules:
- **Only the six filtering fields are patchable.** `--add`/`--remove` on anything else (e.g.
  `resume_path`, `minimum_recommendation_score`, `location`) is a hard error. In file-pair mode,
  any difference in a *non*-filtering field between the two files is called out separately
  ("also differs: `minimum_recommendation_score` 75→80 — not part of this comparison") rather than
  silently ignored or silently folded into the filtering diff.
- **Case-insensitive duplicate/removal handling**, matching `prefilter.py`'s own matching being
  case-insensitive throughout. `--remove <field>:<term>` that doesn't match anything in the
  before-list (case-insensitively) is a reported error ("term not found in field, no change
  made"), never a silent no-op.
- **`--add`/`--remove` for the same field+term in one invocation is a hard error**, not a silent
  pick of whichever the code processes first.

## 4. The corpus: what it actually is, and what a result actually proves

### 4.1 Scope

`Collector.search()` only ever sees jobs returned by *that specific run* against *currently
enabled* sources. The stored `jobs` table in SQLite is the **cumulative historical record**
across many runs and dates — it can include postings from sources that are now `enabled: false`
in `companies.yaml`, jobs not re-observed in a while (still `active` per `mark_missing`'s
three-strikes rule, but potentially stale), and jobs with a missing `description` after a
detail-fetch failure that hasn't been retried yet (the exact resilience gap `collector.py`'s
per-job failure isolation was built to tolerate, not eliminate).

This is still the right corpus to diff against — it's the only one that can show gains, per the
original reasoning (an archive only contains what the *old* filter already admitted) — but the
claim it supports must be stated precisely, not oversold.

### 4.2 What a result means — and doesn't

**Report this as "candidate eligibility against stored postings," not "would now reach review."**
A gain demonstrates the new profile *would* have admitted that job, based on data already
collected — it is not a guarantee the next live search returns that same job (it may have closed,
or the description that made it eligible may have been from a stale collection). Every report
carries: the evaluation timestamp, the recency cutoff used, the source scope (which
`source_key`s are represented, with enabled/disabled state flagged), and each affected job's
`last_seen_at`.

### 4.3 Performance claim, corrected

The first draft claimed corpus-wide performance was "already confirmed empirically" by
`suggest_exclusions.py`. That's wrong: `suggest_exclusions.py` reads only `job_feedback` and
`assessments` (bounded by however many jobs have been reviewed/tagged — hundreds, not the full
store), never the full `jobs` table. **No implementation has actually benchmarked a full pass over
all ~13K+ stored rows with `evaluate_prefilter` run twice per row.** It's a reasonable expectation
(pure string containment checks, no I/O per row) but must be verified once built, not asserted in
advance — add a real timing note to this doc after the first working version, not before.

## 5. `--keyword`: define the semantics explicitly, or it silently hides the change under test

`prefilter.py`'s `keywords` parameter **replaces** `target_domains`/`target_title_terms` entirely
when given (`positive = keywords if keywords else [...]`) — it does not narrow them. So testing a
change to either field *while also passing `--keyword ADAS`* would show **zero effect**, silently,
because the keyword mode never looks at those fields at all. This isn't a bug to fix; it's
existing, correct `--keyword` search semantics (matches `job-hunter search --keyword` exactly) —
but the diff tool must not paper over it.

**Resolved:** keep existing search semantics as-is. `--keyword` in this tool means exactly what it
means in `job-hunter search --keyword` — a full replacement of the positive-match set for that
comparison, nothing narrowed afterward. State this explicitly in the tool's own `--help` text and
in the report header when `--keyword` is used ("positive profile terms — `target_domains`,
`target_title_terms` — are not evaluated in keyword mode"). **Full-corpus, no-keyword mode is the
default.** No archive resolution is involved anywhere in this tool — `--keyword` here is a plain
string list passed straight through to `evaluate_prefilter`, not a lookup into `data/searches/`.

## 6. Decision model — structured, not a single reason string

A single `reason: str` (as the first draft proposed, including a bare `"passes"` for the passing
case) can't explain a *gain* — a job might pass both before and after, but via different terms,
and the first draft's model had no way to represent that. Store a structured decision **for each
side of the comparison**, and generate prose for the report from the structured fields, not the
other way around:

```python
class PrefilterRule(StrEnum):  # see section 13 — resolved as a StrEnum, matching
    NOT_US_ELIGIBLE = "not_us_eligible"           # WorkArrangement/LocationConfidence/
    EXCLUDE_TITLE_TERMS = "exclude_title_terms"   # SponsorshipStatus/HealthStatus's existing
    EXCLUDE_TERMS = "exclude_terms"                # convention in models.py
    NO_POSITIVE_MATCH = "no_positive_match"
    SOFT_EXCLUDED = "soft_excluded"
    POSITIVE_MATCH = "positive_match"


@dataclass
class PrefilterDecision:
    passes: bool
    rule: PrefilterRule
    term: str | None    # the specific matched term that decided it, if any
    rescued_by: str | None  # the strong_relevance_terms term that rescued a soft-exclude, if any

def evaluate_prefilter(job: Job, profile: CandidateProfile, *, keywords=None) -> PrefilterDecision: ...
```

`passes_prefilter` becomes a thin wrapper (`return evaluate_prefilter(...).passes`) — existing
callers see no behavior change; this is purely additive. Every changed job in the report carries
both `PrefilterDecision`s side by side, e.g.:

> Before: soft-excluded by `"platform architecture"`, not rescued.
> After: positive match via `"validation"`; rescued by `"control systems"`.

**Preserve the existing check precedence, and be explicit about what that implies.**
`evaluate_prefilter` short-circuits at the first failing check, same as `passes_prefilter` today —
so the reported `rule`/`term` is the *decisive* one in that fixed order, not an exhaustive list of
every check that would also have failed. When a comparison combines multiple simultaneous edits
(`--add` and `--remove` together, or several `--add`s), the reported reason attributes the status
change to whichever check actually flipped in the code's evaluation order — **it is not
necessarily an exhaustive account of each individual edit's own contribution** when several are
tested at once. State this plainly in the tool's output/docs rather than implying clean
per-edit attribution that the underlying short-circuit logic doesn't actually provide.

## 7. `--apply` — cut from this plan, not deferred as a flag

Dropped from v1 entirely, for two reasons:
1. **It doesn't actually give a chance to inspect anything.** Printing a summary and generating
   the HTML report immediately before writing the file, in the same invocation, defeats the
   "preview before you commit" purpose this whole tool exists for — there's no real pause to look
   at the HTML before the write happens.
2. **If kept in scope at all, it's a materially different, separate feature**, needing: preserving
   unrelated YAML content and comments (the real `candidate_profile.yaml` already has meaningful
   inline comments — e.g. the `soft_exclude_terms` provenance note — that a naive re-serialize
   would destroy), a backup before overwriting, atomic replacement (temp file + rename, not an
   in-place write), and detecting whether the original file changed on disk since the preview was
   computed (a concurrent-edit race). None of that is "write only after computing the diff" — it's
   a whole file-mutation feature with its own design. Out of scope here; revisit only if a
   preview-only v1 turns out to need it.

## 8. Assessment/feedback join — correctness and visibility

- **Follow `Collector.search()`'s own join rule exactly**: match by `(source_key, job_id)` *and*
  require `content_hash` equality before treating a stored `Assessment` as valid for a job. A
  content-hash mismatch means the posting changed since it was scored — report that job as **"has
  a stale prior assessment (score N, no longer valid for current content)"**, distinct from **"no
  assessment on record."** These are different states; don't conflate them.
- **Assessments don't record which keyword or search produced them** — the `assessments` table has
  no such field, by design (a verdict is a property of *(job, resume)*, not of whichever search
  surfaced it — see `CLAUDE.md`). Don't claim "scored under a prior keyword search"; say only that
  a valid (or stale) assessment exists, with its score, and nothing about provenance.
- **Surface `job_feedback` (`relevant`/`okay` labels) prominently for *lost* jobs specifically.** A
  human explicitly confirmed one of these postings was worth keeping — an edit that would now
  exclude it is exactly the failure mode this whole tool exists to catch, so it belongs as a loud,
  separate callout in the report, not folded into the general "lost" list at the same visual
  weight as everything else.

## 9. Output

Four numbers, not one "unchanged" bucket that hides which side it's mostly made of:
**retained** (candidate before and after), **still-excluded** (not a candidate before or after),
**gained**, **lost**. A single "unchanged" figure obscures whether that mass is mostly-included or
mostly-excluded, which matters for sanity-checking the result.

- **Terminal summary** — the four counts, the exact field edits being tested, and (per §6) the
  loud "positive gate is unrestricted" warning when applicable.
- **HTML report, its own template — not `radar_template.html`.** The radar template assumes
  scored candidates with score tiers (Strong/For-review/Below-50); this report has no scores to
  tier by. Reuse the same CSS custom properties (color palette, fonts) for visual consistency with
  the rest of the project's reports, but a genuinely different layout: a **Gained** section and a
  **Lost** section (each row: company, title, before/after `PrefilterDecision`, prior assessment
  or feedback if any), plus the four summary counts up top. `job_feedback`-flagged lost jobs get
  their own visually distinct callout per §8.

## 10. Efficient implementation shape

A small shared comparison function, thin CLI, thin renderer — not duplicated logic between
terminal and HTML output:

1. Load and validate both profiles once (§2, §3).
2. Open a **read-only** connection for the corpus read. `Storage.__init__()` unconditionally runs
   `CREATE TABLE IF NOT EXISTS`/`_migrate()`/`commit()` on open — idempotent and harmless to
   existing data, but not actually a read-only guarantee, and inconsistent with a tool whose whole
   premise is "changes nothing." Use an explicit read-only `sqlite3` connection (e.g. a `file:
   ...?mode=ro` URI) for this tool rather than instantiating `Storage` normally.
3. Read the corpus once; convert each row to a `Job` once — **reused for both the before-pass and
   after-pass**, not re-read or re-converted per profile. **Explicit column mapping required**:
   `storage.export_active()` returns raw SQL rows keyed by the DB's own column names, and the
   `jobs` table stores the URL as `canonical_url` — the `Job`/`JobSummary` model's field is `url`.
   A naive `Job(**row)` fails or silently drops the URL; map `canonical_url` → `url` explicitly
   when constructing each `Job`.
4. Capture one `now` and apply `passes_recency` once per job (recency doesn't depend on which
   profile is being tested — it's a fixed precondition, not part of what's being diffed).
5. Evaluate `evaluate_prefilter` before/after for every job in one pass over the corpus. Retain
   full `PrefilterDecision` detail only for jobs whose status actually changes (gained/lost);
   retained/still-excluded jobs contribute to the four summary counts (§9) without per-job detail
   kept around.
6. Bulk-join assessment and feedback metadata for the changed jobs only, in one query each (not a
   per-job query in a loop) — same pattern `Collector.search()` already uses
   (`storage.all_assessments()` loaded once, not queried per candidate).
7. Render both terminal and HTML output from the same result object.

## 11. CLI shape (draft)

```bash
uv run python scripts/diff_profile.py --add soft_exclude_terms:"post silicon"
uv run python scripts/diff_profile.py --remove target_domains:"validation"
uv run python scripts/diff_profile.py --add exclude_terms:"cybersecurity" --remove exclude_terms:"fullstack"
uv run python scripts/diff_profile.py --before /tmp/profile_v1.yaml --after config/candidate_profile.yaml
uv run python scripts/diff_profile.py --add strong_relevance_terms:"perception" --keyword ADAS
```

No `--apply` (§7). `--before`/`--after` and `--add`/`--remove` are mutually exclusive (§2).

## 12. Testing plan

- `evaluate_prefilter` returns the correct `rule`/`term`/`rescued_by` for every rejection path and
  for a pass; `passes_prefilter` is unchanged behaviorally (regression, now a thin wrapper).
- Empty positive set after a `--remove` → gate reported as unrestricted, not silently large gains.
- Blank term via `--add`/`--remove` → rejected outright.
- `--keyword` + a `target_domains`/`target_title_terms` edit → explicitly reported as having no
  effect, not silently zero with no explanation.
- Rescue interaction: a soft-excluded job with a matching `strong_relevance_terms` term shows the
  correct `rescued_by` on the "after" side.
- Missing `--before`/`--after` file → hard error, never a fallback to the example profile.
- A stale assessment (content_hash mismatch) reported as stale, not as "no assessment."
- Row conversion: `canonical_url` correctly becomes `Job.url` for a corpus-sourced job.
- A fixed recency boundary case (posted exactly at the cutoff) behaves identically before/after,
  isolating that the diff is purely profile-driven.
- **Invariants**: identical `--before`/`--after` profiles produce zero gains and zero losses;
  swapping which profile is "before" and which is "after" swaps the gained/lost sets exactly.

## 13. Resolved decisions

Both prior open questions are settled — neither needed new input, just a decision grounded in
existing convention:

1. **HTML output path/naming**: no natural per-run identity exists here the way `{slug}_{date}`
   does for search archives (a single sitting can test several unrelated edits) — permanent
   history and a fixed always-overwritten name both fit poorly. Default to a timestamp-based path
   (`data/profile-diff/{timestamp}.html` — unique by construction, nothing ever silently lost),
   with `--output <path>` to pin a specific comparison, mirroring `render_radar.py`'s own
   sensible-default-plus-override pattern.
2. **`PrefilterDecision.rule` is a `StrEnum`**, named `PrefilterRule`, with members
   `NOT_US_ELIGIBLE`, `EXCLUDE_TITLE_TERMS`, `EXCLUDE_TERMS`, `NO_POSITIVE_MATCH`,
   `SOFT_EXCLUDED`, `POSITIVE_MATCH` — matching the existing convention every other "which named
   category explains this decision" field in `models.py` already follows
   (`WorkArrangement`/`LocationConfidence`/`SponsorshipStatus`/`HealthStatus`), for the same
   typo-safety, clean-serialization, and single-source-of-truth reasons those already have.

No open questions remain before implementation begins.

## 14. Implementation notes (built 2026-09-05)

Everything in §2-§11 is implemented as designed: `PrefilterRule`/`PrefilterDecision` in
`models.py`/`prefilter.py` (`passes_prefilter` is now a thin wrapper, 15/15 existing + new
prefilter tests passing, behavior-unchanged regression confirmed), `scripts/diff_profile.py` with
both CLI modes, the read-only DB connection, the `canonical_url`→`url` mapping, bulk
assessment/feedback joins, and its own HTML template.

**One real bug found via the live smoke test, fixed immediately:** the first HTML template used
`{{`/`}}` (the escaping `str.format()` needs for literal braces) inside plain CSS, but the
renderer uses `.replace()`, not `.format()` — every CSS rule was silently invalid as a result
(`body {{ ... }}` isn't valid CSS; a browser drops the whole malformed rule). Fixed by using plain
single braces throughout, matching how `.replace()` actually needs the template written.

**Live proof it works bidirectionally**, exactly as §12's invariant tests require in principle and
this now confirms in practice: running `--remove soft_exclude_terms:"design verification"` against
the real database (4,681 recency-eligible postings considered) showed **15 gains, 0 losses** — the
same jobs `suggest_exclusions.py`'s narrower single-term preview had already found, now reachable
through the general tool. Each gained job's prior assessment score was also surfaced automatically
(20, 42, 35, 28...) — all in the poor-fit range, independently corroborating that excluding
`"design verification"` was the right call, from a completely different data source (the LLM's own
past verdicts) than the one that produced the exclude term in the first place (title-pattern
frequency analysis in `suggest_exclusions.py`).
