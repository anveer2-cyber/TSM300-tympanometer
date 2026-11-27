"""Shared test helpers."""

import pytest

try:
    import pty
except ImportError:  # Windows has no pseudo-terminals
    pty = None

needs_pty = pytest.mark.skipif(pty is None, reason="needs a pseudo-terminal to stand in for the serial port")


@pytest.fixture(autouse=True)
def own_settings_folder(tmp_path_factory, monkeypatch):
    """Every test gets a settings folder of its own, so none reads or writes this machine's real one."""
    monkeypatch.setenv("TSM300_DIR", str(tmp_path_factory.mktemp("settings")))
