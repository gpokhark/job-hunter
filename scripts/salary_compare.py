#!/usr/bin/env python3
"""Deterministic salary / total-compensation comparison (stdlib only).

Same inputs always give the same numbers; every judgement call (market research,
narrative, negotiation wording) is left to the salary-compare skill.

Usage:
    uv run python scripts/salary_compare.py init
    uv run python scripts/salary_compare.py current                    # your own tax/comp breakdown, no offer needed
    uv run python scripts/salary_compare.py compare --offer data/output/Acme/offer_Acme_SWE_2026-09-27.json
    uv run python scripts/salary_compare.py compare --offer X.json --offer-base 118000
    uv run python scripts/salary_compare.py compare --offer X.json --sweep 100000:130000:5000
    uv run python scripts/salary_compare.py compare --offer X.json --json
    uv run python scripts/salary_compare.py compare --offer X.json --out data/output/<Company>/comparison_<date>.md
    # one running ledger CSV per job -- every round/what-if adds or replaces its own column,
    # Current is refreshed each time; never a fresh file per run:
    uv run python scripts/salary_compare.py compare --offer X.json \
        --csv-out data/output/<Company>/comparison_<Company>_<Role>.csv --csv-column "Offer round 1"
    uv run python scripts/salary_compare.py compare --offer X.json --offer-base 170000 \
        --csv-out data/output/<Company>/comparison_<Company>_<Role>.csv --csv-column "My ask 170000"

Every subcommand also takes --project (falls back to $JOB_HUNTER_ROOT, then cwd) so it can be run
from any directory, same as the job-hunter CLI and every other scripts/*.py entry point.

Model (per year):
    gross cash   = base + expected bonus + retention/sign-on bonuses (annualized) + equity
    net cash     = gross - 401k contribution - employee costs - federal/FICA/state/local tax
    net value    = net cash + perks + 401k (own contribution + employer match)
    COL-adjusted = (net cash + perks) * current_index / location_index + 401k
    break-even   = offer base at which COL-adjusted net value equals the current job's

401k: both the employee's own contribution and the employer match apply to the same "eligible
compensation" -- base only, unless retirement.match_on_bonus is true (then base + bonus +
retention). Contributing/matching on gross cash (bonus, retention bonus included) is NOT the
default; set match_on_bonus explicitly if that's how a real plan actually works.

`retention_bonuses` (a fixed-dollar award vesting over several years, e.g. a new-hire or
performance retention bonus paid out in prorated installments) is annualized as
`total_amount / vesting_years` and treated as ordinary cash income, same as `bonus` — unlike
`perks`, which model non-cash benefits-in-kind. This is a steady-state average, not a payment
schedule: it ignores exactly when in the year installments land.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from job_hunter.rootutil import add_project_argument, chdir_to_project_root

TAX_DATA = Path("scripts/data/tax_data_2026.json")
DEFAULT_CONFIG = Path("config/salary_config.json")
EXAMPLE_CONFIG = Path("config/salary_config.example.json")


# --------------------------------------------------------------------------- io

def load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        sys.exit(f"error: {path} not found")
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path} is not valid JSON ({e})")


def bracket_tax(taxable: float, brackets: list) -> float:
    tax, lower = 0.0, 0.0
    for upper, rate in brackets:
        top = float("inf") if upper is None else upper
        if taxable > lower:
            tax += (min(taxable, top) - lower) * rate
        lower = top
    return tax


# --------------------------------------------------------------------------- tax

def federal_tax(wages: float, filing: str, data: dict) -> float:
    f = data["federal"][filing]
    return bracket_tax(max(0.0, wages - f["standard_deduction"]), f["brackets"])


def fica_tax(wages: float, filing: str, data: dict) -> float:
    c = data["fica"]
    tax = min(wages, c["ss_wage_base"]) * c["ss_rate"] + wages * c["medicare_rate"]
    tax += max(0.0, wages - c["addl_medicare_threshold"][filing]) * c["addl_medicare_rate"]
    return tax


def state_tax(wages: float, loc: dict, filing: str, data: dict) -> float:
    if loc.get("state_tax_rate_pct") is not None:
        return wages * loc["state_tax_rate_pct"] / 100
    st = data["states"].get(loc["state"].upper())
    if st is None:
        sys.exit(
            f"error: no tax data for state {loc['state']!r}. Add it to {TAX_DATA.name} "
            "or set location.state_tax_rate_pct in the config."
        )
    if st["type"] == "none":
        return 0.0
    if st["type"] == "flat":
        return wages * st["rate"]
    mult = st.get("mfj_multiplier", 2) if filing == "mfj" else 1
    brackets = [[None if u is None else u * mult, r] for u, r in st["brackets_single"]]
    return bracket_tax(max(0.0, wages - st["std_deduction"][filing]), brackets)


def state_note(loc: dict, data: dict) -> str:
    if loc.get("state_tax_rate_pct") is not None:
        return "config override"
    st = data["states"].get(loc["state"].upper(), {})
    bits = [st.get("note", "")]
    if st.get("approximate"):
        bits.append("approximate")
    return "; ".join(b for b in bits if b)


# ----------------------------------------------------------------------- package

def perk_annual(p: dict) -> float:
    if "annual_value" in p:
        return float(p["annual_value"])
    if "monthly_value" in p:
        return float(p["monthly_value"]) * 12
    if "per_workday_value" in p:
        return float(p["per_workday_value"]) * float(p.get("workdays_per_year", 230))
    raise ValueError(f"perk {p.get('name')!r} needs annual_value, monthly_value or per_workday_value")


def cost_annual(c: dict) -> float:
    return float(c["annual_cost"]) if "annual_cost" in c else float(c["monthly_cost"]) * 12


def retention_annual(rb: dict) -> float:
    return float(rb["total_amount"]) / float(rb.get("vesting_years", 3))


def vesting_end(start_date: str | None, vesting_years: float) -> str | None:
    """Rough end-of-vesting date (start date's year + vesting_years, same month/day) for
    footnoting a retention bonus as time-limited. Not used in any calculation."""
    if not start_date:
        return None
    try:
        y, mo, d = (int(x) for x in start_date.split("-"))
        target_year = y + int(vesting_years)
        try:
            return date(target_year, mo, d).isoformat()
        except ValueError:  # e.g. Feb 29 on a non-leap target year
            return date(target_year, mo, d - 1).isoformat()
    except (ValueError, TypeError):
        return None


def compute(pkg: dict, filing: str, data: dict, *, sign_on: float = 0.0) -> dict:
    """Annual after-tax picture of one package. `sign_on` is extra one-time taxable cash."""
    base = float(pkg["base_salary"])
    b = pkg.get("bonus", {})
    bonus = base * b.get("target_pct", 0) / 100 * b.get("expected_payout_pct_of_target", 100) / 100
    equity = float(pkg.get("annual_equity_value", 0))
    retention = [
        {
            "name": rb.get("name", ""),
            "total_amount": float(rb["total_amount"]),
            "vesting_years": float(rb.get("vesting_years", 3)),
            "start_date": rb.get("start_date"),
            "annual_value": retention_annual(rb),
        }
        for rb in pkg.get("retention_bonuses", [])
    ]
    retention_total = sum(rb["annual_value"] for rb in retention)
    gross = base + bonus + retention_total + equity + sign_on

    r = pkg.get("retirement", {})
    # elig is both the match-eligible base AND the employee's own contribution base -- a plan's
    # payroll deferral % and its match % apply to the same "eligible compensation" definition, so
    # match_on_bonus (does eligible pay include bonus/retention, or just base) governs both.
    elig = base + ((bonus + retention_total) if r.get("match_on_bonus") else 0.0)
    contrib_pct = r.get("employee_contribution_pct", 0)
    contrib = min(elig * contrib_pct / 100, data["k401_employee_limit"])
    contrib_pct_eff = contrib / elig * 100 if elig else 0.0
    match = min(contrib_pct_eff, r.get("match_cap_pct", 0)) / 100 * r.get("match_rate", 0) * elig

    perks = [(p["name"], perk_annual(p), bool(p.get("taxable"))) for p in pkg.get("perks", [])]
    perk_total = sum(v for _, v, _ in perks)
    perk_taxable = sum(v for _, v, t in perks if t)

    costs = [(c["name"], cost_annual(c), bool(c.get("pretax", True))) for c in pkg.get("employee_costs", [])]
    cost_total = sum(v for _, v, _ in costs)
    cost_pretax = sum(v for _, v, p in costs if p)

    fed_wages = gross - contrib - cost_pretax + perk_taxable
    fica_wages = gross - cost_pretax + perk_taxable
    loc = pkg["location"]
    fed = federal_tax(fed_wages, filing, data)
    fica = fica_tax(fica_wages, filing, data)
    st = state_tax(fed_wages, loc, filing, data)
    local = fed_wages * loc.get("local_tax_pct", 0) / 100
    taxes = fed + fica + st + local

    net_cash = gross - contrib - cost_total - taxes
    retirement = contrib + match
    net_value = net_cash + perk_total + retirement
    idx = float(loc.get("cost_of_living_index", 100))
    return {
        "label": pkg.get("label", ""),
        "location": f"{loc['city']}, {loc['state']}",
        "col_index": idx,
        "base": base, "bonus": bonus, "equity": equity, "sign_on": sign_on,
        "retention_bonuses": retention, "retention_bonus_total": retention_total,
        "gross_cash": gross,
        "employer_401k_match": match, "employee_401k": contrib, "employee_401k_pct_effective": contrib_pct_eff,
        "perks": [{"name": n, "annual_value": v, "taxable": t} for n, v, t in perks], "perk_total": perk_total,
        "employee_costs": [{"name": n, "annual_cost": v} for n, v, _ in costs], "employee_cost_total": cost_total,
        "federal_tax": fed, "fica": fica, "state_tax": st, "local_tax": local, "total_tax": taxes,
        "effective_tax_rate": taxes / gross if gross else 0.0,
        "net_cash": net_cash, "net_cash_monthly": net_cash / 12,
        "retirement_total": retirement,
        "total_comp_pretax": gross - sign_on + match + perk_total,
        "net_value": net_value,
        "monthly_housing": float(loc.get("monthly_housing_cost", 0)),
        "pto_days": pkg.get("pto_days"),
    }


def col_adjusted(r: dict, ref_index: float) -> float:
    return (r["net_cash"] + r["perk_total"]) * ref_index / r["col_index"] + r["retirement_total"]


def solve(fn, target: float, lo: float, hi: float) -> float | None:
    """Smallest x in [lo, hi] with fn(x) >= target, fn monotonic increasing; None if unreachable."""
    if fn(hi) < target:
        return None
    if fn(lo) >= target:
        return lo
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if fn(mid) >= target else (mid, hi)
    return hi


def round_up(x: float, step: int) -> int:
    return int(-(-x // step) * step)


# ------------------------------------------------------------------- comparison

def build_report(cfg: dict, offer_doc: dict, filing: str, data: dict, offer_base: float | None) -> dict:
    cur_pkg = cfg["current"]
    off_pkg = copy.deepcopy(offer_doc["offer"])
    if offer_base is not None:
        off_pkg["base_salary"] = offer_base
    sign_on = float(off_pkg.get("sign_on_bonus", 0))
    market = offer_doc.get("market") or {}
    one_time = sum(float(o["amount"]) for o in offer_doc.get("one_time_costs", []))

    cur = compute(cur_pkg, filing, data)
    off = compute(off_pkg, filing, data)
    ref = cur["col_index"]
    cur_adj, off_adj = col_adjusted(cur, ref), col_adjusted(off, ref)

    def with_base(x: float, extra_sign_on: float = 0.0) -> dict:
        p = copy.deepcopy(off_pkg)
        p["base_salary"] = x
        return compute(p, filing, data, sign_on=extra_sign_on)

    def adj_at(x: float) -> float:
        return col_adjusted(with_base(x), ref)

    break_even = solve(adj_at, cur_adj, 30000, 800000)

    # Year-1: sign-on is taxed as extra wages, one-time costs are paid out of pocket.
    y1 = compute(off_pkg, filing, data, sign_on=sign_on)
    y1_adj = col_adjusted(y1, ref) - one_time

    def sign_on_needed_for(base: float) -> float | None:
        def f(s: float) -> float:
            return col_adjusted(with_base(base, s), ref) - one_time
        return solve(f, cur_adj, 0, 400000)

    # Negotiation targets (see SKILL.md for how each number is meant to be used).
    p50, p75, rmax = market.get("p50"), market.get("p75"), market.get("range_max")
    floor = round_up(break_even, 500) if break_even else None
    target = round_up(max(x for x in (floor, p50) if x), 1000) if (floor or p50) else None
    stretch = round_up(max(x for x in (floor and floor * 1.04, p75) if x), 1000) if (floor or p75) else None
    anchor = round_up(stretch * 1.05, 1000) if stretch else None
    capped = anchor is not None and rmax is not None and anchor > rmax
    band_below_parity = floor is not None and rmax is not None and rmax < floor
    ceiling_base = min(anchor, rmax) if capped else anchor

    # A user-stated walk-away (offer_doc's "user_walkaway_base") is a personal decision, not a
    # calculated one -- it may sit below the break-even floor if the user has already weighed
    # non-comp factors (role, program, timing) and decided that's still worth it. Never silently
    # replace the calculated floor with it; report both, and the gap between them, so it's clear
    # which number is "makes you financially whole" and which is "what you'll actually accept".
    user_walkaway_base = offer_doc.get("user_walkaway_base")
    user_walkaway_adjusted = adj_at(user_walkaway_base) if user_walkaway_base is not None else None
    user_walkaway_gap_vs_current = (
        cur_adj - user_walkaway_adjusted if user_walkaway_adjusted is not None else None
    )
    user_walkaway_gap_vs_calculated_floor = (
        floor - user_walkaway_base if (floor is not None and user_walkaway_base is not None) else None
    )

    neg = {
        "walk_away_floor_base": floor,
        "target_base": target,
        "stretch_base": stretch,
        "opening_anchor_base": ceiling_base,
        "anchor_capped_at_band_max": capped,
        "band_max_below_break_even": band_below_parity,
        "sign_on_to_bridge_year1_at_offer_base": sign_on_needed_for(off_pkg["base_salary"]),
        "sign_on_to_bridge_year1_at_target_base": sign_on_needed_for(target) if target else None,
        "base_gap_to_target": (target - off_pkg["base_salary"]) if target else None,
        "value_of_1k_base_net_annual": with_base(off_pkg["base_salary"] + 1000)["net_cash"] - off["net_cash"],
        "percentile_of_offer_in_market": _percentile(off_pkg["base_salary"], market),
        "user_walkaway_base": user_walkaway_base,
        "user_walkaway_adjusted": user_walkaway_adjusted,
        "user_walkaway_gap_vs_current": user_walkaway_gap_vs_current,
        "user_walkaway_gap_vs_calculated_floor": user_walkaway_gap_vs_calculated_floor,
    }

    # State tax on the same wages in both places, plus who pays what.
    same_wages = cur["gross_cash"] - cur["employee_401k"]
    state_cmp = {
        "wages_used": same_wages,
        "current_state": cur_pkg["location"]["state"],
        "offer_state": off_pkg["location"]["state"],
        "current_state_tax": state_tax(same_wages, cur_pkg["location"], filing, data)
        + same_wages * cur_pkg["location"].get("local_tax_pct", 0) / 100,
        "offer_state_tax": state_tax(same_wages, off_pkg["location"], filing, data)
        + same_wages * off_pkg["location"].get("local_tax_pct", 0) / 100,
        "current_note": state_note(cur_pkg["location"], data),
        "offer_note": state_note(off_pkg["location"], data),
    }

    return {
        "filing_status": filing,
        "tax_data": {"as_of": data["as_of"], "verified": data["verified"]},
        "current": cur, "offer": off, "offer_year1": y1,
        "adjusted": {
            "reference_index": ref, "current": cur_adj, "offer": off_adj, "delta": off_adj - cur_adj,
            "delta_pct": (off_adj - cur_adj) / cur_adj * 100,
            "offer_year1": y1_adj, "year1_delta": y1_adj - cur_adj, "one_time_costs": one_time,
        },
        "break_even_base": break_even,
        "state_comparison": state_cmp,
        "market": market,
        "negotiation": neg,
        "_off_pkg": off_pkg,  # for sweep; stripped before output
    }


def _percentile(x: float, m: dict) -> str | None:
    pts = [(k, m.get(k)) for k in ("p25", "p50", "p75")]
    if not any(v for _, v in pts):
        return None
    if m.get("p25") and x < m["p25"]:
        return "below p25"
    if m.get("p50") and x < m["p50"]:
        return "p25-p50"
    if m.get("p75") and x < m["p75"]:
        return "p50-p75"
    return "above p75" if m.get("p75") else "above p50"


def sweep(rep: dict, cfg: dict, filing: str, data: dict, spec: str) -> list[dict]:
    lo, hi, step = (float(x) for x in spec.split(":"))
    ref = rep["current"]["col_index"]
    cur_adj = rep["adjusted"]["current"]
    rows, x = [], lo
    while x <= hi + 1e-9:
        p = copy.deepcopy(rep["_off_pkg"])
        p["base_salary"] = x
        r = compute(p, filing, data)
        rows.append({"base": x, "net_cash_monthly": r["net_cash_monthly"], "adjusted_delta": col_adjusted(r, ref) - cur_adj})
        x += step
    return rows


# ---------------------------------------------------------------------- output

def m(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"-${abs(x):,.0f}" if round(x) < 0 else f"${abs(x):,.0f}"


def sm(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def comparison_csv_rows(rep: dict) -> list[tuple[str, object, object]]:
    """(label, current_value, offer_value) with raw, unformatted numbers — for a spreadsheet, not
    a report. Numeric rows get a Difference (Offer - Current) column written alongside; text/blank
    rows (Location, PTO days when unknown) don't."""
    c, o, a = rep["current"], rep["offer"], rep["adjusted"]
    return [
        ("Location", c["location"], o["location"]),
        ("Cost-of-living index (100 = US avg)", c["col_index"], o["col_index"]),
        ("Base salary", c["base"], o["base"]),
        ("Expected bonus", c["bonus"], o["bonus"]),
        ("Retention/sign-on bonus (annualized)", c["retention_bonus_total"], o["retention_bonus_total"]),
        ("Equity (annualised)", c["equity"], o["equity"]),
        ("Employer 401k match", c["employer_401k_match"], o["employer_401k_match"]),
        ("Perks (value)", c["perk_total"], o["perk_total"]),
        ("Total comp, pre-tax", c["total_comp_pretax"], o["total_comp_pretax"]),
        ("Employee costs (premiums etc.)", -c["employee_cost_total"], -o["employee_cost_total"]),
        ("Federal income tax", -c["federal_tax"], -o["federal_tax"]),
        ("FICA", -c["fica"], -o["fica"]),
        ("State income tax", -c["state_tax"], -o["state_tax"]),
        ("Local income tax", -c["local_tax"], -o["local_tax"]),
        ("Own 401k contribution", c["employee_401k"], o["employee_401k"]),
        ("Net cash after tax (per year)", c["net_cash"], o["net_cash"]),
        ("Net cash per month", round(c["net_cash_monthly"], 2), round(o["net_cash_monthly"], 2)),
        ("Effective tax rate (%)", round(c["effective_tax_rate"] * 100, 1), round(o["effective_tax_rate"] * 100, 1)),
        ("Net value (cash + perks + 401k)", c["net_value"], o["net_value"]),
        ("Monthly housing (info only)", c["monthly_housing"], o["monthly_housing"]),
        ("PTO days", c["pto_days"], o["pto_days"]),
        ("COL-adjusted net value", round(a["current"], 2), round(a["offer"], 2)),
        ("Break-even base at offer location", "", rep["break_even_base"]),
    ]


def _csv_number(x: object) -> object:
    return round(x, 2) if isinstance(x, (int, float)) else x


def load_csv_table(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    """(header, {metric_label: row_cells}) from an existing ledger CSV, or ([], {}) if none/empty."""
    if not path.exists():
        return [], {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return [], {}
    header, body = rows[0], rows[1:]
    return header, {row[0]: row for row in body if row}


def update_comparison_csv(rep: dict, path: Path, column_label: str) -> None:
    """Merge this run's numbers into a per-job ledger CSV instead of writing a fresh file: the
    `Current` column is refreshed every run (it should always reflect the latest package, e.g.
    after a config correction), a named scenario column (an offer round, or a what-if ask via
    --offer-base) is added or, if the same label is reused, overwritten in place -- every other
    previously-recorded round's column is left untouched. This is what lets one CSV accumulate a
    whole negotiation's rounds instead of a fresh dated file per run (see SKILL.md Step 6/9)."""
    header, by_metric = load_csv_table(path)
    if not header:
        header = ["Metric", "Current"]
    if "Current" not in header:
        header = [*header, "Current"]
    if column_label not in header:
        header = [*header, column_label]

    rows: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for label, cells in by_metric.items():
        rows[label] = {col: (cells[i] if i < len(cells) else "") for i, col in enumerate(header)}
        rows[label]["Metric"] = label
        order.append(label)

    for label, cur_raw, off_raw in comparison_csv_rows(rep):
        if label not in rows:
            rows[label] = {col: "" for col in header}
            rows[label]["Metric"] = label
            order.append(label)
        rows[label]["Current"] = _csv_number(cur_raw)
        rows[label][column_label] = _csv_number(off_raw)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for label in order:
            writer.writerow([rows[label].get(col, "") for col in header])


def render_md(rep: dict, sweep_rows: list[dict] | None) -> str:
    c, o, a, n, s = rep["current"], rep["offer"], rep["adjusted"], rep["negotiation"], rep["state_comparison"]
    L = []
    L.append(f"# Compensation comparison ({rep['filing_status']} filer)\n")
    L.append(f"_Tax data as of {rep['tax_data']['as_of']}, verified={rep['tax_data']['verified']}. Estimates, not tax advice._\n")
    rows = [
        ("Location", c["location"], o["location"]),
        ("Cost-of-living index (100 = US avg)", f"{c['col_index']:.0f}", f"{o['col_index']:.0f}"),
        ("Base salary", m(c["base"]), m(o["base"])),
        ("Expected bonus", m(c["bonus"]), m(o["bonus"])),
        ("Retention/sign-on bonus (annualized)", m(c["retention_bonus_total"]), m(o["retention_bonus_total"])),
        ("Equity (annualised)", m(c["equity"]), m(o["equity"])),
        ("Employer 401k match", m(c["employer_401k_match"]), m(o["employer_401k_match"])),
        ("Perks (value)", m(c["perk_total"]), m(o["perk_total"])),
        ("**Total comp, pre-tax**", m(c["total_comp_pretax"]), m(o["total_comp_pretax"])),
        ("Employee costs (premiums etc.)", m(-c["employee_cost_total"]), m(-o["employee_cost_total"])),
        ("Federal income tax", m(-c["federal_tax"]), m(-o["federal_tax"])),
        ("FICA", m(-c["fica"]), m(-o["fica"])),
        ("State income tax", m(-c["state_tax"]), m(-o["state_tax"])),
        ("Local income tax", m(-c["local_tax"]), m(-o["local_tax"])),
        ("Own 401k contribution", m(c["employee_401k"]), m(o["employee_401k"])),
        ("**Net cash after tax (per year)**", m(c["net_cash"]), m(o["net_cash"])),
        ("Net cash per month", m(c["net_cash_monthly"]), m(o["net_cash_monthly"])),
        ("Effective tax rate", f"{c['effective_tax_rate']:.1%}", f"{o['effective_tax_rate']:.1%}"),
        ("**Net value** (cash + perks + 401k)", m(c["net_value"]), m(o["net_value"])),
        ("Monthly housing (info only)", m(c["monthly_housing"]), m(o["monthly_housing"])),
        ("PTO days", str(c["pto_days"] or "n/a"), str(o["pto_days"] or "n/a")),
    ]
    L.append("| | Current | Offer |\n|---|---:|---:|")
    L += [f"| {k} | {x} | {y} |" for k, x, y in rows]

    if c["retention_bonuses"] or o["retention_bonuses"]:
        L.append("\n## Retention/sign-on bonuses detail\n")
        L.append("Annualized (`total_amount / vesting_years`), treated as ordinary cash income — not a payment schedule.\n")
        for tag, r in (("Current", c), ("Offer", o)):
            for rb in r["retention_bonuses"]:
                end = vesting_end(rb["start_date"], rb["vesting_years"])
                window = f", vests {rb['start_date']} -> {end}" if rb["start_date"] and end else ""
                L.append(
                    f"- {tag}: {rb['name']} = {m(rb['total_amount'])} over {rb['vesting_years']:.0f}yr "
                    f"({m(rb['annual_value'])}/yr while vesting{window})"
                )

    L.append("\n## Perks and costs detail\n")
    for tag, r in (("Current", c), ("Offer", o)):
        for p in r["perks"]:
            L.append(f"- {tag} perk: {p['name']} = {m(p['annual_value'])}/yr{' (taxable)' if p['taxable'] else ''}")
        for p in r["employee_costs"]:
            L.append(f"- {tag} cost: {p['name']} = {m(p['annual_cost'])}/yr")

    L.append("\n## State income tax on identical wages\n")
    L.append(f"Wages taxed: {m(s['wages_used'])}\n")
    L.append(f"- {s['current_state']} (+local): {m(s['current_state_tax'])}" + (f"  _({s['current_note']})_" if s["current_note"] else ""))
    L.append(f"- {s['offer_state']} (+local): {m(s['offer_state_tax'])}" + (f"  _({s['offer_note']})_" if s["offer_note"] else ""))
    L.append(f"- Difference: {sm(s['current_state_tax'] - s['offer_state_tax'])}/yr in favour of {'offer' if s['offer_state_tax'] < s['current_state_tax'] else 'current' if s['offer_state_tax'] > s['current_state_tax'] else 'neither'}")

    L.append("\n## Standard-of-living adjusted result\n")
    L.append(f"Offer purchasing power is scaled by {a['reference_index']:.0f}/{o['col_index']:.0f} (index of current location / offer location); 401k is not scaled.\n")
    L.append(f"- Current, adjusted: {m(a['current'])}")
    L.append(f"- Offer, adjusted: {m(a['offer'])}  ->  **{sm(a['delta'])}/yr ({a['delta_pct']:+.1f}%)**")
    if a["one_time_costs"] or rep["offer_year1"]["sign_on"]:
        L.append(f"- Year 1 incl. sign-on {m(rep['offer_year1']['sign_on'])} and one-time costs {m(a['one_time_costs'])}: {m(a['offer_year1'])} -> {sm(a['year1_delta'])} vs current")
    L.append(f"- **Break-even base at the offer location: {m(rep['break_even_base'])}** (adjusted parity with current, same bonus %, match and perks)")

    L.append("\n## Negotiation numbers\n")
    if rep["market"]:
        mk = rep["market"]
        L.append(f"Market ({mk.get('role', '')}): band {m(mk.get('range_min'))}-{m(mk.get('range_max'))}, p25 {m(mk.get('p25'))}, p50 {m(mk.get('p50'))}, p75 {m(mk.get('p75'))}, p90 {m(mk.get('p90'))}. Offer base sits **{n['percentile_of_offer_in_market'] or 'n/a'}**.\n")
    L.append("| Number | Base | Meaning |\n|---|---:|---|")
    L.append(f"| Break-even floor (calculated) | {m(n['walk_away_floor_base'])} | Below this you take a real pay cut once tax and cost of living are counted |")
    if n["user_walkaway_base"] is not None:
        L.append(f"| **Your stated walk-away** | {m(n['user_walkaway_base'])} | Your own decision, not a calculation — may be below the break-even floor |")
    L.append(f"| Target | {m(n['target_base'])} | Higher of floor and market p50 |")
    L.append(f"| Stretch | {m(n['stretch_base'])} | Higher of floor +4% and market p75 |")
    L.append(f"| Opening anchor | {m(n['opening_anchor_base'])} | Stretch +5%{' (capped at the band max)' if n['anchor_capped_at_band_max'] else ''} |")
    if n["band_max_below_break_even"]:
        L.append("\n> **The posted band tops out below break-even.** Base alone cannot reach parity: negotiate sign-on, bonus %, relocation, equity, level/title, or accept a deliberate pay cut.")
    if n["user_walkaway_base"] is not None:
        gap_floor = n["user_walkaway_gap_vs_calculated_floor"]
        if gap_floor is not None and gap_floor > 0:
            L.append(
                f"\n> **Your stated walk-away ({m(n['user_walkaway_base'])}) is {m(gap_floor)}/yr below the "
                f"calculated break-even floor.** Landing there is a deliberate choice to accept an "
                f"approximate {sm(-n['user_walkaway_gap_vs_current'])}/yr change vs your current package "
                f"— not full parity."
            )
        elif gap_floor is not None and gap_floor < 0:
            L.append(f"\n> Your stated walk-away ({m(n['user_walkaway_base'])}) is above the calculated break-even floor — more conservative than the minimum needed for parity.")
    L.append(f"\n- Gap between offer base and target: {sm(n['base_gap_to_target']) if n['base_gap_to_target'] is not None else 'n/a'}")
    L.append(f"- Each extra $1,000 of base is worth about {m(n['value_of_1k_base_net_annual'])}/yr in net cash")
    L.append(f"- One-time sign-on (gross) that makes year 1 whole at the offer base: {m(n['sign_on_to_bridge_year1_at_offer_base'])}")
    L.append(f"- Same, if base is raised to target: {m(n['sign_on_to_bridge_year1_at_target_base'])}")

    if sweep_rows:
        L.append("\n## Offer-base sweep\n")
        L.append("| Offer base | Net cash / month | COL-adjusted vs current (per year) |\n|---:|---:|---:|")
        L += [f"| {m(r['base'])} | {m(r['net_cash_monthly'])} | {sm(r['adjusted_delta'])} |" for r in sweep_rows]
    return "\n".join(L) + "\n"


def render_single_md(r: dict, filing: str, data: dict) -> str:
    """One package's tax/comp breakdown, no comparison — for `current` (no offer needed)."""
    L = []
    L.append(f"# {r['label'] or 'Compensation'} ({filing} filer)\n")
    L.append(f"_Tax data as of {data['as_of']}, verified={data['verified']}. Estimates, not tax advice._\n")
    rows = [
        ("Location", r["location"]),
        ("Cost-of-living index (100 = US avg)", f"{r['col_index']:.0f}"),
        ("Base salary", m(r["base"])),
        ("Expected bonus", m(r["bonus"])),
        ("Retention/sign-on bonus (annualized)", m(r["retention_bonus_total"])),
        ("Equity (annualised)", m(r["equity"])),
        ("Employer 401k match", m(r["employer_401k_match"])),
        ("Perks (value)", m(r["perk_total"])),
        ("**Total comp, pre-tax**", m(r["total_comp_pretax"])),
        ("Employee costs (premiums etc.)", m(-r["employee_cost_total"])),
        ("Federal income tax", m(-r["federal_tax"])),
        ("FICA", m(-r["fica"])),
        ("State income tax", m(-r["state_tax"])),
        ("Local income tax", m(-r["local_tax"])),
        ("Own 401k contribution", m(r["employee_401k"])),
        ("**Net cash after tax (per year)**", m(r["net_cash"])),
        ("Net cash per month", m(r["net_cash_monthly"])),
        ("Effective tax rate", f"{r['effective_tax_rate']:.1%}"),
        ("**Net value** (cash + perks + 401k)", m(r["net_value"])),
        ("Monthly housing (info only)", m(r["monthly_housing"])),
        ("PTO days", str(r["pto_days"] or "n/a")),
    ]
    L.append("| | Amount |\n|---|---:|")
    L += [f"| {k} | {v} |" for k, v in rows]

    if r["retention_bonuses"]:
        L.append("\n## Retention/sign-on bonuses detail\n")
        L.append("Annualized (`total_amount / vesting_years`), treated as ordinary cash income — not a payment schedule.\n")
        for rb in r["retention_bonuses"]:
            end = vesting_end(rb["start_date"], rb["vesting_years"])
            window = f", vests {rb['start_date']} -> {end}" if rb["start_date"] and end else ""
            L.append(
                f"- {rb['name']} = {m(rb['total_amount'])} over {rb['vesting_years']:.0f}yr "
                f"({m(rb['annual_value'])}/yr while vesting{window})"
            )

    L.append("\n## Perks and costs detail\n")
    for p in r["perks"]:
        L.append(f"- Perk: {p['name']} = {m(p['annual_value'])}/yr{' (taxable)' if p['taxable'] else ''}")
    for p in r["employee_costs"]:
        L.append(f"- Cost: {p['name']} = {m(p['annual_cost'])}/yr")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------- cli

def cmd_init(args: argparse.Namespace) -> None:
    dest = Path(args.config)
    if dest.exists():
        sys.exit(f"{dest} already exists; not overwriting.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLE_CONFIG, dest)
    print(f"Created {dest} (gitignored). Edit it with your real numbers.")


def cmd_current(args: argparse.Namespace) -> None:
    if not Path(args.config).exists():
        sys.exit(f"error: {args.config} not found. Run `uv run python scripts/salary_compare.py init` first.")
    cfg = load_json(Path(args.config))
    data = load_json(TAX_DATA)
    filing = args.filing or cfg.get("filing_status", "single")
    if filing not in data["federal"]:
        sys.exit(f"error: filing status must be one of {list(data['federal'])}")
    r = compute(cfg["current"], filing, data)
    out = json.dumps(r, indent=2) if args.json else render_single_md(r, filing, data)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"Wrote {args.out}")
    else:
        print(out)


def cmd_compare(args: argparse.Namespace) -> None:
    if not Path(args.config).exists():
        sys.exit(f"error: {args.config} not found. Run `uv run python scripts/salary_compare.py init` first.")
    cfg = load_json(Path(args.config))
    offer_doc = load_json(Path(args.offer))
    data = load_json(TAX_DATA)
    filing = args.filing or cfg.get("filing_status", "single")
    if filing not in data["federal"]:
        sys.exit(f"error: filing status must be one of {list(data['federal'])}")
    rep = build_report(cfg, offer_doc, filing, data, args.offer_base)
    rows = sweep(rep, cfg, filing, data, args.sweep) if args.sweep else None
    rep.pop("_off_pkg")
    if args.csv_out:
        label = args.csv_column or (
            f"Ask ${args.offer_base:,.0f}" if args.offer_base is not None
            else f"Offer ({date.today().isoformat()})"
        )
        update_comparison_csv(rep, Path(args.csv_out), label)
        print(f"Updated {args.csv_out} (column {label!r})")
    out = json.dumps({**rep, "sweep": rows}, indent=2) if args.json else render_md(rep, rows)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"Wrote {args.out}")
    else:
        print(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_project_argument(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create config/salary_config.json from the example")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    add_project_argument(p, suppress_default=True)
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("current", help="print your current package's tax/comp breakdown — no offer needed")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="personal config (default config/salary_config.json)")
    p.add_argument("--filing", choices=["single", "mfj"], help="override filing status")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--out", help="write the report to this file")
    add_project_argument(p, suppress_default=True)
    p.set_defaults(fn=cmd_current)
    p = sub.add_parser("compare", help="compare current package with an offer")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="personal config (default config/salary_config.json)")
    p.add_argument("--offer", required=True, help="offer JSON, e.g. data/output/Acme/offer_Acme_SWE_2026-09-27.json")
    p.add_argument("--offer-base", type=float, help="what-if: override the offer base salary")
    p.add_argument("--sweep", help="lo:hi:step table of offer base salaries, e.g. 100000:130000:5000")
    p.add_argument("--filing", choices=["single", "mfj"], help="override filing status")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--out", help="write the report to this file (use a .md path for the markdown report)")
    p.add_argument("--csv-out", help="merge into a per-job ledger CSV (Metric, Current, one column per round) at this path -- refreshes Current, adds/replaces the named column, never touches other rounds' columns")
    p.add_argument("--csv-column", help="column label for this run in --csv-out, e.g. 'Ask $170,000 (Grade 7-8)' -- default: 'Ask $<offer-base>' if --offer-base given, else 'Offer (<today>)'")
    add_project_argument(p, suppress_default=True)
    p.set_defaults(fn=cmd_compare)
    args = ap.parse_args(argv)
    chdir_to_project_root(args.project)
    args.fn(args)


if __name__ == "__main__":
    main()
