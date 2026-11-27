"""Reading the audiometer: what is sent, the byte log, and what the operator is told when it does not work."""

from __future__ import annotations

from datetime import datetime

import pytest

from hearing_fakes import Audiometer, Clock, Silent, Unplugged
from tsm300.hearing import device
from tsm300.hearing import protocol as p
from tsm300.hearing.samples import DEMO_SERIAL, tonal
from tsm300.hearing.session import fingerprint
from tsm300.hearing.simulator import Behaviour, SimulatedAudiometer

WHEN = datetime(2026, 11, 27, 10, 30, 5)


def fetch(audiometer: Audiometer, folder=None, **options):
    return device.fetch(audiometer.open, folder, now=lambda: WHEN, clock=audiometer.clock, **options)


def test_every_test_in_the_memory_is_read_under_its_device_patient(tmp_path):
    audiometer = Audiometer()
    counted = []

    fetched = fetch(audiometer, tmp_path, progress=counted.append)

    download = fetched.download
    assert download.serial == DEMO_SERIAL and download.finished
    assert [group.patient.number for group in download.groups] == ["1", "2", "3", "4"]
    assert len(download.tests) == 9 and counted[-1] == 9
    assert fetched.warnings == []


def test_only_commands_that_read_are_sent():
    audiometer = Audiometer()
    fetch(audiometer)
    device.check(audiometer.open, clock=audiometer.clock)

    sent = audiometer.sent()
    assert {command[4] for command in sent} <= p.READ_ONLY
    assert [command[4] for command in sent] == [p.SYNC, p.SYNC, p.GET_ALL, p.NACK, p.SYNC, p.NACK]
    assert not any(device_.refused for device_ in audiometer.devices)
    assert all(device_.closed for device_ in audiometer.devices)


def test_every_byte_both_ways_is_logged_with_the_time(tmp_path):
    audiometer = Audiometer()

    fetched = fetch(audiometer, tmp_path)

    assert fetched.log == tmp_path / "20261127_103005.txt"
    text = fetched.log.read_text(encoding="utf-8")
    assert text.startswith("2026-11-27 10:30:05.000  # TSM300 v")
    pieces = device.read_log(fetched.log)
    sent = b"".join(data for direction, data in pieces if direction == "PC>")
    received = b"".join(data for direction, data in pieces if direction == "<DEV")
    assert sent == b"".join(audiometer.sent())
    decoder = p.StreamDecoder()
    tests = [event for event in decoder.feed(received) if isinstance(event, (p.ToneAudiogram, p.SpeechAudiogram))]
    assert [fingerprint(test) for test in tests] == [fingerprint(test) for test in fetched.download.tests]
    assert "받음: 일련번호 R37A21C000777, 기기 환자 4명, 검사 9건, 끝 신호 있음" in text
    assert text.rstrip().endswith("# 닫음")

    second = fetch(audiometer, tmp_path)
    assert second.log == tmp_path / "20261127_103005_2.txt"   # one file per reading, never written over


def test_a_log_that_cannot_be_written_does_not_stop_the_reading(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("")
    fetched = fetch(Audiometer(), blocked / "logs")
    assert fetched.log is None and len(fetched.download.tests) == 9


@pytest.mark.parametrize("behaviour", [
    Behaviour(answer_identification="ACK"),
    Behaviour(answer_identification="KI+ACK"),
    Behaviour(ack_at_end=False),
    Behaviour(chunk=7),
    Behaviour(chunk=512),
    Behaviour(noise_before=b"\x00\x00\xff"),
    Behaviour(ignore_length_field=False),
    Behaviour(unasked_test=True),
])
def test_other_ways_a_device_might_answer_are_coped_with(behaviour):
    fetched = fetch(Audiometer(behaviour))

    assert len({fingerprint(test) for test in fetched.download.tests}) == 9
    if not behaviour.ack_at_end:
        assert fetched.warnings == [device.NOT_FINISHED]


def test_a_device_that_does_not_answer_is_reported(tmp_path):
    clock = Clock()
    silent = Silent(clock)

    with pytest.raises(device.DeviceError) as failed:
        device.fetch(lambda: silent, tmp_path, now=lambda: WHEN, clock=clock)

    assert str(failed.value).startswith(device.NO_ANSWER)
    assert failed.value.log is not None and f"주고받은 기록: {failed.value.log}" in str(failed.value)
    assert silent.closed and all(command[4] in p.READ_ONLY for command in silent.written)
    with pytest.raises(device.DeviceError, match="답하지 않습니다"):
        device.check(lambda: Silent(clock), clock=clock)


@pytest.mark.parametrize("message", [
    "USB 에 연결된 청력검사기가 없습니다. 선과 전원, 그리고 MRS 드라이버 설치를 확인해 주세요",
    "청력검사기를 다른 프로그램이 쓰고 있습니다. MDS 가 켜져 있으면 닫아 주세요",
])
def test_no_device_and_a_device_held_by_another_program_say_what_to_do(tmp_path, message):
    def opener():
        raise device.DeviceError(message)

    with pytest.raises(device.DeviceError) as failed:
        device.fetch(opener, tmp_path, now=lambda: WHEN)

    assert str(failed.value).startswith(message)
    assert f"열지 못함: {message}" in failed.value.log.read_text(encoding="utf-8")


def test_a_link_that_fails_to_open_for_another_reason_is_reported_too():
    def opener():
        raise OSError("장치가 응답하지 않음")

    with pytest.raises(device.DeviceError, match="청력검사기를 열지 못했습니다: 장치가 응답하지 않음"):
        device.fetch(opener)


def test_a_cable_pulled_half_way_is_reported(tmp_path):
    audiometer = Audiometer()
    links = []

    def opener():
        link = Unplugged(SimulatedAudiometer(audiometer.memory), audiometer.clock, reads=8)
        links.append(link)
        return link

    with pytest.raises(device.DeviceError, match="주고받다가 멈췄습니다.*USB 선을 확인"):
        device.fetch(opener, tmp_path, now=lambda: WHEN, clock=audiometer.clock)
    assert links[0].device.closed
    assert "# 멈춤: RuntimeError" in next(tmp_path.glob("*.txt")).read_text(encoding="utf-8")


def test_usb_is_only_for_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(device.DeviceError, match="Windows 에서만"):
        device.open_usb()
    with pytest.raises(device.DeviceError, match="Windows 에서만"):
        device.usb_devices()


def test_connection_check_only_asks_who_it_is():
    audiometer = Audiometer()
    assert device.check(audiometer.open, clock=audiometer.clock) == \
        f"연결됨: MRS Resonance R37A (일련번호 {DEMO_SERIAL})"
    acks_only = Audiometer(Behaviour(answer_identification="ACK"))
    assert device.check(acks_only.open, clock=acks_only.clock) == "연결됨: 일련번호는 알려 주지 않았습니다 (통신 판 3)"


def test_the_practice_device_keeps_its_memory_between_connections():
    first, second = device.practice_device(), device.practice_device()
    assert first.memory is second.memory
    link = device.opener_for(device.SIMULATOR)()
    assert isinstance(link, device.PacedLink)
    assert device.opener_for(device.USB) is device.open_usb


def test_saved_tests_are_remembered_and_a_damaged_file_forgets_nothing_saved_later(tmp_path):
    seen = device.SeenTests(tmp_path / "settings" / device.SEEN_FILE)
    assert seen.load() == set()

    seen.add(["a", "b"], "20261127_103005.jpg", WHEN)
    seen.add(["b", "c"], "other.jpg", WHEN)

    assert seen.load() == {"a", "b", "c"}
    saved = (tmp_path / "settings" / device.SEEN_FILE).read_text(encoding="utf-8")
    assert '"sheet": "20261127_103005.jpg"' in saved

    seen.path.write_text("{ not json", encoding="utf-8")
    assert seen.load() == set()
    seen.add(["d"])
    assert seen.load() == {"d"}


def test_a_new_test_on_the_device_is_told_apart_from_those_saved_before():
    audiometer = Audiometer()
    seen = {fingerprint(test) for test in fetch(audiometer).download.tests}
    audiometer.memory.add(5, "견본", "마", "01/01/1990", "F", [tonal({"Ac R": ({1000: 25}, None)}, serial=DEMO_SERIAL)])

    new = fetch(audiometer).download.new_since(seen)

    assert len(new) == 1 and new[0][0].patient.number == "5"
