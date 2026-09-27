"""Run: uv run pytest tests/test_salary_compare.py -v"""
import copy
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
