---
name: salary-compare
version: 1.7.0
description: Compare a job offer against the user's current total compensation (base, bonus, 401k match, perks, premiums, taxes, cost of living), research the market rate, compare city/state standard of living and state income tax, and write a negotiation script and email using Dale Carnegie warmth plus Chris Voss's "Never Split the Difference" tactics (calibrated questions, labeling, mirroring, the Ackerman model). Use whenever the user mentions a job offer, an upcoming offer, comparing salaries, "should I take this offer", counter-offer, salary negotiation, or total compensation for a specific company and role. All arithmetic comes from scripts/salary_compare.py, never from mental math.
compatibility: Requires uv and Python 3.11+. scripts/salary_compare.py is stdlib only — no extra installs. scripts/market_lookup.py additionally reads job-hunter's own data/jobs.sqlite3 (read-only) for real company/role comparables. Market pay and cost-of-living research use WebSearch/WebFetch (or Playwright MCP for interactive calculators, if this runtime has it configured — not required).
metadata:
  job_hunter:
    stage: negotiation
  hermes:
    tags: [jobs, salary, negotiation]
---

# Salary Compare

Split of responsibilities: **the script does every calculation** (taxes, net pay, break-even salary, negotiation numbers) so results are reproducible; **you do the research, judgement and writing**. Never compute compensation figures yourself: put the numbers in the JSON files, run the script, and quote its output.

## Contract

Input:
- the offer details the user has (company, role, base, bonus, location, whatever else is known) — ask for what's missing
- project path (`--project`; defaults to `$JOB_HUNTER_ROOT`/cwd)

Output (in `data/output/<Company_Name>/`):
- `offer_<Company>_<Role>_<YYYY-MM-DD>.json` — the filled-in offer + market data (round 2+: `offer_<Company>_<Role>_round<N>_<YYYY-MM-DD>.json`, see Step 9)
- `comparison_<YYYY-MM-DD>.md` — the script's full comparison table for this round (narrative, for reading; round 2+: `comparison_round<N>_<YYYY-MM-DD>.md`)
- `comparison_<Company>_<Role>.csv` — **one running ledger per job, not dated, never recreated** — `Metric, Current, <round/ask 1 label>, <round/ask 2 label>, ...`: every `compare --csv-out` call adds or replaces its own named column and refreshes `Current`, so the whole negotiation's history (every offer round and every what-if ask) lives in one file — always update this alongside the dated `.md` (Step 6)
- `negotiation-plan_<YYYY-MM-DD>.md` — the negotiation script/email deliverable (round 2+: `negotiation-plan_round<N>_<YYYY-MM-DD>.md`)

## Files

| Path | Tracked | Purpose |
|---|---|---|
| `config/salary_config.example.json` | yes | Template for the user's current package |
| `config/salary_config.json` | **gitignored** | User's real current package (`salary_compare.py init` creates it) |
| `config/offer.example.json` | yes | Template for one offer + market data |
| `data/output/<Company_Name>/offer_<Company>_<Role>_<YYYY-MM-DD>.json` | gitignored (`data/*`) | One file per real offer |
| `scripts/salary_compare.py` | yes | Deterministic calculator (stdlib only) |
| `scripts/market_lookup.py` | yes | Read-only market-comparables lookup against job-hunter's own job database |
| `scripts/data/tax_data_2026.json` | yes | Federal/FICA/state tax tables (with `verified` flag) |
| `tests/test_salary_compare.py` | yes | `uv run pytest tests/test_salary_compare.py` |
| `tests/test_market_lookup.py` | yes | `uv run pytest tests/test_market_lookup.py` |
| `data/output/<Company_Name>/comparison_<YYYY-MM-DD>.md` | gitignored (`data/*`) | Script's comparison report, one per round |
| `data/output/<Company_Name>/comparison_<Company>_<Role>.csv` | gitignored (`data/*`) | Running ledger, one column per round/ask, never dated/recreated |
| `data/output/<Company_Name>/negotiation-plan_<YYYY-MM-DD>.md` | gitignored (`data/*`) | Final deliverable (always markdown) |

Never put real compensation numbers in a tracked file. Personal data goes only in the gitignored files above. Every command below takes `--project "$CLAUDE_PROJECT_DIR"` (Claude Code) — or the equivalent workspace path for another runtime such as Hermes.

## Step 1: Current package

1. If `config/salary_config.json` is missing, run `uv run python scripts/salary_compare.py init --project "$CLAUDE_PROJECT_DIR"`.
2. Read it. Any key marked placeholder or `_note: ... confirm` must be confirmed with the user before the comparison is trusted. Ask for everything missing in one message, not one question at a time. Typical gaps: city, monthly housing cost, whether the employer vehicle is a taxable fringe benefit, what an equivalent insurance policy would cost, PTO days, how much they contribute to the 401k.
3. Schema notes: `bonus.expected_payout_pct_of_target` is what the bonus historically pays (100 = target); perks accept `annual_value`, `monthly_value` or `per_workday_value`; `employee_costs` are what the user pays (premiums), `pretax: true` for payroll-deducted health premiums. `retirement.employee_contribution_pct` and `retirement.match_cap_pct` are both a % of the *same* eligible-compensation base — **base salary only by default**; set `match_on_bonus: true` only if the real plan's deferral % and match both actually apply to bonus/retention-bonus pay too (most plans are base-only; ask rather than assume). `retention_bonuses` is an optional list for a fixed-dollar award vesting over several years (new-hire or performance retention bonus paid in prorated installments): each entry's `total_amount / vesting_years` is annualized and treated as ordinary cash income, same as `bonus` — not as a `perk` (perks model non-cash benefits-in-kind and don't add to net cash). This is a steady-state average, not a payment schedule — it ignores exactly when in the year installments land. `start_date` is informational only (shown in the report as the vesting window) and isn't used in any calculation.
4. To see just your own current package's tax/comp breakdown — no offer file needed — run `uv run python scripts/salary_compare.py current --project "$CLAUDE_PROJECT_DIR"`. This is the deterministic, zero-token path for "what's my federal/FICA/state/local tax and net cash right now": every number comes from `scripts/salary_compare.py` and `scripts/data/tax_data_2026.json`, never from you doing mental math. Nothing this computes is ever written back into `salary_config.json` — the config holds only the raw facts (base, bonus %, perks, retention award totals, location); taxes and totals are recomputed fresh on every run so they can never go stale relative to the current tax table. What genuinely can't be computed and needs research instead: `location.cost_of_living_index` (needs a real source — BEA RPP/Numbeo/BestPlaces, see Step 5) and `market.*` pay percentiles (Step 4). Both are placeholders in a fresh config until you (or this skill) fill them in with sourced numbers.

## Step 2: The offer

Create `data/output/<Company_Name>/offer_<Company>_<Role>_<YYYY-MM-DD>.json` from `config/offer.example.json`. Fill in what the user knows (base, bonus %, sign-on, relocation, 401k match, premiums, PTO, perks, location). If the offer has not arrived yet, use the posted band or the recruiter's verbal number, say the file is provisional, and plan to re-run when the written offer lands. Unknown one-time costs (unreimbursed relocation, forfeited bonus, unvested benefits left behind) go in `one_time_costs`; ask about them.

If the user states their own walk-away number for this role, record it as the top-level `user_walkaway_base` field (not inside `offer`) — never infer or suggest one yourself. This is their personal decision, separate from the script's calculated break-even floor, and may be lower than it (they may have already weighed non-comp factors — role, program, retention-award timing — and decided that's still worth accepting). The report always shows both, clearly labeled, and the gap between them; it never silently replaces one with the other.

## Step 3: Verify tax data

`tax_data_2026.json` ships with `"verified": false`. For every state involved (current and offer), and once for federal/FICA per calendar year:

1. WebSearch the state revenue department (or a reputable tax-foundation table) for the current-year individual income tax rate/brackets and local wage taxes for the specific city (e.g. Michigan cities such as Detroit levy income tax; Milford does not; Ohio municipalities do).
2. If the file disagrees or the state is missing, edit that state's entry and add `"source"` and `"verified_on"` fields. Put city/school-district taxes in `location.local_tax_pct`.
3. Once federal/FICA and both states are checked, set top-level `verified: true` and `as_of` to today's date.

## Step 4: Market research

Goal: fill `market` in the offer file with real, sourced numbers.

- **Query job-hunter's own job database first** — it already holds real, currently (or recently) open postings, many with a disclosed salary range extracted by `salary.py`'s evidence-based matcher, which beats generic occupation-wide percentiles for finding true company/role peers:
  ```bash
  uv run python scripts/market_lookup.py --title-like "Autonomous Vehicle,ADAS,Autonomy,Validation" --project "$CLAUDE_PROJECT_DIR"
  uv run python scripts/market_lookup.py --title-like "..." --companies gm,ford,rivian,caterpillar --states MI,AZ --project "$CLAUDE_PROJECT_DIR"
  uv run python scripts/market_lookup.py --title-like "..." --include-closed --json --project "$CLAUDE_PROJECT_DIR"
  ```
  This is a **read-only, deterministic lookup** (stdlib + sqlite3, matches `diff_profile.py`'s genuine-read-only connection pattern) — it returns raw candidate rows (company, title, location, status, salary range, sponsorship stance, URL), never an opinion. **Curating which rows are genuine peers is your job, not the script's**: bucket by seniority (a "Sr."/"Staff"/"Manager" title is a level-up reference, not a same-level peer — useful for justifying a higher-grade ask, not as a direct comparable), note metro differences (a Bay Area number isn't comparable to a Michigan one without COL context), and prefer the *same company, different location* case when it exists (the strongest, least-disputable comparable — e.g. an employer's own posted band for the identical title varying by site). `--include-no-salary` surfaces title-matched rows with no disclosed range for context only, never as a comparable number. Cite each row you actually use (company, title, location, URL) the same as any other source.
- **Cross-check with O*NET/BLS OEWS** for the user's SOC code (`soc_code` in `salary_config.json`; e.g. `https://www.onetonline.org/link/localwages/17-2141.02?zip=<zip>`): use the state wage percentiles (p25/p50/p75/p90) for `market`. Also cross-check: the JD pay range, Levels.fyi, Glassdoor, Indeed, Salary.com, and the **US DOL H-1B/PERM LCA disclosure data** for the same employer and title (actual employer-filed wages).
- Record `range_min`/`range_max` (posted band), `p25`/`p50`/`p75` (only where a source gives them), and `sources` with URL and retrieval date — a `market_lookup.py` row's `canonical_url` counts as a source. Also note typical bonus % and 401k match at that employer if found.
- Do not invent percentiles. If only a band exists, leave the percentiles `null` and say so; the script then uses the floor from break-even. Name the seniority level you assumed and why.
- Compare like with like: same level, same metro, base separate from total comp.

## Step 5: Standard of living: city and state

**Use where the user will live, not where the job is.** If they keep their current home, both `location` blocks describe the same residence (index ratio 1) and only the commute and car costs change; say so and skip the city comparison. If relocating, compare both places.

Try the calculators the user prefers (NerdWallet, Bankrate, Forbes Advisor, Numbeo comparison page, We Are Calculator). They are interactive and mostly metro-level, so WebFetch often gets no result: drive them with the Playwright MCP if this runtime has it configured, otherwise fall back to BEA RPP, BestPlaces and rent listings, and say which you used. For both locations gather and cite: a cost-of-living index (BEA Regional Price Parities is the most defensible; Numbeo/BestPlaces/Payscale as cross-checks, 100 = US average), median rent for the unit type the user would actually rent (Zillow/Apartments.com), property tax and sales tax if relevant, auto insurance level (matters in Michigan), commute time, and healthcare or climate points that matter to them. Put the index and typical housing cost into each `location` block; the script treats housing as informational and applies the index to spending power.

Write a short qualitative city/state comparison alongside the script's numbers (housing, taxes, insurance, weather, airport/family access, industry job density for the user's field).

## Step 6: Run the comparison

```bash
uv run python scripts/salary_compare.py compare --offer data/output/<Company>/offer_<Company>_<Role>_<date>.json --project "$CLAUDE_PROJECT_DIR"
# save the round's report as markdown AND update the job's running CSV ledger (always do both):
uv run python scripts/salary_compare.py compare --offer <file> \
  --out data/output/<Company_Name>/comparison_<YYYY-MM-DD>.md \
  --csv-out data/output/<Company_Name>/comparison_<Company>_<Role>.csv \
  --csv-column "Offer round 1 ($125,000, Grade 6A)" \
  --project "$CLAUDE_PROJECT_DIR"
# a what-if "ask" (e.g. what the user is about to counter with) gets its own ledger column too --
# base only, on the SAME offer file, via --offer-base (note: quote --csv-column with single
# quotes in a shell command so a literal $ in the label isn't read as shell variable expansion):
uv run python scripts/salary_compare.py compare --offer <file> --offer-base 170000 \
  --csv-out data/output/<Company_Name>/comparison_<Company>_<Role>.csv \
  --csv-column 'My ask ($170,000, Grade 7/8)' \
  --project "$CLAUDE_PROJECT_DIR"
# a range instead of one number:
uv run python scripts/salary_compare.py compare --offer <file> --sweep 110000:140000:5000 --project "$CLAUDE_PROJECT_DIR"
# --json for machine-readable output, --filing mfj to override filing status
```

The ledger CSV (`Metric, Current, <round/ask label>, ...`) is unformatted numbers only — no `$`/`%` text — so it opens directly in a spreadsheet for a side-by-side read, sort, or chart across every round and what-if at once. It's a mechanical export of the same `compute()` output the `.md` report renders, never a second source of truth. `--csv-column` labels each run's column; omit it and the script defaults to `Ask $<offer-base>` (when `--offer-base` is given) or `Offer (<today>)` — but an explicit label naming the round/grade/context is more useful once the ledger has several columns, so prefer passing one. Re-using an existing label overwrites that column in place (e.g. correcting a mistake) rather than duplicating it; `Current` always refreshes to the latest `config/salary_config.json`, so a correction there (like a wrong 401k match %) updates every column's `Current` value on the next run without touching prior rounds' offer/ask columns.

Report the script's tables faithfully. Then interpret: is the offer above or below break-even, how much of any gap is the vehicle/lunch/bonus the user would lose, what state tax and cost of living contribute, and the year-1 picture including sign-on and one-time costs. State the assumptions that move the answer most (bonus payout %, perk valuation, taxable vehicle, COL index) and show a `--sweep` or re-run with the alternative when one assumption could flip the conclusion. Point out anything the model does not capture (equity vesting, promotion path, job security, pension, commute cost, health plan quality).

## Step 7: Negotiation script

Numbers come from the script's "Negotiation numbers" table: **walk-away floor**, **target**, **stretch**, **opening anchor**, value of +$1k base, and sign-on needed to bridge year 1. Build the plan file `data/output/<Company_Name>/negotiation-plan_<YYYY-MM-DD>.md` with:

1. **Position summary**: three lines: offer vs market, offer vs current on an adjusted basis, your ask.
2. **Numbers**: floor / target / stretch / anchor, plus the non-base ask (sign-on, bonus %, relocation, equity, level, PTO, start date, review-at-6-months) ranked in the order to concede them.
3. **Phone script**: opening (gratitude and enthusiasm, Carnegie-style — thank first, frame the ask as making the role work for both sides), the ask with a market-based justification, then stop talking (silence does real work here — don't fill it). Write pushback responses as *Never Split the Difference* (Chris Voss) tactics, not flat declarations:
   - **Calibrated questions** — start with "How" or "What", never "Why" (reads as accusatory): "How am I supposed to make that work?" / "What would need to be true to get to $X?" — puts the problem back on them instead of a demand they can just reject.
   - **Labeling** — name their likely constraint before they have to: "It sounds like this req has a fixed band" / "It seems like level is the real lever here." Confirms or corrects your read either way, and defuses resistance before it hardens.
   - **Mirroring** — repeat the last 2-3 words of what they just said, as a question, to get them talking instead of you filling the silence with your next point.
   - **"That's right," not "you're right"** — summarize their position back accurately until they confirm it; "that's right" is a real agreement signal, "you're right" is just placation, so don't settle for it.
   - **A "no"-oriented opening** for a sensitive ask is often answered more honestly than a "yes"-oriented one: "Would it be unreasonable to revisit the base given the market data?" instead of "Can we revisit the base?"
   - Still cover the standard pushbacks ("that's the top of the band", "we don't negotiate", "what are you making now?", "what number would you need to accept?", "we need an answer today", "can you send competing offers?") and how to bridge if the budget is genuinely capped — phrase them as calibrated questions/labels above wherever it fits naturally, not just flat lines.
4. **Email version** of the ask (short, warm, one specific number, cites market data, ends with a question). Keep this in Carnegie's simpler warm/direct style — Voss's live-conversation tactics (mirroring, labeling, calibrated questions) need real back-and-forth to work and don't translate to a one-shot written message.
5. **Decision rule**: accept / counter / walk thresholds and what deadline to request. If this is a second-or-later round of the employer countering your ask (Step 9), apply Voss's **Ackerman model** rather than splitting the difference in even steps: concede in decreasing increments (e.g. roughly 100% / 80% / 60% / 20% of the remaining gap) and land on a deliberately non-round final number (e.g. $172,750, not $173,000) so the last offer reads as carefully calculated, not arbitrary.

Rules for the script text:
- Anchor on market data and the role's scope, not on the user's personal expenses or current pay. The user's own current comp is theirs to disclose or not; default to "I'd like to focus on the value I'd bring to this role" and never lie about it.
- Never invent competing offers, deadlines, or facts. Only reference an alternative if the user actually has one.
- Ask for one number, not a range starting at the floor. Negotiate the whole package, not just base.
- Keep the tone collaborative (Dale Carnegie style, same as `outreach-writer`): thank first, state the goal to make it work, ask for help solving the gap. Layer *Never Split the Difference* tactics (calibrated questions, labeling, mirroring, "that's right," no-oriented openings, the Ackerman model) on top of that warmth for the live phone conversation and any real-time pushback — Carnegie sets the tone, Voss supplies the tactics for the actual back-and-forth.
- Prefer a written offer before negotiating final numbers; ask for the benefits summary (401k match/vesting, premiums, bonus plan document, relocation terms).

## Step 8: Wrap-up

Tell the user which assumptions in the config are still estimates, what is unverified (`verified` flag in the tax file), and what to re-run when the written offer arrives. If SKILL.md, the script, or the tax data change behavior, bump `version` in the frontmatter.

## Step 9: A new round (counter-offer, or their response to your ask)

A negotiation is rarely one shot: the user makes an ask (Step 7's numbers), the employer comes back with something new. Handle that as its own round, not a from-scratch re-run:

1. **Never overwrite a previous round's offer file or `.md` report** — each round is real history. Name round 2+ `offer_<Company>_<Role>_round<N>_<YYYY-MM-DD>.json` (round 1 keeps the plain `offer_<Company>_<Role>_<YYYY-MM-DD>.json` name already established) and `comparison_round<N>_<YYYY-MM-DD>.md` / `negotiation-plan_round<N>_<YYYY-MM-DD>.md` to match. The **`.csv` ledger is the one exception** — it's a single non-dated file per job (`comparison_<Company>_<Role>.csv`) that every round updates in place by adding its own column; never create a second dated `.csv`.
2. **Record what was already asked for** in the new offer file's `_readme`/`_note` (e.g. "user countered at $170k base, Grade 7/8, per round 1's plan") — pull this from the previous round's negotiation-plan file (its "Position summary"/"Ask ladder" sections) rather than asking the user to repeat it, unless it's genuinely missing from the record.
3. Re-run Step 6 (`compare`, with `--out` pointing at the new round's dated `.md` and `--csv-out` pointing at the *same* ledger `.csv` as every prior round, with a fresh `--csv-column` label for this round), then rewrite the negotiation plan (Step 7) for this round — but frame it against the *previous* round's ask, not in a vacuum: what moved (base, level/grade, sign-on), what didn't, and whether the remaining gap changes the ask ladder or the accept/counter/walk decision. If the employer's move fell short of what would close the gap, say so plainly rather than re-running the same script unchanged.
4. If nothing about the user's own package changed since the last round (no raise, no new offer elsewhere), `current`'s numbers are already correct — no need to re-verify Steps 1/3/5 again for this round; only Steps 2/4/6/7 repeat. If something *did* change (e.g. a config correction), the ledger's `Current` column reflects it automatically on the next `--csv-out` run — no separate fix-up step needed.
