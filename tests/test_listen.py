"""The listener, fed with transmissions captured from a real TSM-300."""

import io
import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest
import serial
from PIL import Image

from conftest import needs_pty, pty
from tsm300 import cli, listen
from tsm300.console import Console, open_port
from tsm300.protocol import parse_message

DATA = Path(__file__).parent / "data"
BOTH = (DATA / "both_ears.bin").read_bytes()
LEFT_ONLY = (DATA / "left_only.bin").read_bytes()
EMPTY = (DATA / "empty.bin").read_bytes()
POWER_ON = (DATA / "power_on.bin").read_bytes()
WHEN = datetime(2026, 11, 26, 21, 12, 5)


def make_listener(tmp_path, **options):
    out = io.StringIO()
    listener = listen.Listener(Console(out=out), tmp_path / "results", now=lambda: WHEN, **options)
    return listener, out


def test_a_measurement_is_saved_as_raw_bytes_values_and_sheet(tmp_path):
    listener, out = make_listener(tmp_path)

    listener.handle([parse_message(BOTH)])

    results = tmp_path / "results"
    assert sorted(path.name for path in results.iterdir()) == [
        "20261126_211205.bin", "20261126_211205.jpg", "20261126_211205.json",
    ]
    assert (results / "20261126_211205.bin").read_bytes() == BOTH
    with Image.open(results / "20261126_211205.jpg") as sheet:
        assert sheet.format == "JPEG" and sheet.size == (2480, 3508)
    assert listener.saved == [results / "20261126_211205.jpg"]

    saved = json.loads((results / "20261126_211205.json").read_text(encoding="utf-8"))
    assert saved["received_at"] == "2026-11-26T21:12:05"
    assert saved["patient"] == {"chart_no": "", "name": "", "sex": "", "birth_date": None}
    assert saved["device"] == {"name": "TSM-300", "code": "6349"}
    assert saved["memory_id"] == 1
    right = saved["right"]
    assert right["middle_ear_pressure_dapa"] == 0
    assert right["ear_volume_ml"] == 8.39
    assert right["compliance_ml"] == 1.593
    assert right["width_dapa"] == 103
    assert len(right["tympanogram"]["pressure_dapa"]) == len(right["tympanogram"]["compliance_ml"]) == 160
    assert right["tympanogram"]["pressure_dapa"][-1] == 200.0
    assert sorted(right["reflex_ml"]) == ["1000", "2000", "3000", "4000", "500"]
    assert len(right["reflex_ml"]["1000"]) == 40

    text = out.getvalue()
    assert "[21:12:05] 검사 수신 (ID 1)" in text
    assert "오른쪽: 압력 0 daPa, 용적 8.39 ml, 컴플라이언스 1.59 ml, 폭 103 daPa, 반사 5개 주파수" in text
    assert "왼쪽: 압력 0 daPa, 용적 9.30 ml, 컴플라이언스 1.66 ml, 폭 103 daPa, 반사 5개 주파수" in text


def test_one_ear_measurement_reports_the_other_as_not_measured(tmp_path):
    listener, out = make_listener(tmp_path)

    listener.handle([parse_message(LEFT_ONLY)])

    saved = json.loads((tmp_path / "results" / "20261126_211205.json").read_text(encoding="utf-8"))
    assert saved["right"] is None and saved["left"]["compliance_ml"] == 1.657
    assert "오른쪽: 측정 없음" in out.getvalue()


def test_an_empty_memory_slot_is_reported_and_not_saved(tmp_path):
    listener, out = make_listener(tmp_path)

    listener.handle([parse_message(EMPTY)])

    assert not (tmp_path / "results").exists()
    assert "저장된 검사가 없습니다" in out.getvalue()


def test_two_results_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    listener, _ = make_listener(tmp_path)

    listener.handle([parse_message(BOTH), parse_message(LEFT_ONLY)])

    names = sorted(path.name for path in (tmp_path / "results").glob("*.json"))
    assert names == ["20261126_211205.json", "20261126_211205_2.json"]


def test_show_opens_the_saved_sheet(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(listen, "open_file", opened.append)
    listener, _ = make_listener(tmp_path, show=True)

    listener.handle([parse_message(BOTH)])

    assert opened == [tmp_path / "results" / "20261126_211205.jpg"]


def test_decode_command_turns_saved_raw_files_into_results(tmp_path, capsys):
    code = cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path)])

    assert code == 0
    assert len(list(tmp_path.glob("*.jpg"))) == 1
    assert "왼쪽: 압력 0 daPa, 용적 9.30 ml" in capsys.readouterr().out


def test_decode_command_attaches_the_given_patient(tmp_path):
    code = cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path),
                     "--chart", "12345", "--name", "홍길동", "--sex", "m", "--birth", "19800131"])

    assert code == 0
    (saved,) = tmp_path.glob("*_12345.json")
    assert saved.with_suffix(".jpg").exists()
    patient = json.loads(saved.read_text(encoding="utf-8"))["patient"]
    assert patient == {"chart_no": "12345", "name": "홍길동", "sex": "남", "birth_date": "1980-01-31"}


def test_decode_command_rejects_an_unreadable_birth_date(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path), "--birth", "1980-13-45"])
    assert "없는 날짜입니다" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@needs_pty
class TestOnASerialPort:
    """End to end over a pseudo-terminal standing in for the device."""

    pty = pty

    def open_device(self):
        master, slave = self.pty.openpty()
        return master, open_port(os.ttyname(slave), 9600)

    def test_stream_from_the_device_is_decoded_as_it_arrives(self, tmp_path):
        master, ser = self.open_device()
        listener, out = make_listener(tmp_path)
        threading.Timer(0.2, os.write, (master, POWER_ON)).start()
        threading.Timer(0.6, os.write, (master, BOTH)).start()

        listener.run(ser, stop=lambda: bool(listener.saved))

        assert "장비 켜짐: TSM-300 SCREENING (KEY 6349)" in out.getvalue()
        assert (tmp_path / "results" / "20261126_211205.bin").read_bytes() == BOTH

    def test_a_transmission_that_stops_part_way_is_reported_and_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(listen, "STALE_AFTER", 0.3)
        master, ser = self.open_device()
        listener, out = make_listener(tmp_path)
        threading.Timer(0.1, os.write, (master, BOTH[:300])).start()
        threading.Timer(1.0, os.write, (master, BOTH)).start()

        listener.run(ser, stop=lambda: bool(listener.saved))

        results = tmp_path / "results"
        assert (results / "corrupt_20261126_211205.bin").read_bytes() == BOTH[:300]
        assert "전송을 해독하지 못했습니다" in out.getvalue()
        assert (results / "20261126_211205.bin").read_bytes() == BOTH

    def test_run_reconnects_after_the_port_fails(self, tmp_path):
        master, ser = self.open_device()
        attempts = []

        def opener(port, baud):
            attempts.append((port, baud))
            if len(attempts) == 1:
                raise serial.SerialException("port busy")
            if len(attempts) == 2:
                os.write(master, BOTH)
                return ser
            raise KeyboardInterrupt

        class Unplugged:
            """Delivers what the real port has, then fails like a removed USB adapter."""
            in_waiting = 0

            def __init__(self):
                self._reads = 0

            def read(self, size):
                self._reads += 1
                if self._reads > 20:
                    raise serial.SerialException("device removed")
                return ser.read(ser.in_waiting or 1)

            def close(self):
                pass

        out = io.StringIO()
        args = cli.build_parser().parse_args(["listen", "--port", "COM5", "--out", str(tmp_path / "results")])
        code = listen.run(
            args, Console(out=out),
            port_opener=lambda port, baud: Unplugged() if opener(port, baud) else None,
            sleep=lambda seconds: None,
        )

        text = out.getvalue()
        assert code == 0
        assert attempts == [("COM5", 9600)] * 3
        assert "COM5 포트를 열 수 없습니다: port busy" in text
        assert "검사 수신 (ID 1)" in text
        assert "포트 연결이 끊겼습니다: device removed" in text
        assert "종료합니다" in text
        assert len(list((tmp_path / "results").glob("*.jpg"))) == 1


def test_a_saved_result_can_be_read_back(tmp_path):
    from datetime import date

    from tsm300.patient import Patient
    from tsm300.store import load_result, save_result

    patient = Patient("00512", "홍길동", "남", date(1975, 3, 4))
    measurement = parse_message(BOTH)
    history = ({"ok": True, "id": "1", "at": "2026-11-26T21:13:00"},)
    sheet = save_result(measurement, tmp_path, WHEN, patient, "메모", history)

    for path in (sheet, sheet.with_suffix(".json"), sheet.with_suffix(".bin")):
        loaded = load_result(path)
        assert (loaded.sheet, loaded.measurement, loaded.received_at) == (sheet, measurement, WHEN)
        assert (loaded.patient, loaded.note, loaded.capture) == (patient, "메모", history)


def test_decode_command_prints_a_memo_on_the_sheet(tmp_path):
    code = cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path), "--note", "재검 필요"])

    assert code == 0
    (record,) = tmp_path.glob("*.json")
    assert json.loads(record.read_text(encoding="utf-8"))["note"] == "재검 필요"


def test_saved_result_says_what_kind_of_test_it_holds(tmp_path):
    from tsm300.store import save_result

    sheet = save_result(parse_message(BOTH), tmp_path, WHEN)

    record = json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8"))
    assert (record["exam"], record["format"]) == ("tymp", 1)


def test_result_of_another_kind_of_test_is_not_opened_as_a_tympanogram(tmp_path):
    from tsm300.store import load_result, save_result

    sheet = save_result(parse_message(BOTH), tmp_path, WHEN)
    record_file = sheet.with_suffix(".json")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record_file.write_text(json.dumps(dict(record, exam="pta")), encoding="utf-8")

    with pytest.raises(ValueError, match="pta"):
        load_result(sheet)


def test_result_saved_before_kinds_were_written_down_still_opens(tmp_path):
    from tsm300.store import load_result, save_result

    measurement = parse_message(BOTH)
    sheet = save_result(measurement, tmp_path, WHEN)
    record_file = sheet.with_suffix(".json")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    del record["exam"], record["format"]
    record_file.write_text(json.dumps(record), encoding="utf-8")

    assert load_result(sheet).measurement == measurement


# ----- what a saved result holds, and replacing one ------------------------------------------

def files_in(folder):
    return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}


def test_saved_result_remembers_the_test_time_and_the_clinic(tmp_path):
    from tsm300.clinic import Clinic
    from tsm300.store import load_result, save_result

    clinic = Clinic("바른이비인후과의원", "부산시 해운대구 가나로 12", "051-123-4567")
    tested = datetime(2026, 11, 25, 14, 30)

    as_received = load_result(save_result(parse_message(BOTH), tmp_path / "a", WHEN))
    corrected = load_result(save_result(parse_message(BOTH), tmp_path / "b", WHEN, tested_at=tested, clinic=clinic))

    assert (as_received.received_at, as_received.tested_at, as_received.clinic) == (WHEN, WHEN, Clinic())
    assert (corrected.received_at, corrected.tested_at, corrected.clinic) == (WHEN, tested, clinic)
    assert corrected.sheet.name == "20261126_211205.jpg"  # named after its arrival, whatever the test time


def test_result_saved_before_test_times_were_kept_reads_the_time_it_arrived(tmp_path):
    from tsm300.store import load_result, save_result

    sheet = save_result(parse_message(BOTH), tmp_path, WHEN)
    record_file = sheet.with_suffix(".json")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    del record["tested_at"], record["clinic"]
    record_file.write_text(json.dumps(record), encoding="utf-8")

    loaded = load_result(sheet)

    assert (loaded.tested_at, loaded.clinic) == (WHEN, None)   # no clinic written down, not "printed without one"


def test_replacing_a_result_keeps_its_name_or_renames_it_for_another_patient(tmp_path):
    from datetime import date

    from tsm300.patient import Patient
    from tsm300.store import load_result, save_result

    measurement = parse_message(BOTH)
    hong, kim = Patient("00512", "홍길동", "남", date(1975, 3, 4)), Patient("12345", "김하나")
    first = save_result(measurement, tmp_path, WHEN, hong)

    noted = save_result(measurement, tmp_path, WHEN, hong, "메모", replacing=first)

    assert noted == first and load_result(noted).note == "메모"
    assert sorted(files_in(tmp_path)) == ["20261126_211205_00512.bin", "20261126_211205_00512.jpg",
                                          "20261126_211205_00512.json"]

    moved = save_result(measurement, tmp_path, WHEN, kim, "메모", replacing=noted)

    assert moved.name == "20261126_211205_12345.jpg" and load_result(moved).patient == kim
    assert sorted(files_in(tmp_path)) == ["20261126_211205_12345.bin", "20261126_211205_12345.jpg",
                                          "20261126_211205_12345.json"]


def test_a_save_that_fails_leaves_the_earlier_files_exactly_as_they_were(tmp_path, monkeypatch):
    from tsm300 import store

    measurement = parse_message(BOTH)
    sheet = store.save_result(measurement, tmp_path, WHEN)
    before = files_in(tmp_path)

    def no_room(*arguments, **options):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "render", no_room)
    with pytest.raises(OSError):
        store.save_result(measurement, tmp_path, WHEN, note="메모", replacing=sheet)
    with pytest.raises(OSError):
        store.save_result(measurement, tmp_path / "new", WHEN)

    assert files_in(tmp_path) == before
    assert list((tmp_path / "new").iterdir()) == []


def test_a_sheet_held_open_by_another_program_stops_the_change_before_anything_is_replaced(tmp_path, monkeypatch):
    from tsm300 import store

    measurement = parse_message(BOTH)
    sheet = store.save_result(measurement, tmp_path, WHEN)
    before = files_in(tmp_path)
    real_replace = store.os.replace

    def held_open(source, target):
        if str(target).endswith(".jpg"):
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        real_replace(source, target)

    monkeypatch.setattr(store.os, "replace", held_open)
    monkeypatch.setattr(store, "_REPLACE_PAUSE", 0)
    with pytest.raises(PermissionError):
        store.save_result(measurement, tmp_path, WHEN, note="메모", replacing=sheet)

    assert files_in(tmp_path) == before


def test_a_sheet_held_open_stops_a_rename_for_another_patient_and_leaves_no_second_copy(tmp_path, monkeypatch):
    from tsm300 import store
    from tsm300.patient import Patient

    measurement = parse_message(BOTH)
    sheet = store.save_result(measurement, tmp_path, WHEN)
    before = files_in(tmp_path)
    really_unlink = Path.unlink

    def held_open(path, *arguments, **options):
        if path == sheet:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        really_unlink(path, *arguments, **options)

    monkeypatch.setattr(Path, "unlink", held_open)
    with pytest.raises(PermissionError):
        store.save_result(measurement, tmp_path, WHEN, Patient("12345", "김하나"), replacing=sheet)

    assert files_in(tmp_path) == before


def test_a_file_held_for_a_moment_by_a_virus_scanner_is_moved_on_the_next_try(tmp_path, monkeypatch):
    from tsm300 import store

    refusals = [PermissionError(13, "Access is denied"), PermissionError(13, "Access is denied")]
    real_replace = store.os.replace

    def briefly_held(source, target):
        if refusals:
            raise refusals.pop()
        real_replace(source, target)

    monkeypatch.setattr(store.os, "replace", briefly_held)
    monkeypatch.setattr(store, "_REPLACE_PAUSE", 0)

    sheet = store.save_result(parse_message(BOTH), tmp_path, WHEN)

    assert refusals == [] and sorted(files_in(tmp_path)) == ["20261126_211205.bin", "20261126_211205.jpg",
                                                              "20261126_211205.json"]
    assert store.load_result(sheet).received_at == WHEN


def test_decode_command_prints_the_clinic_from_the_settings_and_the_test_time_given(tmp_path):
    from tsm300 import settings as settings_file
    from tsm300.settings import Settings

    settings_file.save(Settings(clinic_name="바른이비인후과의원", clinic_phone="051-123-4567"))

    code = cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path), "--tested", "2026-11-25 14:30"])

    assert code == 0
    (record_file,) = tmp_path.glob("*.json")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["tested_at"] == "2026-11-25T14:30:00"
    assert record["clinic"] == {"name": "바른이비인후과의원", "address": "", "phone": "051-123-4567"}


def test_decode_command_refuses_a_test_time_it_cannot_read_and_reports_a_file_it_cannot_open(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path), "--tested", "어제"])

    assert cli.main(["decode", str(tmp_path / "없는 파일.bin"), "--out", str(tmp_path)]) == 1
    assert "파일을 읽지 못했습니다" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_a_save_that_fails_part_of_the_way_puts_back_every_file_it_had_replaced(tmp_path, monkeypatch):
    from tsm300 import store

    measurement = parse_message(BOTH)
    sheet = store.save_result(measurement, tmp_path, WHEN, note="처음 메모")
    before = files_in(tmp_path)
    real_replace = store.os.replace

    def record_held(source, target):
        if str(target).endswith(".json") and "earlier" not in Path(target).name and "earlier" not in Path(source).name:
            raise PermissionError(13, "Access is denied")
        real_replace(source, target)

    monkeypatch.setattr(store.os, "replace", record_held)
    monkeypatch.setattr(store, "_REPLACE_PAUSE", 0)
    with pytest.raises(PermissionError):
        store.save_result(measurement, tmp_path, WHEN, note="고친 메모", replacing=sheet)     # the sheet was already placed
    with pytest.raises(PermissionError):
        store.save_result(measurement, tmp_path / "new", WHEN)                                 # as was a new result's

    assert files_in(tmp_path) == before
    assert files_in(tmp_path / "new") == {}


def test_decode_command_says_when_the_sheet_will_carry_no_clinic_name(tmp_path, capsys):
    from tsm300 import settings as settings_file
    from tsm300.settings import Settings

    cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path / "a")])
    assert "결과지에 병원 이름이 인쇄되지 않습니다" in capsys.readouterr().out

    settings_file.save(Settings(clinic_name="바른이비인후과의원"))
    cli.main(["decode", str(DATA / "left_only.bin"), "--out", str(tmp_path / "b")])
    assert "병원 이름" not in capsys.readouterr().out
