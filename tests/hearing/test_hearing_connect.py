"""Connect to PC: the operator may press it on the audiometer before or after [받기], and the
device may then wait to be asked, send its tests by itself, or wait for an answer to each test.

The maker's program's own rules are what is followed (docs/hearing/통신_절차.md §5): it asks
again and again, takes tests sent unasked without asking for more, and answers over USB with
KC 07 each test that comes with no device patient before it.
"""

from __future__ import annotations

import pytest

from hearing_fakes import Bench
from tsm300.hearing import device
from tsm300.hearing import protocol as p
from tsm300.hearing.samples import speech, tonal
from tsm300.hearing.session import DEVICE_WAIT, Session
from tsm300.hearing.simulator import Behaviour, Memory, SimulatedAudiometer

R27A = "R27A20C000456"


def r27a_screen() -> Memory:
    """What an R27A holds until it is switched off: the curves on its screen, with no patient."""
    memory = Memory(serial=R27A)
    memory.add_without_patient([
        tonal({"Ac R": ({500: 30, 1000: 35, 2000: 40, 4000: 50}, {}),
               "Ac L": ({500: 20, 1000: 25, 2000: 30, 4000: 45}, {})}, serial=R27A),
        tonal({"Spkr R": ({500: 45, 1000: 50, 2000: 55, 4000: 60}, {}),
               "SpkrA R": ({500: 25, 1000: 30, 2000: 35, 4000: 40}, {})}, serial=R27A),
        speech({"Ac R": [(50, 60, 245), (60, 84, 245)], "Ac L": [(40, 72, 245), (50, 92, 245)]},
               srt={"Ac R": 40, "Ac L": 30}, serial=R27A),
    ])
    return memory


def commands(bench: Bench) -> list[int]:
    return [command[4] for command in bench.device.received]


def only_reading(bench: Bench) -> bool:
    return all(number in p.READ_ONLY for number in commands(bench)) and not bench.device.refused


@pytest.mark.parametrize("press_at", [0.0, 4.0, 12.5, 27.0])
def test_connect_to_pc_pressed_before_or_after_receiving(press_at):
    bench = Bench(Behaviour(needs_button=True), r27a_screen(), press_at=press_at)

    fetched = bench.fetch()

    assert len(fetched.download.tests) == 3 and fetched.download.finished and not fetched.warnings
    assert fetched.download.serial == R27A
    assert only_reading(bench)
    if press_at:
        # the operator was told what to press, with the seconds left counting down
        assert bench.waited and bench.waited == sorted(bench.waited, reverse=True)
        assert bench.waited[0] <= DEVICE_WAIT and bench.waited[-1] >= DEVICE_WAIT - press_at - 2
    else:
        assert bench.waited == []


def test_an_audiometer_that_shows_up_on_usb_only_once_pressed():
    bench = Bench(Behaviour(needs_button=True), r27a_screen(), press_at=7.0, on_usb=False)

    fetched = bench.fetch()

    assert len(fetched.download.tests) == 3 and bench.opened == 1
    assert bench.waited and bench.waited[0] == 30


@pytest.mark.parametrize("press_at", [0.0, 6.0])
def test_tests_the_audiometer_sends_by_itself_are_taken_without_asking_for_more(press_at):
    bench = Bench(Behaviour(pushes=True, waits_for_ack=True), r27a_screen(), press_at=press_at)

    fetched = bench.fetch()

    assert len(fetched.download.tests) == 3 and fetched.download.pushed
    assert fetched.warnings == []  # going quiet is how such a download ends
    assert p.GET_ALL not in commands(bench)  # as the maker's program: what came is what there is
    assert bench.device.test_acks >= 3 and only_reading(bench)


def test_each_test_without_a_patient_is_answered_so_that_the_next_one_comes():
    bench = Bench(Behaviour(waits_for_ack=True), r27a_screen())

    fetched = bench.fetch()

    assert len(fetched.download.tests) == 3 and fetched.download.finished
    assert bench.device.test_acks == 3 + 1  # one for each test, and the goodbye

    # Without the answer such a device stops after its first test.
    clock = Bench(Behaviour(waits_for_ack=True), r27a_screen()).clock
    silent_device = SimulatedAudiometer(r27a_screen(), Behaviour(waits_for_ack=True))
    from hearing_fakes import Timed
    session = Session(Timed(silent_device, clock), clock=clock, acknowledge=False)
    session.connect()
    assert len(session.download().tests) == 1


def test_tests_under_a_device_patient_are_not_answered_one_by_one():
    bench = Bench(Behaviour())  # the practice memory: every test under a device patient

    fetched = bench.fetch()

    assert len(fetched.download.tests) == 9
    assert bench.device.test_acks == 1  # only the goodbye, as the maker's program


def test_connect_to_pc_never_pressed():
    bench = Bench(Behaviour(needs_button=True), r27a_screen(), press_at=None)

    with pytest.raises(device.DeviceError) as failed:
        bench.fetch()

    assert str(failed.value).startswith(device.NO_ANSWER) and "[Connect to PC]" in str(failed.value)
    assert DEVICE_WAIT <= bench.clock.now <= DEVICE_WAIT + 3
    assert bench.waited[-1] >= 1 and only_reading(bench)
    assert set(commands(bench)) == {p.SYNC, p.NACK}  # asking who it is, and the goodbye


def test_an_audiometer_never_seen_on_usb():
    bench = Bench(Behaviour(needs_button=True), r27a_screen(), press_at=None, on_usb=False)

    with pytest.raises(device.DeviceError) as failed:
        bench.fetch()

    assert str(failed.value).startswith(device.NO_DEVICE)
    assert DEVICE_WAIT <= bench.clock.now <= DEVICE_WAIT + 1 and bench.opened == 0


def test_the_connection_check_waits_too_and_takes_nothing_as_delivered():
    answering = Bench(Behaviour(needs_button=True), r27a_screen(), press_at=5.0)
    assert answering.check() == f"연결됨: MRS Resonance R27A (일련번호 {R27A})" and answering.waited

    pushing = Bench(Behaviour(pushes=True, waits_for_ack=True), r27a_screen(), press_at=3.0)
    said = pushing.check()
    assert "기기가 묻기 전에 검사를 보내 왔습니다" in said and "[Connect to PC]를 다시 누른 뒤" in said
    assert pushing.device.test_acks == 0  # no answer and no goodbye: nothing reads as delivered

    pushing.device.press()  # pressed again, as it says
    fetched = pushing.fetch()
    assert len(fetched.download.tests) == 3 and fetched.download.pushed


def test_a_device_that_answers_with_an_ack_only_is_not_kept_waiting():
    bench = Bench(Behaviour(answer_identification="ACK"), r27a_screen())

    fetched = bench.fetch()

    assert len(fetched.download.tests) == 3 and bench.waited == [] and bench.clock.now < 10
