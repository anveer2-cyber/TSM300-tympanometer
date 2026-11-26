"""The program's icon and a desktop shortcut that starts it without a console window."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

ICON = Path(__file__).parent / "assets" / "tsm300.ico"
SHORTCUT_NAME = "TSM-300 임피던스 검사"
APP_ID = "TSM300.Tympanometry"

_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_SHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_CLSCTX_INPROC_SERVER = 1

# Positions in the COM method tables, counted from IUnknown's QueryInterface at 0.
_QUERY_INTERFACE, _RELEASE = 0, 2
_SET_DESCRIPTION, _SET_WORKING_DIRECTORY, _SET_ARGUMENTS, _SET_ICON_LOCATION, _SET_PATH = 7, 9, 11, 17, 20
_PERSIST_FILE_SAVE = 6


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_ulong), ("data2", ctypes.c_ushort), ("data3", ctypes.c_ushort),
                ("data4", ctypes.c_ubyte * 8)]


def windowless_python() -> Path:
    """The interpreter that runs without opening a console, where the installation has one."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def create_shortcut(working_directory: Path, folder: Path | None = None) -> Path:
    """Creates the shortcut on the desktop, or in ``folder``, and returns its path.

    The program started from it keeps its results under ``working_directory``.
    The link is written through the shell's Unicode interface, because the scripting
    one (WScript.Shell) loses any character outside the system's ANSI code page.
    """
    if sys.platform != "win32":
        raise RuntimeError("바로가기는 Windows 에서만 만들 수 있습니다.")
    ole32 = ctypes.OleDLL("ole32")
    initialised = ctypes.windll.ole32.CoInitialize(None) >= 0
    try:
        path = (folder or _desktop(ole32)) / f"{SHORTCUT_NAME}.lnk"
        link = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(_guid(ole32, _CLSID_SHELL_LINK)), None, _CLSCTX_INPROC_SERVER,
                               ctypes.byref(_guid(ole32, _IID_SHELL_LINK_W)), ctypes.byref(link))
        try:
            _call(link, _SET_PATH, ctypes.c_wchar_p)(str(windowless_python()))
            _call(link, _SET_ARGUMENTS, ctypes.c_wchar_p)("-m tsm300")
            _call(link, _SET_WORKING_DIRECTORY, ctypes.c_wchar_p)(str(working_directory))
            _call(link, _SET_ICON_LOCATION, ctypes.c_wchar_p, ctypes.c_int)(str(ICON), 0)
            _call(link, _SET_DESCRIPTION, ctypes.c_wchar_p)(SHORTCUT_NAME)
            file = ctypes.c_void_p()
            _call(link, _QUERY_INTERFACE, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_guid(ole32, _IID_PERSIST_FILE)), ctypes.byref(file))
            try:
                _call(file, _PERSIST_FILE_SAVE, ctypes.c_wchar_p, ctypes.c_int)(str(path), 1)
            finally:
                _release(file)
        finally:
            _release(link)
    except OSError as error:
        raise RuntimeError(f"바로가기를 만들지 못했습니다: {error}") from None
    finally:
        if initialised:
            ctypes.windll.ole32.CoUninitialize()
    return path


def claim_taskbar_identity() -> None:
    """Makes Windows show this program's own icon on the taskbar instead of Python's."""
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def _guid(ole32, text: str) -> _Guid:
    guid = _Guid()
    ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _call(pointer: ctypes.c_void_p, index: int, *argument_types):
    """The COM method at ``index`` of the object's table; a failure HRESULT raises OSError."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argument_types)(table[index])
    return lambda *arguments: function(pointer, *arguments)


def _release(pointer: ctypes.c_void_p) -> None:
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[_RELEASE])(pointer)


def _desktop(ole32) -> Path:
    """The user's desktop folder, wherever Windows or OneDrive has put it."""
    shell32 = ctypes.OleDLL("shell32")
    found = ctypes.c_void_p()
    shell32.SHGetKnownFolderPath(ctypes.byref(_guid(ole32, _FOLDERID_DESKTOP)), 0, None, ctypes.byref(found))
    try:
        return Path(ctypes.wstring_at(found.value))
    finally:
        ctypes.windll.ole32.CoTaskMemFree(found)
