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
