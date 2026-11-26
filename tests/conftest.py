"""Shared test helpers."""

import pytest

try:
    import pty
except ImportError:  # Windows has no pseudo-terminals
    pty = None

needs_pty = pytest.mark.skipif(pty is None, reason="needs a pseudo-terminal to stand in for the serial port")
