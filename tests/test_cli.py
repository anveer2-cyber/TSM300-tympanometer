"""How the program is started: with or without output streams, and where the window keeps results."""

import sys

from tsm300 import cli


def test_commands_run_where_there_are_no_output_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)  # as in a program started without a console
    monkeypatch.setattr(sys, "stderr", None)

    assert cli.main(["ports"]) == 0


def test_window_is_given_the_results_folder(tmp_path, monkeypatch):
    import tsm300.app as app

    folders = []
    monkeypatch.setattr(app, "run", lambda args: folders.append(args.out) or 0)
    monkeypatch.setenv("TSM300_DIR", str(tmp_path))

    assert cli.main([]) == 0
    assert folders == ["results"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli.main([]) == 0
    assert folders[-1] == str(tmp_path / "results")

    assert cli.main(["--out", "elsewhere"]) == 0
    assert folders[-1] == "elsewhere"


def test_update_command_tells_the_versions_and_installs_only_what_is_newer(tmp_path, monkeypatch, capsys):
    from tsm300 import __version__, update

    program, installed = tmp_path / "TSM300.exe", []
    latest = [update.Release("99.0.0", "http://unused")]
    monkeypatch.setattr(update, "latest_release", lambda: latest[0])
    monkeypatch.setattr(update, "program_file", lambda: program)
    monkeypatch.setattr(update, "install", lambda release, target: installed.append((release.version, target)))

    assert cli.main(["update", "--check"]) == 0
    assert f"지금 버전: {__version__}" in (said := capsys.readouterr().out) and "올라와 있는 버전: 99.0.0" in said
    assert installed == []

    assert cli.main(["update"]) == 0
    assert installed == [("99.0.0", program)]

    latest[0] = update.Release(__version__, "http://unused")
    assert cli.main(["update"]) == 0
    assert "이미 최신 버전입니다" in capsys.readouterr().out
    assert len(installed) == 1

    assert cli.main(["update", "--again"]) == 0
    assert installed[-1] == (__version__, program)

    started = []
    monkeypatch.setattr(update, "restart", lambda target, arguments: started.append((target, arguments)))
    assert cli.main(["update", "--again", "--restart"]) == 0
    assert started == [(program, [])]


def test_update_command_says_what_went_wrong_and_how_a_python_install_updates(monkeypatch, capsys):
    from tsm300 import update

    monkeypatch.setattr(update, "latest_release", lambda: update.Release("99.0.0", "http://unused"))
    assert cli.main(["update"]) == 1
    assert "pip install --upgrade" in capsys.readouterr().out

    def unreachable():
        raise update.UpdateError("새 버전이 있는지 확인하지 못했습니다.")

    monkeypatch.setattr(update, "latest_release", unreachable)
    assert cli.main(["update"]) == 1
    assert "확인하지 못했습니다" in capsys.readouterr().out
