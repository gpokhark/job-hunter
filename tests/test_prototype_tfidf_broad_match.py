"""Minimal coverage for `scripts/prototype_tfidf_broad_match.py`'s CLI argument validation --
docs/agent-runtime-audit.md's "input validation" finding asked for negative-value rejection to be
applied consistently across every numeric CLI option in this codebase, this prototype/diagnostic
script included, even though it otherwise has no dedicated test suite (see CLAUDE.md/docs/SPEC.md
on this script's status as an exception to the operational `--project` convention)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from prototype_tfidf_broad_match import main  # noqa: E402


@pytest.mark.parametrize("option", ["--top", "--min-postings"])
def test_negative_numeric_options_are_rejected(option, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prototype_tfidf_broad_match.py", option, "-1"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "non-negative" in capsys.readouterr().err
