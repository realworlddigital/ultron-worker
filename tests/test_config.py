from __future__ import annotations

from pathlib import Path

import pytest

from ultron_worker.__main__ import main
from ultron_worker.config import ConfigError, load

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_example_config_parses(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "k")
    monkeypatch.setenv("ProgramData", str(tmp_path))
    cfg = load(EXAMPLE)
    assert cfg.domain == "compute" and cfg.actor == "ultron-runner"
    assert cfg.gui_host == "127.0.0.1" and cfg.ollama_url == "http://127.0.0.1:11434"
    assert cfg.poll_seconds == 60 and cfg.max_attempts == 3


def test_key_comes_from_file_or_env_never_created(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ULTRON_BACKBONE_KEY", raising=False)
    key = tmp_path / "runner.key"
    cfg_text = (
        f'[backbone]\nurl = "https://bb.example"\nkey_file = "{key.as_posix()}"\n'
        f'[runner]\nallowed_roots = ["{tmp_path.as_posix()}"]\n'
    )
    with pytest.raises(ConfigError, match="no backbone key"):
        load(write(tmp_path, cfg_text))
    assert not key.exists()  # the runner never writes a key
    key.write_text("secret-from-david\n")
    assert load(write(tmp_path, cfg_text)).key == "secret-from-david"


def test_cli_rejects_bad_config(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "k")
    p = write(
        tmp_path,
        '[backbone]\nurl = "https://bb.example"\ndomain = "finance"\n'
        f'[runner]\nallowed_roots = ["{tmp_path.as_posix()}"]\n',
    )
    assert main(["--config", str(p), "--check"]) == 2
    assert "hard-excluded" in capsys.readouterr().err
