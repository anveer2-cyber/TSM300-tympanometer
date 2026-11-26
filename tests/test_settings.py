import json
import sys
from pathlib import Path

from tsm300 import settings as settings_file
from tsm300.settings import Settings


def test_missing_file_gives_defaults(tmp_path):
    assert settings_file.load(tmp_path) == Settings()
    assert not Settings().capture_ready


def test_settings_survive_a_save_and_load(tmp_path):
    saved = Settings(port="COM5", emr_via="server", emr_server="192.0.2.1", emr_token="token",
                     cap_on=True, cap_url="192.0.2.2", cap_key="key")

    settings_file.save(saved, tmp_path)

    assert settings_file.load(tmp_path) == saved
    assert saved.capture_ready
    assert json.loads((tmp_path / "설정.json").read_text(encoding="utf-8"))["cap_url"] == "192.0.2.2"


def test_damaged_file_falls_back_to_the_previous_save(tmp_path):
    settings_file.save(Settings(port="COM5"), tmp_path)
    settings_file.save(Settings(port="COM6"), tmp_path)
    (tmp_path / "설정.json").write_text("{ not json", encoding="utf-8")

    assert settings_file.load(tmp_path).port == "COM5"


def test_unknown_keys_and_odd_values_are_tolerated(tmp_path):
    (tmp_path / "설정.json").write_text(
        json.dumps({"port": None, "cap_on": 1, "cap_url": 5090, "added_later": "x"}), encoding="utf-8")

    assert settings_file.load(tmp_path) == Settings(cap_on=True, cap_url="5090")


def test_directory_can_be_overridden_for_development(monkeypatch, tmp_path):
    monkeypatch.setenv("TSM300_DIR", str(tmp_path))
    assert settings_file.settings_directory() == tmp_path


def test_packaged_program_keeps_results_beside_its_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM300_DIR", str(tmp_path))
    assert settings_file.results_directory() == Path("results")

    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert settings_file.results_directory() == tmp_path / "results"
