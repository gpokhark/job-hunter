# Broadened Recall via TF-IDF + Boilerplate Stripping — Plan

Status: **draft — prototype stage only.** Nothing in this plan is wired into the collection
pipeline (`prefilter.py`/`collector.py`/`cli.py`) yet; the prototype script described in §5 reads
directly from the existing SQLite store and makes no changes to it.

## 1. Problem recap

The current keyword prefilter (`passes_prefilter`, `docs/SPEC.md` §7.3) matches only against
`title + department`, deliberately excluding the free-text `description` — a documented, tested
fix for a real false-positive problem (boilerplate/optional-qualification text injecting an
irrelevant keyword into an unrelated posting).

Measured cost of that precision: replicating the full pipeline (us_eligible, exclude terms,
recency) against the whole DB and comparing "title/department match" vs. "title+department+
description match" —

| Keyword | Currently included | Would gain if description were checked |
|---|---|---|
| ADAS | 15 | +51 |
| Robotics | 11 | +110 |

Spot-checking the gained set confirmed both things are real: genuine false positives (a
"Senior Manager, Forensic Investigations" role matching only because "ADAS" appears once in a
list of systems the team investigates) *and* genuine misses (Honda Research Institute's
"Research Scientist: ..." titles, which are real robotics-adjacent roles that just don't use the
word "robotics" in the title).

## 2. Approach

Two independent, composable, dependency-free techniques, used together:

- **(A) Per-source boilerplate-paragraph stripping** — statistical recurrence detection, not
  structural/semantic HTML parsing (§4.1).
- **(B) TF-IDF cosine similarity** between the resume and each (cleaned) job description, as a
  ranking signal over the pool description-matching would otherwise add wholesale (§4.2).

## 3. Why this design

- **No new dependency.** Hand-rolled TF-IDF (term frequency × smoothed IDF, cosine similarity) is
  ~80 lines of plain Python — scikit-learn/numpy would be overkill for a few hundred short
  documents per run, and the project currently has a deliberately minimal dependency footprint
  (`httpx`, `pydantic`, `pyyaml`, `selectolax`).
- **Fits the existing architecture.** `prefilter.py`'s `relevance_score()` is already documented
  as "ordering only, no gating role," already uses the full description — TF-IDF cosine similarity
  is a strictly better version of that same idea, not a new category of thing. The final
  evidence-based judgment call stays with the local LLM; this only changes what reaches it.
- **IDF weighting partially solves the boilerplate problem for free.** A phrase that recurs across
  many postings (company-wide "About Us" copy, or industry-wide "autonomous driving" marketing
  language several companies share) has low document frequency and gets naturally down-weighted in
  a cosine-similarity comparison — no explicit "About Us" section detection required. Per-source
  paragraph stripping (§4.1) additionally catches company-*unique* boilerplate that a global corpus
  wouldn't down-weight on its own (a phrase repeated in every GM posting but not used anywhere
  else still has non-trivial global IDF).
- **Additive, not a replacement.** The existing title+department hard gate stays exactly as-is —
  it's the proven precision safety net. This produces a **second, clearly separate pool**
  ("broad matches") of description-only matches, ranked by similarity — never silently merged into
  the primary `candidates` list.

## 4. Algorithm detail

### 4.1 Boilerplate detection (per-source, frequency-based)

1. Group all active jobs by `source_key`.
2. For a source with at least `min_postings` (default 5) stored postings, split each posting's
   raw HTML description into block-level chunks (split on `</p>`, `<br>`, `</li>`, `</div>`,
   heading close tags), strip tags, normalize whitespace. Discard chunks under ~40 characters
   (avoids treating short repeated headers like "Responsibilities:" as boilerplate).
3. Count, per chunk, how many *distinct* postings from that source contain it.
4. A chunk is boilerplate if it appears in ≥ `boilerplate_threshold` (default 50%) of that
   source's postings.
5. `clean_description(job)` = the original description with every boilerplate chunk for that
   source removed, tags stripped, whitespace normalized.

This is deliberately *not* trying to identify "the About Us section" as a structural unit — that
would need reliable heading detection across ~10 different ATS platforms with no shared structure,
several of which have no headers in their descriptions at all. Recurrence-based detection sidesteps
that entirely: boilerplate is defined by *appearing verbatim across many of that company's
postings*, regardless of where in the document it sits or whether it's under a heading.

### 4.2 TF-IDF + cosine similarity

- **Tokenization:** lowercase, `[a-z][a-z0-9+#./-]{1,}` word regex (keeps tokens like `c++`,
  `adas`), filtered against a small hardcoded English stopword list (no dependency).
- **Corpus:** every active job's *cleaned* description (§4.1) — the full ~10,400-row store, not
  scoped to one keyword search, for a more statistically stable IDF than a single run's ~50-300
  candidates would give.
- **IDF:** smoothed, `log((1 + N) / (1 + df[term])) + 1`.
- **TF:** log-scaled per document, `1 + log(count)`.
- **Vector:** `{term: tf * idf}`, L2-normalized per document.
- **Resume vector:** computed against the *same* corpus IDF table, so it's directly comparable.
- **Similarity:** cosine = dot product of the two normalized vectors.

### 4.3 The "broad matches" pool

For a given `--keyword`:
1. `base_ok(job)`: `us_eligible`, not excluded by `exclude_title_terms`/`exclude_terms`, passes
   the recency window — identical checks to today's `passes_prefilter`/`passes_recency`.
2. `tight_matches`: `base_ok` jobs where the keyword is in `title + department` (today's existing
   behavior, unchanged).
3. `broad_candidates`: `base_ok` jobs where the keyword is in `title + department + clean_description`
   **and the job is not already in `tight_matches`** — i.e. purely the incremental set description
   matching would add.
4. Rank `broad_candidates` by cosine similarity to the resume, descending.

## 5. Prototype plan (this step — standalone, no pipeline changes)

- **New file:** `scripts/prototype_tfidf_broad_match.py`, clearly marked in its docstring as an
  experimental prototype, not part of the production pipeline.
- **Reads:** `Storage.export_active()` and the configured resume — read-only, no DB writes, no
  changes to `prefilter.py`/`collector.py`/`cli.py`.
- **CLI:** `--keyword` (required, comma-separated), `--top` (default 20), `--min-postings`
  (default 5), `--boilerplate-threshold` (default 0.5), optional `--csv <path>`.
- **Output:** ranked table (score, company, title, source, department) for the broad pool to
  stdout, plus boilerplate-stripping stats per source (how much text got removed). The script also
  explicitly locates and prints where known reference postings land in the full-corpus similarity
  ranking (the PACCAR/Hyundai false positives found earlier, expected low; HRI's robotics-adjacent
  research titles, expected mid-to-high) so ranking quality is visible immediately without manual
  DB digging on every run.

## 6. Success criteria for the prototype

- The known false-positive examples (PACCAR "Electrical Engineer - Siemens Capital Administrator",
  Hyundai "Senior Manager, Forensic Investigations") rank low relative to the rest of the broad
  pool.
- The known plausible true positives (HRI's robotics-adjacent research titles) rank in the upper
  half.
- Boilerplate stripping visibly reduces shared/repeated text per source (reported as a stat, not
  just assumed).
- A manual eyeball of the top 20 by the user looks reasonable.

## 7. Next steps after the prototype validates

- Wire into `job-scout` as an opt-in `--broad` output, kept clearly separate from `candidates`.
- Tune the similarity threshold empirically against real local-LLM verdicts once a broad pool has
  actually been reviewed once — not guessed in advance.
- Decide promotion criteria (whether "broad" ever becomes part of the default flow) as an explicit
  later decision, not an automatic consequence of the prototype working.
- Add unit tests: TF-IDF/cosine on a small synthetic corpus with known expected ordering;
  boilerplate detection on synthetic postings with a deliberately repeated paragraph.

## 8. Prototype results (first run, 2026-09-04)

Ran `scripts/prototype_tfidf_broad_match.py --keyword ADAS,Robotics` against the live DB (10,403
active jobs). **Results are genuinely mixed — not a clean validation of §6's success criteria.**

**Boilerplate stripping worked as expected:** ~14% of total description text across 20
sources removed as recurring boilerplate (Honda: 14 chunks, GM: 8, Toyota: 12, Nissan: 7, etc.).

**Calibration check — local rank within each keyword's own broad pool (not the full corpus):**

| Reference posting | Expected | Broad-pool rank |
|---|---|---|
| PACCAR "Electrical Engineer - Siemens Capital Administrator" (ADAS) | low | 33/49 (bottom third) — roughly as hoped |
| Hyundai "Senior Manager, Forensic Investigations" (ADAS) | low | **22/49 (middle of the pack)** — not clearly suppressed |
| HRI "AI Research Scientist: Computational Social Systems" (Robotics) | mid-high | **99/108 (bottom ~8%)** — the opposite of hoped |

Two real limitations, not implementation bugs:
- **TF-IDF doesn't suppress the Hyundai false positive reliably** — a management/investigations
  role's description still shares plenty of generic engineering vocabulary with a technical
  resume even though the actual job function is unrelated. Bag-of-words similarity measures word
  overlap, not job function or seniority.
- **TF-IDF doesn't surface the HRI true positive** — HRI's academic-research-lab phrasing
  ("cognitive modeling," "multimodal learning," "embodied," "computational social systems") shares
  little surface vocabulary with a more industry/engineering-styled resume, even though the role
  is conceptually related. This is the classic TF-IDF blind spot: it can't bridge a
  vocabulary/register gap the way a semantic/embedding model could.

**What did work, qualitatively:** the top of the ADAS broad-pool ranking looks genuinely good —
Ford "BlueCruise Feature and SW Engineer" (Ford's actual ADAS/hands-free-driving brand), GM
"Senior Software, AV Platform Core Test," GM "Senior Robotics Engineer (Dynamics and Controls),"
Stellantis "AI Validation Engineer," Ford "HIL System Engineer" — all plausible, genuinely
ADAS-adjacent roles that the current title-only gate misses entirely. Robotics surfaced GM
"Robotic Process Simulation Engineer," GM "Senior Software Engineer, Autonomy Evaluation," GM
"Senior Controls Engineer" similarly.

**Conclusion — pivoting the recommendation from §7:** the similarity score is not reliable enough
to trust as an automatic filter or threshold on this evidence (one calibration case failed in each
direction). What the prototype *is* clearly good for: **keyword discovery.** The top-ranked broad
matches surfaced real title vocabulary the profile/keyword list doesn't currently have —
"BlueCruise," "AV Platform," "Autonomy Evaluation," "HIL," "ASWb," "Perception," "DAT Feature" —
each of which is a precise, title-level term that could be added directly to `--keyword` and
recovers real candidates through the *existing*, already-validated title+department gate, with no
new filtering mechanism, no threshold tuning, and no risk to precision. This is a strictly safer
path to the recall improvement than promoting TF-IDF similarity into a gating/ranking role.

**Revised next step:** use this prototype as a one-off (or periodic) keyword-discovery scan, not
as a production filter. §7's "wire into job-scout as `--broad`" plan is downgraded from "next
step" to "not recommended based on this evidence" unless a future iteration (e.g. swapping cosine
similarity for something that captures synonymy, or requiring the keyword to appear within N words
of a role/responsibility-flagged sentence rather than anywhere in the cleaned description) closes
the gap the calibration check exposed.

## 9. Open questions / decisions

- Threshold values (`min_postings`, `boilerplate_threshold`, and an eventual similarity cutoff)
  start at the defaults above; expect to tune them from what the prototype actually shows, not
  from theory.
- Should the "broad" pool ever reach `job-reviewer` (real LLM cost), or stay a human-eyeball-only
  scouting list indefinitely? Left open until the prototype's ranking quality is judged.
- IDF is recomputed from the live DB on every prototype run rather than cached — at ~10K short
  documents this is fast enough (sub-second) that caching isn't worth the complexity yet.
