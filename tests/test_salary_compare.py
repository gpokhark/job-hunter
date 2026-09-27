"""Run: uv run pytest tests/test_salary_compare.py -v"""
import copy
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import salary_compare as sc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "scripts" / "data" / "tax_data_2026.json").read_text())
CFG = json.loads((ROOT / "config" / "salary_config.example.json").read_text())
PKG = CFG["current"]


class TestFederalTax:
    def test_single_known_value(self):
        # 100k wages -> 83.9k taxable: 1240 + 4560 + 0.22*(83900-50400)
        assert sc.federal_tax(100000, "single", DATA) == 1240 + 4560 + 7370


class TestFicaTax:
    def test_caps_social_security(self):
        assert sc.fica_tax(300000, "single", DATA) == 184500 * 0.062 + 300000 * 0.0145 + 100000 * 0.009


class TestStateTax:
    def test_no_tax_state_and_flat_state(self):
        assert sc.state_tax(100000, {"state": "TX"}, "single", DATA) == 0
        assert round(sc.state_tax(100000, {"state": "MI"}, "single", DATA), 2) == 4250

    def test_override(self):
        assert sc.state_tax(100000, {"state": "ZZ", "state_tax_rate_pct": 3}, "single", DATA) == 3000


class TestCompute:
    def test_deterministic(self):
        assert sc.compute(PKG, "single", DATA) == sc.compute(PKG, "single", DATA)

    def test_match_capped(self):
        r = sc.compute(PKG, "single", DATA)  # 6% contrib, 4% cap, base only
        assert round(r["employer_401k_match"], 2) == 100000 * 0.04

    def test_taxable_perk_raises_tax_not_net_value_sign(self):
        a = copy.deepcopy(PKG)
        b = copy.deepcopy(PKG)
        for p in b["perks"]:
            p["taxable"] = True
        assert sc.compute(b, "single", DATA)["total_tax"] > sc.compute(a, "single", DATA)["total_tax"]

    def test_more_base_more_net(self):
        hi = copy.deepcopy(PKG)
        hi["base_salary"] += 10000
        assert sc.compute(hi, "single", DATA)["net_cash"] > sc.compute(PKG, "single", DATA)["net_cash"]


class TestEmployeeContribution:
    """employee_401k and employer_401k_match both apply to `elig` (base only unless
    match_on_bonus is set) -- a plan's deferral % and match % share one eligible-pay definition."""

    def test_contribution_is_base_only_by_default(self):
        pkg = copy.deepcopy(PKG)
        pkg["retirement"]["employee_contribution_pct"] = 6
        r = sc.compute(pkg, "single", DATA)
        assert round(r["employee_401k"], 2) == round(pkg["base_salary"] * 0.06, 2)

    def test_contribution_unaffected_by_bonus_size_when_match_on_bonus_false(self):
        small_bonus = copy.deepcopy(PKG)
        small_bonus["bonus"] = {"target_pct": 1, "expected_payout_pct_of_target": 100}
        big_bonus = copy.deepcopy(PKG)
        big_bonus["bonus"] = {"target_pct": 80, "expected_payout_pct_of_target": 100}
        assert (
            sc.compute(small_bonus, "single", DATA)["employee_401k"]
            == sc.compute(big_bonus, "single", DATA)["employee_401k"]
        )

    def test_contribution_matches_employer_match_dollar_for_dollar_when_rates_equal(self):
        pkg = copy.deepcopy(PKG)
        pkg["retirement"] = {
            "employee_contribution_pct": 6, "match_rate": 1.0, "match_cap_pct": 6, "match_on_bonus": False,
        }
        r = sc.compute(pkg, "single", DATA)
        assert r["employee_401k"] == r["employer_401k_match"]

    def test_match_on_bonus_widens_the_contribution_base_too(self):
        pkg = copy.deepcopy(PKG)
        pkg.pop("retention_bonuses", None)
        pkg["bonus"] = {"target_pct": 20, "expected_payout_pct_of_target": 100}
        pkg["retirement"]["match_on_bonus"] = True
        r = sc.compute(pkg, "single", DATA)
        expected_elig = pkg["base_salary"] + pkg["base_salary"] * 0.20
        assert round(r["employee_401k"], 2) == round(expected_elig * 0.06, 2)


def _pkg_without_retention() -> dict:
    """PKG (from the example config) now ships its own example retention_bonuses entry —
    strip it for tests that want a clean baseline."""
    pkg = copy.deepcopy(PKG)
    pkg.pop("retention_bonuses", None)
    return pkg


class TestRetentionBonus:
    def test_annualized_and_added_to_gross_and_net_cash(self):
        clean = _pkg_without_retention()
        with_rb = copy.deepcopy(clean)
        with_rb["retention_bonuses"] = [{"name": "Sign-on", "total_amount": 60000, "vesting_years": 3}]
        base = sc.compute(clean, "single", DATA)
        r = sc.compute(with_rb, "single", DATA)
        assert r["retention_bonus_total"] == 20000
        assert r["gross_cash"] == base["gross_cash"] + 20000
        # Real payroll cash: unlike a perk, it must actually raise net cash, not just taxable wages.
        assert r["net_cash"] > base["net_cash"]

    def test_defaults_to_three_years_when_vesting_years_omitted(self):
        pkg = _pkg_without_retention()
        pkg["retention_bonuses"] = [{"name": "Sign-on", "total_amount": 30000}]
        assert sc.compute(pkg, "single", DATA)["retention_bonus_total"] == 10000

    def test_multiple_entries_sum(self):
        pkg = _pkg_without_retention()
        pkg["retention_bonuses"] = [
            {"name": "Sign-on", "total_amount": 60000, "vesting_years": 3},
            {"name": "Performance", "total_amount": 9000, "vesting_years": 3},
        ]
        assert sc.compute(pkg, "single", DATA)["retention_bonus_total"] == 23000

    def test_absent_by_default(self):
        assert sc.compute(_pkg_without_retention(), "single", DATA)["retention_bonus_total"] == 0


class TestVestingEnd:
    def test_adds_whole_years(self):
        assert sc.vesting_end("2025-09-02", 3) == "2028-09-02"

    def test_none_when_no_start_date(self):
        assert sc.vesting_end(None, 3) is None

    def test_leap_day_start_falls_back_a_day_on_non_leap_target(self):
        assert sc.vesting_end("2024-02-29", 3) == "2027-02-28"


class TestBuildReport:
    def test_break_even_gives_parity_and_identical_job_is_zero_delta(self):
        offer = {"offer": copy.deepcopy(PKG), "market": {}, "one_time_costs": []}
        rep = sc.build_report(CFG, offer, "single", DATA, None)
        assert abs(rep["adjusted"]["delta"]) < 1e-6
        assert abs(rep["break_even_base"] - PKG["base_salary"]) <= 1

    def test_cheaper_city_lowers_break_even(self):
        o = copy.deepcopy(PKG)
        o["location"]["cost_of_living_index"] = 80
        rep = sc.build_report(CFG, {"offer": o}, "single", DATA, None)
        assert rep["break_even_base"] < PKG["base_salary"]

    def test_absent_user_walkaway_leaves_negotiation_fields_none(self):
        offer = {"offer": copy.deepcopy(PKG), "market": {}, "one_time_costs": []}
        rep = sc.build_report(CFG, offer, "single", DATA, None)
        n = rep["negotiation"]
        assert n["user_walkaway_base"] is None
        assert n["user_walkaway_adjusted"] is None
        assert n["user_walkaway_gap_vs_current"] is None
        assert n["user_walkaway_gap_vs_calculated_floor"] is None

    def test_user_walkaway_below_calculated_floor_reports_a_positive_gap(self):
        o = copy.deepcopy(PKG)
        o["base_salary"] = 90000  # well below PKG's own base -> a real shortfall
        offer = {"offer": o, "market": {}, "one_time_costs": [], "user_walkaway_base": 50000}
        rep = sc.build_report(CFG, offer, "single", DATA, None)
        n = rep["negotiation"]
        assert n["user_walkaway_base"] == 50000
        assert n["user_walkaway_adjusted"] is not None
        # A stated walkaway below the calculated break-even floor -> positive gap (shortfall).
        assert n["user_walkaway_gap_vs_calculated_floor"] > 0
        assert n["user_walkaway_gap_vs_current"] > 0

    def test_user_walkaway_above_calculated_floor_reports_a_negative_gap(self):
        offer = {
            "offer": copy.deepcopy(PKG), "market": {}, "one_time_costs": [],
            "user_walkaway_base": PKG["base_salary"] + 50000,
        }
        rep = sc.build_report(CFG, offer, "single", DATA, None)
        n = rep["negotiation"]
        assert n["user_walkaway_gap_vs_calculated_floor"] < 0
        assert n["user_walkaway_gap_vs_current"] < 0  # above current -> a raise, not a shortfall


class TestComparisonCsv:
    def _rep(self, base_salary=90000):
        o = copy.deepcopy(PKG)
        o["base_salary"] = base_salary
        rep = sc.build_report(CFG, {"offer": o, "market": {}, "one_time_costs": []}, "single", DATA, None)
        rep.pop("_off_pkg")
        return rep

    def test_numeric_rows_get_a_difference_and_text_rows_dont(self):
        rows = {label: (cur, off) for label, cur, off in sc.comparison_csv_rows(self._rep())}
        assert rows["Base salary"] == (PKG["base_salary"], 90000.0)
        loc_cur, loc_off = rows["Location"]
        assert isinstance(loc_cur, str) and isinstance(loc_off, str)

    def test_first_write_creates_metric_current_and_named_column(self, tmp_path):
        out = tmp_path / "ledger.csv"
        sc.update_comparison_csv(self._rep(base_salary=PKG["base_salary"] - 1000), out, "Round 1")
        rows = list(csv.reader(out.open()))
        assert rows[0] == ["Metric", "Current", "Round 1"]
        base_row = next(r for r in rows if r[0] == "Base salary")
        assert float(base_row[1]) == PKG["base_salary"]
        assert float(base_row[2]) == PKG["base_salary"] - 1000

    def test_second_round_adds_a_column_and_keeps_the_first(self, tmp_path):
        out = tmp_path / "ledger.csv"
        sc.update_comparison_csv(self._rep(base_salary=125000), out, "Round 1")
        sc.update_comparison_csv(self._rep(base_salary=170000), out, "My ask $170,000")
        rows = list(csv.reader(out.open()))
        assert rows[0] == ["Metric", "Current", "Round 1", "My ask $170,000"]
        base_row = next(r for r in rows if r[0] == "Base salary")
        assert [float(x) for x in base_row[1:]] == [PKG["base_salary"], 125000.0, 170000.0]

    def test_reusing_a_column_label_overwrites_in_place_not_duplicates(self, tmp_path):
        out = tmp_path / "ledger.csv"
        sc.update_comparison_csv(self._rep(base_salary=125000), out, "Round 1")
        sc.update_comparison_csv(self._rep(base_salary=130000), out, "Round 1")
        rows = list(csv.reader(out.open()))
        assert rows[0] == ["Metric", "Current", "Round 1"]
        base_row = next(r for r in rows if r[0] == "Base salary")
        assert float(base_row[2]) == 130000.0

    def test_current_column_refreshes_every_run(self, tmp_path):
        out = tmp_path / "ledger.csv"
        sc.update_comparison_csv(self._rep(), out, "Round 1")
        higher_current = copy.deepcopy(self._rep())
        higher_current["current"]["base"] = PKG["base_salary"] + 12345
        sc.update_comparison_csv(higher_current, out, "Round 2")
        rows = list(csv.reader(out.open()))
        base_row = next(r for r in rows if r[0] == "Base salary")
        assert float(base_row[1]) == PKG["base_salary"] + 12345

    def test_rounds_away_floating_point_noise(self, tmp_path):
        out = tmp_path / "ledger.csv"
        sc.update_comparison_csv(self._rep(), out, "Round 1")
        rows = list(csv.reader(out.open()))
        net_cash_row = next(r for r in rows if r[0] == "Net cash after tax (per year)")
        for cell in net_cash_row[1:]:
            assert len(cell.split(".")[-1]) <= 2


class TestCli:
    """Relative config/data paths resolve against the cwd `chdir_to_project_root` leaves the
    process in (rootutil convention; see check_lm_studio.py's equivalent test)."""

    def test_init_and_compare_run_from_the_project_root(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config").mkdir()
        (tmp_path / "scripts" / "data").mkdir(parents=True)
        (tmp_path / "config" / "salary_config.example.json").write_text(json.dumps(CFG))
        (tmp_path / "scripts" / "data" / "tax_data_2026.json").write_text(json.dumps(DATA))
        monkeypatch.chdir(tmp_path)

        sc.main(["init"])
        assert (tmp_path / "config" / "salary_config.json").exists()
        capsys.readouterr()

        offer_path = tmp_path / "offer.json"
        offer_path.write_text(json.dumps({"offer": copy.deepcopy(PKG), "market": {}, "one_time_costs": []}))
        sc.main(["compare", "--offer", str(offer_path)])
        out = capsys.readouterr().out
        assert "Compensation comparison" in out

    def test_compare_csv_out_writes_alongside_markdown(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config").mkdir()
        (tmp_path / "scripts" / "data").mkdir(parents=True)
        (tmp_path / "config" / "salary_config.json").write_text(json.dumps(CFG))
        (tmp_path / "scripts" / "data" / "tax_data_2026.json").write_text(json.dumps(DATA))
        monkeypatch.chdir(tmp_path)

        offer = copy.deepcopy(PKG)
        offer["base_salary"] -= 5000
        offer_path = tmp_path / "offer.json"
        offer_path.write_text(json.dumps({"offer": offer, "market": {}, "one_time_costs": []}))
        md_path, csv_path = tmp_path / "comparison.md", tmp_path / "comparison.csv"
        sc.main(["compare", "--offer", str(offer_path), "--out", str(md_path), "--csv-out", str(csv_path), "--csv-column", "Round 1"])

        assert md_path.exists() and csv_path.exists()
        rows = list(csv.reader(csv_path.open()))
        assert rows[0] == ["Metric", "Current", "Round 1"]
        base_row = next(r for r in rows if r[0] == "Base salary")
        assert float(base_row[1]) - float(base_row[2]) == 5000.0

    def test_compare_csv_out_second_round_via_offer_base_gets_a_default_label(self, tmp_path, monkeypatch):
        (tmp_path / "config").mkdir()
        (tmp_path / "scripts" / "data").mkdir(parents=True)
        (tmp_path / "config" / "salary_config.json").write_text(json.dumps(CFG))
        (tmp_path / "scripts" / "data" / "tax_data_2026.json").write_text(json.dumps(DATA))
        monkeypatch.chdir(tmp_path)

        offer_path = tmp_path / "offer.json"
        offer_path.write_text(json.dumps({"offer": copy.deepcopy(PKG), "market": {}, "one_time_costs": []}))
        csv_path = tmp_path / "ledger.csv"
        sc.main(["compare", "--offer", str(offer_path), "--csv-out", str(csv_path), "--csv-column", "Round 1"])
        sc.main(["compare", "--offer", str(offer_path), "--offer-base", "170000", "--csv-out", str(csv_path)])

        rows = list(csv.reader(csv_path.open()))
        assert rows[0] == ["Metric", "Current", "Round 1", "Ask $170,000"]

    def test_current_prints_tax_breakdown_with_no_offer_file(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config").mkdir()
        (tmp_path / "scripts" / "data").mkdir(parents=True)
        (tmp_path / "config" / "salary_config.json").write_text(json.dumps(CFG))
        (tmp_path / "scripts" / "data" / "tax_data_2026.json").write_text(json.dumps(DATA))
        monkeypatch.chdir(tmp_path)

        sc.main(["current"])
        out = capsys.readouterr().out
        assert "Federal income tax" in out
        assert "FICA" in out
        assert "State income tax" in out
        assert "Retention/sign-on bonus (annualized)" in out

    def test_current_json_matches_compute(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config").mkdir()
        (tmp_path / "scripts" / "data").mkdir(parents=True)
        (tmp_path / "config" / "salary_config.json").write_text(json.dumps(CFG))
        (tmp_path / "scripts" / "data" / "tax_data_2026.json").write_text(json.dumps(DATA))
        monkeypatch.chdir(tmp_path)

        sc.main(["current", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["net_cash"] == sc.compute(PKG, "single", DATA)["net_cash"]


class TestRenderSingleMd:
    def test_includes_retention_detail_only_when_present(self):
        with_rb = _pkg_without_retention()
        with_rb["retention_bonuses"] = [{"name": "Sign-on", "total_amount": 30000, "vesting_years": 3, "start_date": "2025-01-01"}]
        r = sc.compute(with_rb, "single", DATA)
        out = sc.render_single_md(r, "single", DATA)
        assert "Retention/sign-on bonuses detail" in out
        assert "Sign-on = $30,000 over 3yr" in out

        clean = sc.compute(_pkg_without_retention(), "single", DATA)
        assert "Retention/sign-on bonuses detail" not in sc.render_single_md(clean, "single", DATA)


class TestRenderMdWalkaway:
    def test_stated_walkaway_row_and_shortfall_note_appear_only_when_set(self):
        o = copy.deepcopy(PKG)
        o["base_salary"] = 90000
        offer = {"offer": o, "market": {}, "one_time_costs": [], "user_walkaway_base": 50000}
        rep = sc.build_report(CFG, offer, "single", DATA, None)
        rep.pop("_off_pkg")
        out = sc.render_md(rep, None)
        assert "Your stated walk-away" in out
        assert "deliberate choice to accept" in out

        offer_without = {"offer": copy.deepcopy(PKG), "market": {}, "one_time_costs": []}
        rep2 = sc.build_report(CFG, offer_without, "single", DATA, None)
        rep2.pop("_off_pkg")
        assert "Your stated walk-away" not in sc.render_md(rep2, None)
