"""The audiometer on USB, opened the way the maker's program opens it (Windows only).

The maker's driver (MRSResonanceDriver.inf) puts the audiometer on Windows' own WinUSB driver
and registers the interface GUID {CF6C4074-E7E9-4E0A-B965-0D7D7F2E7DD4}. The maker's program
finds it by that GUID (WinUSBNet's USBDevice.GetDevices) and talks over interface 0's first
IN and OUT pipes with a 500 ms read timeout. This does the same through winusb.dll and
setupapi.dll with ctypes, so nothing else has to be installed. Only one program can hold the
device: while MDS has it open, opening it here fails, and the other way round.
"""

from __future__ import annotations

import ctypes
import sys
import uuid

INTERFACE_GUID = "{CF6C4074-E7E9-4E0A-B965-0D7D7F2E7DD4}"
READ_TIMEOUT_MS = 500

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PIPE_TRANSFER_TIMEOUT = 0x03
ERROR_SEM_TIMEOUT = 121
ERROR_NO_MORE_ITEMS = 259
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
USB_ENDPOINT_DIRECTION_IN = 0x80
PIPE_TYPE_NAMES = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}


class WinUsbError(RuntimeError):
    pass


class NoAudiometer(WinUsbError):
    """Windows sees no audiometer on USB (it may appear once Connect to PC is pressed on it)."""


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        u = uuid.UUID(text)
        g = cls(u.fields[0], u.fields[1], u.fields[2])
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("InterfaceClassGuid", GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("bLength", ctypes.c_ubyte), ("bDescriptorType", ctypes.c_ubyte), ("bInterfaceNumber", ctypes.c_ubyte),
                ("bAlternateSetting", ctypes.c_ubyte), ("bNumEndpoints", ctypes.c_ubyte),
                ("bInterfaceClass", ctypes.c_ubyte), ("bInterfaceSubClass", ctypes.c_ubyte),
                ("bInterfaceProtocol", ctypes.c_ubyte), ("iInterface", ctypes.c_ubyte)]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [("PipeType", ctypes.c_int), ("PipeId", ctypes.c_ubyte), ("MaximumPacketSize", ctypes.c_ushort),
                ("Interval", ctypes.c_ubyte)]


def _libraries():
    if sys.platform != "win32":
        raise WinUsbError("USB 연결은 Windows 에서만 됩니다")
    from ctypes import wintypes as w
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    winusb = ctypes.WinDLL("winusb", use_last_error=True)
    HDEVINFO = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.restype = HDEVINFO
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.restype = w.BOOL
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [HDEVINFO, ctypes.c_void_p, ctypes.POINTER(GUID), w.DWORD,
                                                     ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = w.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [HDEVINFO, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                          ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = w.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [HDEVINFO]
    kernel32.CreateFileW.restype = w.HANDLE
    kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel32.CloseHandle.restype = w.BOOL
    kernel32.CloseHandle.argtypes = [w.HANDLE]
    winusb.WinUsb_Initialize.restype = w.BOOL
    winusb.WinUsb_Initialize.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    winusb.WinUsb_Free.restype = w.BOOL
    winusb.WinUsb_Free.argtypes = [ctypes.c_void_p]
    winusb.WinUsb_QueryInterfaceSettings.restype = w.BOOL
    winusb.WinUsb_QueryInterfaceSettings.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)]
    winusb.WinUsb_QueryPipe.restype = w.BOOL
    winusb.WinUsb_QueryPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.POINTER(WINUSB_PIPE_INFORMATION)]
    winusb.WinUsb_SetPipePolicy.restype = w.BOOL
    winusb.WinUsb_SetPipePolicy.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, w.ULONG, w.ULONG, ctypes.c_void_p]
    winusb.WinUsb_ReadPipe.restype = w.BOOL
    winusb.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG), ctypes.c_void_p]
    winusb.WinUsb_WritePipe.restype = w.BOOL
    winusb.WinUsb_WritePipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG), ctypes.c_void_p]
    return setupapi, kernel32, winusb


def device_paths(guid: str = INTERFACE_GUID) -> list[str]:
    """The device paths of every present device that offers ``guid``."""
    from ctypes import wintypes as w
    setupapi, _, _ = _libraries()
    g = GUID.parse(guid)
    info = setupapi.SetupDiGetClassDevsW(ctypes.byref(g), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if info in (None, INVALID_HANDLE_VALUE):
        raise WinUsbError(f"장치 목록을 읽지 못했습니다 ({ctypes.get_last_error()})")
    paths = []
    try:
        index = 0
        while True:
            data = SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(g), index, ctypes.byref(data)):
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                raise WinUsbError(f"장치를 읽지 못했습니다 ({ctypes.get_last_error()})")
            index += 1
            needed = w.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), None, 0, ctypes.byref(needed), None)
            buffer = ctypes.create_string_buffer(needed.value + 2)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W: a DWORD cbSize then the path. cbSize is the struct's own
            # size, which alignment makes 8 in a 64-bit process and 6 in a 32-bit one.
            ctypes.c_uint32.from_buffer(buffer).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), buffer, needed, None, None):
                raise WinUsbError(f"장치 경로를 읽지 못했습니다 ({ctypes.get_last_error()})")
            paths.append(ctypes.wstring_at(ctypes.addressof(buffer) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


class WinUsbLink:
    """One open audiometer. ``read`` returns b"" when nothing came within the timeout."""

    def __init__(self, path: str, timeout_ms: int = READ_TIMEOUT_MS) -> None:
        from ctypes import wintypes as w
        self._w = w
        self._setupapi, self._kernel32, self._winusb = _libraries()
        self.name = "USB"
        self.path = path
        self._file = self._kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                                None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, None)
        if self._file in (None, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            if error in (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION):
                raise WinUsbError("청력검사기를 다른 프로그램이 쓰고 있습니다. MDS 가 켜져 있으면 닫아 주세요")
            raise WinUsbError(f"청력검사기를 열지 못했습니다 (Windows 오류 {error})")
        self._handle = ctypes.c_void_p()
        if not self._winusb.WinUsb_Initialize(self._file, ctypes.byref(self._handle)):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(self._file)
            raise WinUsbError(f"청력검사기 USB 를 준비하지 못했습니다 (Windows 오류 {error})")
        try:
            self.pipes = self._query_pipes()
            ins = [p for p in self.pipes if p.PipeId & USB_ENDPOINT_DIRECTION_IN]
            outs = [p for p in self.pipes if not p.PipeId & USB_ENDPOINT_DIRECTION_IN]
            if not ins or not outs:
                raise WinUsbError("청력검사기의 USB 통로를 찾지 못했습니다")
            self._in, self._out = ins[0], outs[0]
            timeout = w.ULONG(timeout_ms)
            for pipe in (self._in, self._out):
                self._winusb.WinUsb_SetPipePolicy(self._handle, pipe.PipeId, PIPE_TRANSFER_TIMEOUT,
                                                  ctypes.sizeof(timeout), ctypes.byref(timeout))
            size = max(4096, self._in.MaximumPacketSize * 8)
            self._buffer = ctypes.create_string_buffer(size - size % max(1, self._in.MaximumPacketSize))
        except Exception:
            self.close()
            raise

    def _query_pipes(self) -> list[WINUSB_PIPE_INFORMATION]:
        descriptor = USB_INTERFACE_DESCRIPTOR()
        if not self._winusb.WinUsb_QueryInterfaceSettings(self._handle, 0, ctypes.byref(descriptor)):
            raise WinUsbError(f"USB 설정을 읽지 못했습니다 ({ctypes.get_last_error()})")
        pipes = []
        for index in range(descriptor.bNumEndpoints):
            info = WINUSB_PIPE_INFORMATION()
            if self._winusb.WinUsb_QueryPipe(self._handle, 0, index, ctypes.byref(info)):
                pipes.append(info)
        return pipes

    def describe(self) -> str:
        return ", ".join(f"0x{p.PipeId:02X} {PIPE_TYPE_NAMES.get(p.PipeType, p.PipeType)} {p.MaximumPacketSize}B"
                         for p in self.pipes)

    def read(self) -> bytes:
        done = self._w.ULONG(0)
        if self._winusb.WinUsb_ReadPipe(self._handle, self._in.PipeId, self._buffer, len(self._buffer),
                                        ctypes.byref(done), None):
            return self._buffer.raw[:done.value]
        error = ctypes.get_last_error()
        if error == ERROR_SEM_TIMEOUT:
            return b""
        raise WinUsbError(f"청력검사기에서 읽지 못했습니다 (Windows 오류 {error}) — USB 선을 확인해 주세요")

    def write(self, data: bytes) -> None:
        done = self._w.ULONG(0)
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        if not self._winusb.WinUsb_WritePipe(self._handle, self._out.PipeId, buffer, len(data), ctypes.byref(done), None):
            raise WinUsbError(f"청력검사기로 보내지 못했습니다 (Windows 오류 {ctypes.get_last_error()})")

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._winusb.WinUsb_Free(self._handle)
            self._handle = None
        if getattr(self, "_file", None) not in (None, INVALID_HANDLE_VALUE):
            self._kernel32.CloseHandle(self._file)
            self._file = None


def open_audiometer() -> WinUsbLink:
    """The first Resonance audiometer on USB."""
    paths = device_paths()
    if not paths:
        raise NoAudiometer("USB 에 연결된 청력검사기가 없습니다. 선과 전원, 그리고 MRS 드라이버 설치를 확인해 주세요")
    return WinUsbLink(paths[0])


def main() -> None:
    """``python -m tsm300.hearing.winusb``: lists what Windows sees, for checking a PC."""
    found = device_paths()
    print(f"청력검사기(GUID {INTERFACE_GUID}) {len(found)}대")
    for path in found:
        print(" ", path)
        try:
            link = WinUsbLink(path)
            print("   통로:", link.describe())
            link.close()
        except WinUsbError as error:
            print("  ", error)
    usb_all = device_paths("{A5DCBF10-6530-11D2-901F-00C04FB951ED}")
    print(f"USB 장치 전체 {len(usb_all)}개 (목록 읽기 점검용)")


if __name__ == "__main__":
    main()
