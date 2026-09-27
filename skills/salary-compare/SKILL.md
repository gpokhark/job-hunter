---
name: salary-compare
version: 1.0.0
description: Compare a job offer against the user's current total compensation (base, bonus, 401k match, perks, premiums, taxes, cost of living), research the market rate, compare city/state standard of living and state income tax, and write a negotiation script and email. Use whenever the user mentions a job offer, an upcoming offer, comparing salaries, "should I take this offer", counter-offer, salary negotiation, or total compensation for a specific company and role. All arithmetic comes from scripts/salary_compare.py, never from mental math.
compatibility: Requires uv and Python 3.11+. scripts/salary_compare.py is stdlib only — no extra installs. Market pay and cost-of-living research use WebSearch/WebFetch (or Playwright MCP for interactive calculators, if this runtime has it configured — not required).
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
- `offer_<Company>_<Role>_<YYYY-MM-DD>.json` — the filled-in offer + market data
- `comparison_<YYYY-MM-DD>.md` — the script's full comparison table
- `negotiation-plan_<YYYY-MM-DD>.md` — the negotiation script/email deliverable

## Files

| Path | Tracked | Purpose |
|---|---|---|
| `config/salary_config.example.json` | yes | Template for the user's current package |
| `config/salary_config.json` | **gitignored** | User's real current package (`salary_compare.py init` creates it) |
| `config/offer.example.json` | yes | Template for one offer + market data |
| `data/output/<Company_Name>/offer_<Company>_<Role>_<YYYY-MM-DD>.json` | gitignored (`data/*`) | One file per real offer |
| `scripts/salary_compare.py` | yes | Deterministic calculator (stdlib only) |
| `scripts/data/tax_data_2026.json` | yes | Federal/FICA/state tax tables (with `verified` flag) |
| `tests/test_salary_compare.py` | yes | `uv run pytest tests/test_salary_compare.py` |
| `data/output/<Company_Name>/comparison_<YYYY-MM-DD>.md` | gitignored (`data/*`) | Script's comparison report |
| `data/output/<Company_Name>/negotiation-plan_<YYYY-MM-DD>.md` | gitignored (`data/*`) | Final deliverable (always markdown) |

Never put real compensation numbers in a tracked file. Personal data goes only in the gitignored files above. Every command below takes `--project "$CLAUDE_PROJECT_DIR"` (Claude Code) — or the equivalent workspace path for another runtime such as Hermes.

## Step 1: Current package

1. If `config/salary_config.json` is missing, run `uv run python scripts/salary_compare.py init --project "$CLAUDE_PROJECT_DIR"`.
2. Read it. Any key marked placeholder or `_note: ... confirm` must be confirmed with the user before the comparison is trusted. Ask for everything missing in one message, not one question at a time. Typical gaps: city, monthly housing cost, whether the employer vehicle is a taxable fringe benefit, what an equivalent insurance policy would cost, PTO days, how much they contribute to the 401k.
3. Schema notes: `bonus.expected_payout_pct_of_target` is what the bonus historically pays (100 = target); perks accept `annual_value`, `monthly_value` or `per_workday_value`; `employee_costs` are what the user pays (premiums), `pretax: true` for payroll-deducted health premiums; `retirement.match_cap_pct` is the % of pay the employer matches up to.

## Step 2: The offer

Create `data/output/<Company_Name>/offer_<Company>_<Role>_<YYYY-MM-DD>.json` from `config/offer.example.json`. Fill in what the user knows (base, bonus %, sign-on, relocation, 401k match, premiums, PTO, perks, location). If the offer has not arrived yet, use the posted band or the recruiter's verbal number, say the file is provisional, and plan to re-run when the written offer lands. Unknown one-time costs (unreimbursed relocation, forfeited bonus, unvested benefits left behind) go in `one_time_costs`; ask about them.

## Step 3: Verify tax data

`tax_data_2026.json` ships with `"verified": false`. For every state involved (current and offer), and once for federal/FICA per calendar year:

1. WebSearch the state revenue department (or a reputable tax-foundation table) for the current-year individual income tax rate/brackets and local wage taxes for the specific city (e.g. Michigan cities such as Detroit levy income tax; Milford does not; Ohio municipalities do).
2. If the file disagrees or the state is missing, edit that state's entry and add `"source"` and `"verified_on"` fields. Put city/school-district taxes in `location.local_tax_pct`.
3. Once federal/FICA and both states are checked, set top-level `verified: true` and `as_of` to today's date.

## Step 4: Market research

Goal: fill `market` in the offer file with real, sourced numbers.

- **Start with O*NET/BLS OEWS** for the user's SOC code (`soc_code` in `salary_config.json`; e.g. `https://www.onetonline.org/link/localwages/17-2141.02?zip=<zip>`): use the state wage percentiles (p25/p50/p75/p90) for `market`. Then cross-check with: the JD pay range, Levels.fyi, Glassdoor, Indeed, Salary.com, BLS OEWS for the occupation and metro, and the **US DOL H-1B/PERM LCA disclosure data** for the same employer and title (actual employer-filed wages).
- Record `range_min`/`range_max` (posted band), `p25`/`p50`/`p75` (only where a source gives them), and `sources` with URL and retrieval date. Also note typical bonus % and 401k match at that employer if found.
- Do not invent percentiles. If only a band exists, leave the percentiles `null` and say so; the script then uses the floor from break-even. Name the seniority level you assumed and why.
- Compare like with like: same level, same metro, base separate from total comp.

## Step 5: Standard of living: city and state

**Use where the user will live, not where the job is.** If they keep their current home, both `location` blocks describe the same residence (index ratio 1) and only the commute and car costs change; say so and skip the city comparison. If relocating, compare both places.

Try the calculators the user prefers (NerdWallet, Bankrate, Forbes Advisor, Numbeo comparison page, We Are Calculator). They are interactive and mostly metro-level, so WebFetch often gets no result: drive them with the Playwright MCP if this runtime has it configured, otherwise fall back to BEA RPP, BestPlaces and rent listings, and say which you used. For both locations gather and cite: a cost-of-living index (BEA Regional Price Parities is the most defensible; Numbeo/BestPlaces/Payscale as cross-checks, 100 = US average), median rent for the unit type the user would actually rent (Zillow/Apartments.com), property tax and sales tax if relevant, auto insurance level (matters in Michigan), commute time, and healthcare or climate points that matter to them. Put the index and typical housing cost into each `location` block; the script treats housing as informational and applies the index to spending power.

Write a short qualitative city/state comparison alongside the script's numbers (housing, taxes, insurance, weather, airport/family access, industry job density for the user's field).

## Step 6: Run the comparison

```bash
uv run python scripts/salary_compare.py compare --offer data/output/<Company>/offer_<Company>_<Role>_<date>.json --project "$CLAUDE_PROJECT_DIR"
# save the report as markdown:
uv run python scripts/salary_compare.py compare --offer <file> --out data/output/<Company_Name>/comparison_<YYYY-MM-DD>.md --project "$CLAUDE_PROJECT_DIR"
# what-if on a specific number, or a range:
uv run python scripts/salary_compare.py compare --offer <file> --offer-base 125000 --project "$CLAUDE_PROJECT_DIR"
uv run python scripts/salary_compare.py compare --offer <file> --sweep 110000:140000:5000 --project "$CLAUDE_PROJECT_DIR"
# --json for machine-readable output, --filing mfj to override filing status
```

Report the script's tables faithfully. Then interpret: is the offer above or below break-even, how much of any gap is the vehicle/lunch/bonus the user would lose, what state tax and cost of living contribute, and the year-1 picture including sign-on and one-time costs. State the assumptions that move the answer most (bonus payout %, perk valuation, taxable vehicle, COL index) and show a `--sweep` or re-run with the alternative when one assumption could flip the conclusion. Point out anything the model does not capture (equity vesting, promotion path, job security, pension, commute cost, health plan quality).

## Step 7: Negotiation script

Numbers come from the script's "Negotiation numbers" table: **walk-away floor**, **target**, **stretch**, **opening anchor**, value of +$1k base, and sign-on needed to bridge year 1. Build the plan file `data/output/<Company_Name>/negotiation-plan_<YYYY-MM-DD>.md` with:

1. **Position summary**: three lines: offer vs market, offer vs current on an adjusted basis, your ask.
2. **Numbers**: floor / target / stretch / anchor, plus the non-base ask (sign-on, bonus %, relocation, equity, level, PTO, start date, review-at-6-months) ranked in the order to concede them.
3. **Phone script**: opening (gratitude and enthusiasm), the ask with a market-based justification, then stop talking; responses for the likely pushbacks ("that's the top of the band", "we don't negotiate", "what are you making now?", "what number would you need to accept?", "we need an answer today", "can you send competing offers?"), and how to bridge if the budget is genuinely capped.
4. **Email version** of the ask (short, warm, one specific number, cites market data, ends with a question).
5. **Decision rule**: accept / counter / walk thresholds and what deadline to request.

Rules for the script text:
- Anchor on market data and the role's scope, not on the user's personal expenses or current pay. The user's own current comp is theirs to disclose or not; default to "I'd like to focus on the value I'd bring to this role" and never lie about it.
- Never invent competing offers, deadlines, or facts. Only reference an alternative if the user actually has one.
- Ask for one number, not a range starting at the floor. Negotiate the whole package, not just base.
- Keep the tone collaborative (Dale Carnegie style, same as `outreach-writer`): thank first, state the goal to make it work, ask for help solving the gap.
- Prefer a written offer before negotiating final numbers; ask for the benefits summary (401k match/vesting, premiums, bonus plan document, relocation terms).

## Step 8: Wrap-up

Tell the user which assumptions in the config are still estimates, what is unverified (`verified` flag in the tax file), and what to re-run when the written offer arrives. If SKILL.md, the script, or the tax data change behavior, bump `version` in the frontmatter.
