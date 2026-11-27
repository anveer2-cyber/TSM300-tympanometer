import json
import sys
from pathlib import Path

from tsm300 import settings as settings_file
from tsm300.clinic import Clinic
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

    assert settings_file.load(tmp_path) == Settings(cap_on=True, cap_url="5090",
                                                    clinic_name=settings_file.EARLIER_CLINIC_NAME)


def test_directory_can_be_overridden_for_development(monkeypatch, tmp_path):
    monkeypatch.setenv("TSM300_DIR", str(tmp_path))
    assert settings_file.settings_directory() == tmp_path


def test_packaged_program_keeps_results_beside_its_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM300_DIR", str(tmp_path))
    assert settings_file.results_directory() == Path("results")

    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert settings_file.results_directory() == tmp_path / "results"


def test_a_new_installation_has_no_clinic_and_one_from_before_keeps_its_heading(tmp_path):
    assert settings_file.load(tmp_path).clinic == Clinic()

    (tmp_path / "설정.json").write_text(json.dumps({"port": "COM5", "cap_url": "192.0.2.1"}), encoding="utf-8")
    earlier = settings_file.load(tmp_path)
    assert earlier.clinic == Clinic(settings_file.EARLIER_CLINIC_NAME)
    # The port was the program's own find, not a choice: it becomes a memory of where the device was.
    assert (earlier.port, earlier.port_heard, earlier.cap_url) == ("", "COM5", "192.0.2.1")

    settings_file.save(Settings(port="COM5", clinic_name=""), tmp_path)
    kept = settings_file.load(tmp_path)
    assert kept.clinic == Clinic()      # a heading the clinic removed stays removed
    assert kept.port == "COM5"          # and a port chosen in this version stays chosen


def test_clinic_details_and_the_results_folder_are_kept(tmp_path):
    saved = Settings(clinic_name=" 바른이비인후과의원 ", clinic_address="부산시 해운대구 가나로 12, 3층",
                     clinic_phone="051-123-4567", results_dir=str(tmp_path / "검사 결과"))
    settings_file.save(saved, tmp_path)

    loaded = settings_file.load(tmp_path)

    assert loaded == saved
    assert loaded.clinic == Clinic("바른이비인후과의원", "부산시 해운대구 가나로 12, 3층", "051-123-4567")
    assert settings_file.results_directory(loaded.results_dir) == tmp_path / "검사 결과"
    assert settings_file.results_directory("  ") == Path("results")
