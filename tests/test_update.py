"""Updating the single-file program from the releases page, against a made-up page on this machine."""

import subprocess
import sys
from pathlib import Path

import pytest

from fakes import ReleaseSite
from tsm300 import update
from tsm300.update import Release, UpdateError


@pytest.fixture
def site():
    site = ReleaseSite()
    yield site
    site.close()


@pytest.fixture
def program(tmp_path):
    program = tmp_path / "TSM300.exe"
    program.write_bytes(b"the program in use")
    return program


def trying(says="99.0.0", code=0):
    """Stands in for starting the downloaded file, which answers with its version."""
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, code, stdout=(says + "\r\n").encode(), stderr=b"")

    run.calls = calls
    return run


def names(folder):
    return sorted(path.name for path in folder.iterdir())


def test_newest_release_is_found_by_following_the_latest_address(site):
    assert update.latest_release(site.url) == Release("99.0.0", f"{site.url}/download/v99.0.0/TSM300.exe")
    # Whether the request that follows the redirect is still a HEAD depends on the Python version.
    assert site.requests[0] == ("HEAD", "/releases/latest")
    assert [path for _, path in site.requests] == ["/releases/latest", "/releases/tag/v99.0.0"]


def test_a_page_without_releases_and_a_page_out_of_reach_are_said_plainly():
    empty = ReleaseSite(published=False)
    try:
        with pytest.raises(UpdateError, match="아직 올라와 있지 않습니다"):
            update.latest_release(empty.url)
    finally:
        empty.close()

    with pytest.raises(UpdateError, match="인터넷 연결을 확인"):
        update.latest_release("http://127.0.0.1:9/releases", timeout=2)


def test_versions_are_compared_as_numbers():
    assert update.is_newer("0.10.0", than="0.9.9")
    assert update.is_newer("1.0", than="0.9.1")
    assert not update.is_newer("0.9.1", than="0.9.1")
    assert not update.is_newer("0.9.0", than="0.9.1")


def test_install_puts_the_tried_download_in_the_programs_place(site, program):
    run, progress = trying(), []

    update.install(update.latest_release(site.url), program, lambda received, total: progress.append((received, total)), run)

    assert program.read_bytes() == ReleaseSite.PROGRAM
    assert program.with_name("TSM300.exe.old").read_bytes() == b"the program in use"
    assert names(program.parent) == ["TSM300.exe", "TSM300.exe.old"]
    ((command, options),) = run.calls
    assert command == [str(program.with_name("TSM300.new.exe")), "--version"]
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert update.RESTARTED not in options["env"]
    assert len(progress) > 1 and progress[-1] == (len(ReleaseSite.PROGRAM), len(ReleaseSite.PROGRAM))


@pytest.mark.parametrize("run", [trying(says="0.1.0"), trying(code=1), trying(says="")])
def test_a_download_that_is_not_the_version_expected_leaves_the_program_alone(site, program, run):
    with pytest.raises(UpdateError, match="v99.0.0 이 아닙니다"):
        update.install(update.latest_release(site.url), program, run=run)

    assert program.read_bytes() == b"the program in use"
    assert names(program.parent) == ["TSM300.exe"]


def test_a_download_that_cannot_be_started_leaves_the_program_alone(site, program):
    def blocked(command, **options):
        raise PermissionError("access denied")

    with pytest.raises(UpdateError, match="실행해 보지 못했습니다"):
        update.install(update.latest_release(site.url), program, run=blocked)

    assert names(program.parent) == ["TSM300.exe"]


def test_a_download_cut_short_is_not_even_tried(program):
    site, run = ReleaseSite(cut_short=True), trying()
    try:
        with pytest.raises(UpdateError, match="끊겼습니다|내려받지 못했습니다"):
            update.install(update.latest_release(site.url), program, run=run)
    finally:
        site.close()

    assert run.calls == []
    assert program.read_bytes() == b"the program in use"
    assert names(program.parent) == ["TSM300.exe"]


def test_what_an_update_left_behind_is_removed_at_the_next_start(program):
    program.with_name("TSM300.exe.old").write_bytes(b"the version before")
    program.with_name("TSM300.new.exe").write_bytes(b"half a download")

    update.clean_up(program)
    update.clean_up(program)  # nothing left to remove is not an error

    assert names(program.parent) == ["TSM300.exe"]


def test_restart_starts_the_program_with_the_same_options_and_a_clean_slate(program):
    started = []

    update.restart(program, ["--out", "D:\\검사결과"], start=lambda command, **options: started.append((command, options)))

    ((command, options),) = started
    assert command == [str(program), "--out", "D:\\검사결과"]
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert options["env"][update.RESTARTED] == "1"       # so that it waits for this program to finish closing
    assert options["stdin"] == subprocess.DEVNULL


def test_only_the_packaged_program_has_a_file_to_replace(monkeypatch):
    assert update.program_file() is None

    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert update.program_file() == Path(sys.executable)
