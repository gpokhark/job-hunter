import sys
from pathlib import Path

import httpx
import respx
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from check_lm_studio import main  # noqa: E402


def _write_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "lm_studio.yaml").write_text(
        yaml.safe_dump({"base_url": "http://127.0.0.1:1234/v1", "model": "local-model"})
    )


@respx.mock
def test_main_exits_zero_and_prints_ok_when_reachable(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    respx.get("http://127.0.0.1:1234/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "local-model"}]})
    )
    exit_code = main([])
    assert exit_code == 0
    assert "OK LM Studio" in capsys.readouterr().out


@respx.mock
def test_main_exits_nonzero_and_prints_fail_when_unreachable(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    respx.get("http://127.0.0.1:1234/v1/models").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    exit_code = main([])
    assert exit_code == 1
    assert "FAIL LM Studio" in capsys.readouterr().out


def test_main_exits_nonzero_when_no_config_or_example(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    monkeypatch.chdir(tmp_path)
    exit_code = main([])
    assert exit_code == 1
    assert "FAIL LM Studio" in capsys.readouterr().err
