"""The operator's window: choose or type the patient, receive results, save and send the result sheet."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import serial
from PySide6.QtCore import QDateTime, QEvent, QLockFile, QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPageLayout, QPageSize, QPainter, QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import __version__, links
from . import update as update_module
from . import settings as settings_file
from .archive import archive_directory, file_result, filed_sheet, refresh_copy
from .clinic import Clinic
from .console import available_ports, open_port
from .hearing import device as hearing_device
from .hearing.editor import Edited, HearingEditor
from .hearing.record import HearingResult, MergeError, Part, can_be_aided, describe, device_birth_date, type_of
from .hearing.session import fingerprint
from .listen import RETRY_AFTER, open_file, read_events
from .patient import Order, Patient, PatientSource, parse_birth_date
from .protocol import Corrupt, Measurement, PowerOn
from .records import EXAM_NAMES, Records, exams_of, records_from_settings, send_sheet, tested_on
from .report import prepare, wrap_note
from .settings import Settings
from .shortcut import ICON, claim_taskbar_identity
from .store import load_result, note_in_record, save_result

NO_SOURCE_NOTE = "차트번호 조회와 오늘 검사 오더 목록은 [설정]에서 환자 조회를 연결하면 쓸 수 있습니다. 지금은 직접 입력해 주세요."
WAITING = "검사 결과를 기다리는 중입니다. 검사 후 장비의 → 키를 누르세요."
SAVE_LABEL = "이 환자 정보로 저장"
REPLACE_LABEL = "직전 결과지의 환자 바꾸기..."
EDIT_LABEL = "검사일시 · 메모 저장"
OUTPUT_LABEL = "출력 (인쇄 + 보내기 + 보관)"
OUTPUT_LABEL_WITHOUT_SENDING = "출력 (인쇄 + 보관)"
EDITED_AFTER_SENDING = "edited"
ORDER_COLUMNS = ("접수", "차트번호", "이름", "나이·성별", "진료실", "검사")
ORDERS_REFRESH_MS = 30_000
EARLIEST_TEST = datetime(2000, 1, 1)
PORT_SEARCH_MS = 3_000
DONE_INK = QColor("#9a9a9a")
NO_PORT = "시리얼 포트를 찾지 못했습니다. USB-시리얼 케이블을 꽂으면 자동으로 연결합니다."
NO_CLINIC_HINT = "결과지에 인쇄할 병원 이름이 비어 있습니다. [설정] → 병원 정보에서 넣어 주세요."
ALREADY_RUNNING = "TSM300 이 이미 켜져 있습니다.\n작업 표시줄에서 열려 있는 창을 찾아 주세요."
HEARING_LABEL = "청력검사기에서 받기"
WAITING_WITH_HEARING = ("검사 결과를 기다리는 중입니다. 임피던스는 검사 후 장비의 → 키를, "
                        "청력검사는 검사를 마친 뒤 [청력검사기에서 받기]를 누르세요.")
HEARING_PROGRESS_MS = 200
VALUES_LABEL = "검사 값 고치기..."
MANUAL_LABEL = "직접 입력..."
REMARKS_LABEL = "특이사항 (결과지에 안 나옴)"

Background = Callable[[QObject, Callable[[], object], Callable[[object, "Exception | None"], None]], None]


def run_in_background(owner: QObject, work: Callable[[], object],
                      done: Callable[[object, Exception | None], None]) -> None:
    """Runs ``work`` on a thread, then calls ``done(result, error)`` on the window's thread.

    Nothing is called if ``owner`` is destroyed first, so a closing window is safe.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = work()
        except Exception as error:  # reported to ``done``; a failed lookup must not end the thread silently
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    timer = QTimer(owner)
    timer.setInterval(50)

    def poll() -> None:
        if thread.is_alive():
            return
        timer.stop()
        timer.deleteLater()
        done(box.get("result"), box.get("error"))

    timer.timeout.connect(poll)
    timer.start()


class SerialWorker(QThread):
    """Reads the port in the background, reconnecting whenever the connection drops."""

    status = Signal(str)
    received = Signal(object)

    def __init__(self, port: str, baud: int, port_opener=open_port, parent=None) -> None:
        super().__init__(parent)
        self._port = port
        self._baud = baud
        self._open = port_opener

    def run(self) -> None:
        while not self.isInterruptionRequested():
            try:
                ser = self._open(self._port, self._baud)
            except serial.SerialException as error:
                self.status.emit(f"{self._port} 포트를 열 수 없습니다. 다시 시도하는 중입니다. ({error})")
                self._pause()
                continue
            self.status.emit(f"{self._port} 연결됨")
            try:
                for event in read_events(ser, self.isInterruptionRequested):
                    self.received.emit(event)
            except serial.SerialException as error:
                self.status.emit(f"{self._port} 연결이 끊겼습니다. 다시 연결하는 중입니다. ({error})")
            finally:
                ser.close()
            self._pause()

    def _pause(self) -> None:
        for _ in range(int(RETRY_AFTER * 10)):
            if self.isInterruptionRequested():
                return
            self.msleep(100)


class NoWheel(QObject):
    """Keeps the mouse wheel from changing a box it merely passes over while the panel is scrolled.

    A test date or a patient's sex changed that way would be printed without anyone having meant it.
    """

    def eventFilter(self, watched, event) -> bool:
        return event.type() == QEvent.Type.Wheel


class SheetPreview(QLabel):
    """Shows the saved sheet scaled to whatever room the window gives it."""

    def __init__(self) -> None:
        super().__init__()
        self._sheet = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background: #e9e9e9; border-radius: 6px;")

    def show_sheet(self, path: Path | None) -> None:
        self._sheet = QPixmap(str(path)) if path else QPixmap()
        self._rescale()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._sheet.isNull():
            self.clear()
            return
        ratio = self.devicePixelRatioF()
        room = (self.size() - QSize(24, 24)) * ratio
        scaled = self._sheet.scaled(room, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self.setPixmap(scaled)


class PatientConfirm(QDialog):
    """Asks whether the patient found in the records is the one in the room. Enter accepts, Esc declines."""

    def __init__(self, parent: QWidget, patient: Patient, today) -> None:
        super().__init__(parent)
        self.setWindowTitle("환자 확인")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.addWidget(QLabel("이 환자가 맞습니까?"))
        name = QLabel(patient.name)
        name.setStyleSheet("font-size: 26pt; font-weight: 700;")
        layout.addWidget(name)
        chart = QLabel(f"차트번호 {patient.chart_no}")
        chart.setStyleSheet("font-size: 20pt;")
        layout.addWidget(chart)
        layout.addWidget(QLabel(_details(patient, today)))
        buttons = QDialogButtonBox()
        yes = buttons.addButton("맞습니다 (Enter)", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("아니요 (Esc)", QDialogButtonBox.ButtonRole.RejectRole)
        yes.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def keyPressEvent(self, event) -> None:
        # An Enter still held down from the chart number box must not answer the question by itself.
        if not event.isAutoRepeat():
            super().keyPressEvent(event)


class SettingsDialog(QDialog):
    """The clinic printed on the sheets, the device's port, where results are kept,
    where patients are looked up and where sheets are sent."""

    def __init__(self, parent: QWidget | None, settings: Settings, background: Background = run_in_background,
                 ports: Callable[[], list] = available_ports, fixed_folder: Path | None = None,
                 hearing_opener: Callable[[str], Callable[[], object]] = hearing_device.opener_for) -> None:
        super().__init__(parent)
        self._original = settings
        self._background = background
        self._ports = ports
        self._fixed_folder = fixed_folder
        self._hearing_opener = hearing_opener
        self.setWindowTitle("설정")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.tabs.addTab(self._clinic_tab(settings), "병원 정보")
        self.tabs.addTab(self._device_tab(settings, fixed_folder), "장비 · 저장")
        self.tabs.addTab(self._lookup_tab(settings), "환자 조회")
        self.tabs.addTab(self._capture_tab(settings), "상쾌한캡처")
        self.tabs.addTab(self._hearing_tab(settings), "청력검사기")
        self.tabs.addTab(self._about_tab(), "프로그램 정보")
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox()
        buttons.addButton("저장", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.via_combo.currentIndexChanged.connect(self._show_fields)
        self._show_fields()

    def _clinic_tab(self, settings: Settings) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.clinic_name_edit = QLineEdit(settings.clinic_name)
        self.clinic_name_edit.setPlaceholderText("예: ○○이비인후과의원")
        form.addRow("병원 이름", self.clinic_name_edit)
        self.clinic_address_edit = QLineEdit(settings.clinic_address)
        self.clinic_address_edit.setPlaceholderText("예: ○○시 ○○구 ○○로 123, 2층")
        form.addRow("주소", self.clinic_address_edit)
        self.clinic_phone_edit = QLineEdit(settings.clinic_phone)
        self.clinic_phone_edit.setPlaceholderText("예: 051-123-4567")
        form.addRow("전화번호", self.clinic_phone_edit)
        form.addRow(_note("결과지 맨 위에 병원 이름이, 맨 아래에 주소와 전화번호가 인쇄됩니다. 비워 둔 칸은 인쇄되지 않습니다.\n"
                          "화면에 떠 있는 결과지와 지금부터 받는 결과지에 적용됩니다. "
                          "이미 상쾌한캡처로 보낸 결과지는 그대로 둡니다."))
        return tab

    def _device_tab(self, settings: Settings, fixed_folder: Path | None) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.port_combo = QComboBox()
        self.rescan_button = QPushButton("포트 다시 찾기")
        self.rescan_button.clicked.connect(lambda: self._list_ports(self.port_combo.currentData()))
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, stretch=1)
        port_row.addWidget(self.rescan_button)
        form.addRow("장비 포트", port_row)
        self._list_ports(settings.port)
        form.addRow(_note("장비(TSM-300)의 SERIAL 단자와 연결된 포트입니다. 보통은 자동으로 두면 됩니다.\n"
                          "자동: 장비가 한 번 응답한 USB-시리얼 케이블을 기억해서, 다른 USB 단자에 꽂아도 그 케이블만 엽니다. "
                          "기억이 없을 때 케이블이 하나면 그것을 쓰고, 여러 개면 열지 않고 골라 달라고 합니다. "
                          "다른 장비의 포트를 잘못 여는 일이 없게 하려는 것입니다.\n"
                          "직접 고르면 그 포트만 씁니다. 케이블을 다른 USB 단자로 옮겨 이름이 바뀌면 다시 골라 주세요."))
        self.folder_edit = QLineEdit(settings.results_dir)
        self.folder_edit.setPlaceholderText(f"비워 두면: {settings_file.results_directory().resolve()}")
        self.browse_button = QPushButton("찾아보기...")
        self.browse_button.clicked.connect(lambda: self._browse(self.folder_edit, "저장 폴더"))
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, stretch=1)
        folder_row.addWidget(self.browse_button)
        form.addRow("저장 폴더", folder_row)
        note = ("받은 결과가 모두 자동으로 저장되는 폴더입니다. 결과지(JPG)와 로데이터가 받은 시각 이름으로 들어갑니다. "
                "환자 정보가 들어 있으니 다른 사람과 함께 쓰는 폴더는 피해 주세요.")
        if fixed_folder is not None:
            note += f"\n이번 실행은 시작할 때 명령(--out)으로 정한 폴더를 씁니다: {fixed_folder.resolve()}"
        form.addRow(_note(note))
        self.archive_edit = QLineEdit(settings.archive_dir)
        self.archive_edit.setPlaceholderText("비워 두면: 저장 폴더 안의 '보관' 폴더")
        self.archive_browse_button = QPushButton("찾아보기...")
        self.archive_browse_button.clicked.connect(lambda: self._browse(self.archive_edit, "보관 폴더"))
        archive_row = QHBoxLayout()
        archive_row.addWidget(self.archive_edit, stretch=1)
        archive_row.addWidget(self.archive_browse_button)
        form.addRow("보관 폴더", archive_row)
        form.addRow(_note("[출력]이나 [컴퓨터에 보관]을 누른 결과를 날짜별로 정리해 두는 폴더입니다. "
                          "예: 보관\\2026-11\\2026-11-26\\00512_홍길동_1430.jpg (같은 이름의 .json, .bin 이 로데이터)"))
        return tab

    def _lookup_tab(self, settings: Settings) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.via_combo = QComboBox()
        for label, value in (("쓰지 않음 (직접 입력만)", ""), ("매직차트 DB (이 PC 의 ODBC 연결)", "odbc"),
                             ("예진이 서버", "server")):
            self.via_combo.addItem(label, value)
        self.via_combo.setCurrentIndex(max(0, self.via_combo.findData(settings.emr_via)))
        form.addRow("방법", self.via_combo)
        self.dsn_edit = QLineEdit(settings.emr_dsn)
        self.dsn_edit.setPlaceholderText("비워 두면 이 PC 의 SQL Anywhere 연결을 찾아 씁니다")
        form.addRow("ODBC 이름", self.dsn_edit)
        self.server_edit = QLineEdit(settings.emr_server)
        self.server_edit.setPlaceholderText("예진이 서버 PC 주소, 예: 192.168.0.10 (예진이 v4.52.0 이상)")
        form.addRow("예진이 서버 주소", self.server_edit)
        self.token_edit = QLineEdit(settings.emr_token)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("서버 PC 콘솔에서 m + Enter 를 치면 나옵니다")
        form.addRow("예진이 서버 토큰", self.token_edit)
        self.lookup_test_button = QPushButton("연결 시험")
        self.lookup_test_button.clicked.connect(self.test_lookup)
        self.lookup_result = QLabel()
        self.lookup_result.setWordWrap(True)
        form.addRow(self.lookup_test_button, self.lookup_result)
        form.addRow(_note("전자차트에서 환자 정보와 오늘 검사 오더를 읽어 옵니다. 쓰지 않아도 환자 정보를 직접 넣어 쓸 수 있습니다."))
        return tab

    def _capture_tab(self, settings: Settings) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.cap_url_edit = QLineEdit(settings.cap_url)
        self.cap_url_edit.setPlaceholderText("진료실 PC 주소, 예: 192.168.0.10")
        form.addRow("받는 PC 주소", self.cap_url_edit)
        self.cap_key_edit = QLineEdit(settings.cap_key)
        self.cap_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cap_key_edit.setPlaceholderText("상쾌한캡처 설정의 [검사 장비 추가]에서 받은 키")
        form.addRow("키", self.cap_key_edit)
        self.cap_on_check = QCheckBox("결과지를 저장하면 바로 상쾌한캡처로 보내기")
        self.cap_on_check.setChecked(settings.cap_on)
        form.addRow(self.cap_on_check)
        self.capture_test_button = QPushButton("연결 시험")
        self.capture_test_button.clicked.connect(self.test_capture)
        self.capture_result = QLabel()
        self.capture_result.setWordWrap(True)
        form.addRow(self.capture_test_button, self.capture_result)
        form.addRow(_note("결과지를 상쾌한캡처 프로그램으로 보내는 기능입니다. 상쾌한캡처를 쓰지 않으면 비워 둡니다."))
        return tab

    def _hearing_tab(self, settings: Settings) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.hearing_on_check = QCheckBox("청력검사기(MRS Resonance) 쓰기")
        self.hearing_on_check.setChecked(settings.hearing_on)
        form.addRow(self.hearing_on_check)
        self.hearing_link_combo = QComboBox()
        for label, value in (("USB (자동으로 찾음)", hearing_device.USB),
                             ("모의 장비 (연습 · 시연용, 실제 청력검사기가 아님)", hearing_device.SIMULATOR)):
            self.hearing_link_combo.addItem(label, value)
        self.hearing_link_combo.setCurrentIndex(max(0, self.hearing_link_combo.findData(settings.hearing_link)))
        form.addRow("연결", self.hearing_link_combo)
        self.hearing_log_check = QCheckBox("주고받은 바이트 기록 남기기")
        self.hearing_log_check.setChecked(settings.hearing_log)
        form.addRow(self.hearing_log_check)
        self.hearing_test_button = QPushButton("연결 시험")
        self.hearing_test_button.clicked.connect(self.test_hearing)
        self.hearing_result = QLabel()
        self.hearing_result.setWordWrap(True)
        self.hearing_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.hearing_test_button, self.hearing_result)
        form.addRow(_note(
            "켜면 창 위쪽에 [청력검사기에서 받기]가 생깁니다. 순음 · 어음 · 음장 검사를 기기에 저장한 뒤 누르면 "
            "기기의 검사를 읽어 와 결과지로 만듭니다. 기기의 검사는 지우지 않고 읽기만 합니다.\n"
            "USB: 청력검사기를 USB 선으로 이 PC 에 연결합니다. 제조사 프로그램(MDS)이 켜져 있으면 연결되지 않으니 닫아 주세요. "
            "MRS 드라이버는 설치되어 있어야 합니다.\n"
            "모의 장비: 실제 기기 없이 받기를 연습하는 가짜 청력검사기입니다. 견본 환자의 검사가 들어 있습니다. "
            "실제 검사에는 쓰지 마세요.\n"
            f"바이트 기록: 저장 폴더 안의 '{hearing_device.LOG_FOLDER}' 폴더에 받을 때마다 파일이 하나씩 생깁니다. "
            "처음 몇 번은 켜 두세요. 문제가 생겼을 때 원인을 찾는 데 씁니다.\n"
            "[연결 시험]은 기기에게 누구인지 묻기만 합니다."))
        return tab

    def _about_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        folder = settings_file.settings_directory()
        about = QLabel(
            f"TSM300 v{__version__}\n"
            "Oscilla TSM-300 임피던스 청력검사기의 결과를 받아 결과지로 만드는 프로그램입니다. "
            "MRS Resonance 청력검사기의 순음 · 어음 · 음장 검사 결과도 받을 수 있습니다.\n\n"
            f"설정 파일: {folder / settings_file.FILE_NAME}\n"
            f"오류 기록: {folder / 'error.log'}\n\n"
            "이 프로그램은 다음 공개 소프트웨어를 사용합니다. 각 소프트웨어의 저작권과 사용 조건은 해당 프로젝트에 있습니다.\n"
            "Python (PSF License), Qt for Python · PySide6 (LGPL v3), Matplotlib (Matplotlib License), "
            "NumPy (BSD), Pillow (MIT-CMU), pySerial (BSD), pyodbc (MIT), PyInstaller (GPL v2, 실행 파일 예외 조항)")
        about.setWordWrap(True)
        about.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(about)
        layout.addStretch(1)
        return tab

    def settings(self) -> Settings:
        """The settings as edited in the dialog."""
        return replace(
            self._original,
            clinic_name=self.clinic_name_edit.text().strip(),
            clinic_address=self.clinic_address_edit.text().strip(),
            clinic_phone=self.clinic_phone_edit.text().strip(),
            port=self.port_combo.currentData() or "",
            results_dir=_full_path(self.folder_edit.text()),
            archive_dir=_full_path(self.archive_edit.text()),
            emr_via=self.via_combo.currentData(),
            emr_dsn=self.dsn_edit.text().strip(),
            emr_server=self.server_edit.text().strip(),
            emr_token=self.token_edit.text().strip(),
            cap_url=self.cap_url_edit.text().strip(),
            cap_key=self.cap_key_edit.text().strip(),
            cap_on=self.cap_on_check.isChecked(),
            hearing_on=self.hearing_on_check.isChecked(),
            hearing_link=self.hearing_link_combo.currentData(),
            hearing_log=self.hearing_log_check.isChecked(),
        )

    def accept(self) -> None:
        """Closes only when the results folder can really be written to; a sheet that cannot be saved is a lost test."""
        for name, box in (("저장 폴더", self.folder_edit), ("보관 폴더", self.archive_edit)):
            chosen = _full_path(box.text())
            problem = folder_problem(Path(chosen)) if chosen else ""
            if problem:
                self.tabs.setCurrentIndex(1)
                self.problem_label.setText(f"⚠ {name}에 쓸 수 없습니다: {problem}")
                return
        super().accept()

    def test_lookup(self) -> None:
        records = records_from_settings(self.settings())
        if records is None:
            self.lookup_result.setText("환자 조회를 쓰지 않는 설정입니다.")
            return
        self.lookup_result.setText("연결하는 중...")

        def done(result, error) -> None:
            records.close()
            self.lookup_result.setText(f"⚠ {error}" if error else f"✔ {result}")

        self._background(self, records.check, done)

    def test_capture(self) -> None:
        edited = self.settings()
        self.capture_result.setText("연결하는 중...")

        def done(result, error) -> None:
            if error:
                self.capture_result.setText(f"⚠ {error}")
            else:
                self.capture_result.setText(f"✔ 연결됨: {result.get('device')} → {result.get('pc')}")

        self._background(self, lambda: links.entcapture_hello(edited.cap_url, edited.cap_key), done)

    def test_hearing(self) -> None:
        """Asks the audiometer who it is, and nothing else, with the connection chosen in the dialog."""
        edited = self.settings()
        folder = None
        if edited.hearing_log:
            results = self._fixed_folder or settings_file.results_directory(edited.results_dir)
            folder = Path(os.path.abspath(results)) / hearing_device.LOG_FOLDER
        opener = self._hearing_opener(edited.hearing_link)
        self.hearing_test_button.setEnabled(False)
        self.hearing_result.setText("청력검사기에 묻는 중...")

        def done(result, error) -> None:
            self.hearing_test_button.setEnabled(True)
            self.hearing_result.setText(f"⚠ {error}" if error else f"✔ {result}")

        self._background(self, lambda: hearing_device.check(opener, folder), done)

    def _list_ports(self, chosen: str | None) -> None:
        """Fills the port list, keeping the chosen port in it even while its cable is unplugged."""
        chosen = chosen or ""
        self.port_combo.clear()
        self.port_combo.addItem("자동 (USB-시리얼 포트를 찾아 연결)", "")
        names = []
        for port in self._ports():
            names.append(port.device)
            self.port_combo.addItem(f"{port.device} - {port.description}", port.device)
        if chosen and chosen not in names:
            self.port_combo.addItem(f"{chosen} - 지금은 보이지 않음", chosen)
        self.port_combo.setCurrentIndex(max(0, self.port_combo.findData(chosen)))

    def _browse(self, box: QLineEdit, title: str) -> None:
        start = box.text().strip() or str(settings_file.results_directory().resolve())
        chosen = QFileDialog.getExistingDirectory(self, title, start)
        if chosen:
            box.setText(str(Path(chosen)))

    def _show_fields(self) -> None:
        via = self.via_combo.currentData()
        self.dsn_edit.setEnabled(via == "odbc")
        self.server_edit.setEnabled(via == "server")
        self.token_edit.setEnabled(via == "server")
        self.lookup_test_button.setEnabled(bool(via))


def folder_problem(folder: Path) -> str:
    """Why results cannot be kept in ``folder``, or an empty string when they can."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / "~TSM300 쓰기 확인.tmp"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return str(error)
    return ""


def _full_path(typed: str) -> str:
    """A folder typed without a drive would otherwise mean a different place each time the
    program is started from somewhere else."""
    return os.path.abspath(typed.strip()) if typed.strip() else ""


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #666666;")
    return label


class HearingReceiveDialog(QDialog):
    """The tests fetched from the audiometer, under the device's patients they were saved for,
    for the operator to choose those of the patient in the window.

    Only tests not saved before are listed, unless the operator asks for all of them. The
    newest device patient's new tests are chosen to start with. The chosen tests become one
    sheet; a test with soundfield curves only may be marked as done with hearing aids.
    """

    TEST, AIDED, ORDER = range(3)

    def __init__(self, parent: QWidget | None, fetched, seen: set[str], patient: Patient, patient_is_new: bool,
                 received_at: datetime) -> None:
        super().__init__(parent)
        self._download = fetched.download
        self._seen = seen
        self._patient = patient
        self._choice: tuple[HearingResult, bool] | None = None
        self._tests: dict[tuple[int, int], tuple] = {}
        self._chosen: set[tuple[int, int]] = set()
        self._aided: set[tuple[int, int]] = set()
        self._filling = False
        self.setWindowTitle("청력검사기에서 받은 검사")
        self.setMinimumSize(700, 520)
        layout = QVBoxLayout(self)

        self.patient_check = QCheckBox()
        self.patient_note = QLabel()
        self.patient_note.setWordWrap(True)
        if patient.is_empty:
            self.patient_check.setVisible(False)
            self.patient_note.setText("환자를 고르지 않았습니다. 환자 정보 없이 결과지를 만듭니다. "
                                      "만든 뒤 환자 정보를 넣고 [이 환자 정보로 저장]을 누르세요.")
            self.patient_note.setStyleSheet("color: #b3261e;")
        else:
            self.patient_check.setText(f"{_who(patient)} 환자의 검사입니다")
            self.patient_check.setStyleSheet("font-size: 15px; font-weight: 700;")
            self.patient_check.setChecked(patient_is_new)
            self.patient_check.toggled.connect(self._show_patient_note)
        layout.addWidget(self.patient_check)
        layout.addWidget(self.patient_note)
        self._patient_is_new = patient_is_new
        self._show_patient_note()

        tests = list({fingerprint(test): test for test in self._download.tests}.values())
        new = [test for test in tests if fingerprint(test) not in seen]
        serial = self._download.serial or (tests[0].packet.serial if tests else "")
        self.summary = QLabel(f"기기 {serial or '일련번호 모름'} · 새 검사 {len(new)}건 · 기기에 있는 검사 {len(tests)}건"
                              + ("" if new else "\n새 검사가 없습니다. 이미 받은 검사를 다시 만들려면 [이미 받은 검사도 보기]를 켜세요."))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        warnings = fetched.warnings
        self.warning_label = QLabel("\n".join(f"⚠ {text}" for text in warnings))
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #8c1d18;")
        self.warning_label.setVisible(bool(warnings))
        layout.addWidget(self.warning_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["검사 (기기에 입력된 환자별)", "보청기 착용", "받은 순서"])
        self.tree.header().setSectionResizeMode(self.TEST, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(self.AIDED, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(self.ORDER, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, stretch=1)
        self.all_check = QCheckBox("이미 받은 검사도 보기")
        self.all_check.toggled.connect(self._show_all)
        layout.addWidget(self.all_check)
        layout.addWidget(_note(
            "이 환자의 검사에 체크하세요. 체크한 검사가 결과지 한 장이 됩니다 (순음 + 어음 + 고주파).\n"
            "보청기를 끼고 한 음장검사는 [보청기 착용]에 체크하면 같은 결과지에 '음장 보청기'로 들어갑니다.\n"
            f"검사일시는 받은 시각({received_at:%Y-%m-%d %H:%M})으로 적습니다. 다르면 만든 뒤 [화면의 결과지 고치기]에서 고칩니다."))
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox()
        self.ok_button = buttons.addButton("결과지 만들기", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # A test can come twice in one reading: old firmware pushes the newest test as soon as it is
        # asked who it is, before the memory follows. The copy under its patient is the one listed.
        last = {}
        order = 0
        for group_index, group in enumerate(self._download.groups):
            for test_index, test in enumerate(group.tests):
                order += 1
                last[fingerprint(test)] = (group_index, test_index, order)
        newest = None
        for group_index, test_index, order in sorted(last.values()):
            group = self._download.groups[group_index]
            test = group.tests[test_index]
            fresh = fingerprint(test) not in seen
            self._tests[(group_index, test_index)] = (group, test, order, fresh)
            if fresh and type_of(test):
                newest = group_index
        if newest is not None:
            self._chosen = {key for key, (_, test, _, fresh) in self._tests.items()
                            if key[0] == newest and fresh and type_of(test)}
        self._fill()

    def choice(self) -> tuple[HearingResult, bool] | None:
        """The sheet made of the chosen tests, and whether it is the patient's in the window."""
        return self._choice

    def chosen_keys(self) -> set[tuple[int, int]]:
        return set(self._chosen)

    def set_chosen(self, key: tuple[int, int], chosen: bool = True, aided: bool | None = None) -> None:
        """Ticks a test, as the operator would."""
        item = self._items.get(key)
        if item is None:
            raise KeyError(key)
        item.setCheckState(self.TEST, Qt.CheckState.Checked if chosen else Qt.CheckState.Unchecked)
        if aided is not None:
            item.setCheckState(self.AIDED, Qt.CheckState.Checked if aided else Qt.CheckState.Unchecked)

    def accept(self) -> None:
        keys = [key for key in sorted(self._tests) if key in self._chosen]
        if not keys:
            self.problem_label.setText("결과지에 넣을 검사를 하나 이상 고르세요.")
            return
        parts = []
        for key in keys:
            group, test, _, _ = self._tests[key]
            parts.append(Part(test, group.patient, key in self._aided))
        try:
            result = HearingResult(tuple(parts))
        except MergeError as error:
            self.problem_label.setText(f"⚠ {error}")
            return
        self._choice = (result, not self._patient.is_empty and self.patient_check.isChecked())
        super().accept()

    def _show_patient_note(self) -> None:
        if self._patient.is_empty:
            return
        if self.patient_check.isChecked():
            text, color = "체크한 검사를 이 환자의 결과로 저장합니다.", "#666666"
        elif self._patient_is_new:
            text, color = "체크하지 않으면 환자 정보 없이 저장합니다.", "#b3261e"
        else:
            text, color = ("화면의 환자 정보는 앞 검사에 쓴 것입니다. 같은 환자의 검사면 위에 체크하세요. "
                           "체크하지 않으면 환자 정보 없이 저장합니다."), "#b3261e"
        self.patient_note.setText(text)
        self.patient_note.setStyleSheet(f"color: {color};")

    def _show_all(self, shown: bool) -> None:
        if not shown:  # what can no longer be seen must not go on the sheet unseen
            self._chosen = {key for key in self._chosen if self._tests[key][3]}
        self._fill()

    def _fill(self) -> None:
        self._filling = True
        self.tree.clear()
        self._items: dict[tuple[int, int], QTreeWidgetItem] = {}
        everything = self.all_check.isChecked()
        for group_index, group in enumerate(self._download.groups):
            keys = [key for key in sorted(self._tests) if key[0] == group_index and (everything or self._tests[key][3])]
            if not keys:
                continue
            heading = QTreeWidgetItem([self._group_text(group), "", ""])
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = heading.font(self.TEST)
            font.setBold(True)
            heading.setFont(self.TEST, font)
            self.tree.addTopLevelItem(heading)
            for key in keys:
                _, test, order, fresh = self._tests[key]
                text = describe(test) + ("" if fresh else "  (이미 받음)")
                item = QTreeWidgetItem([text, "", str(order)])
                if type_of(test):
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(self.TEST, Qt.CheckState.Checked if key in self._chosen else Qt.CheckState.Unchecked)
                    if can_be_aided(test):
                        item.setCheckState(self.AIDED,
                                           Qt.CheckState.Checked if key in self._aided else Qt.CheckState.Unchecked)
                else:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                if not fresh or not type_of(test):
                    item.setForeground(self.TEST, DONE_INK)
                heading.addChild(item)
                self._items[key] = item
            heading.setExpanded(True)
        self._filling = False

    def _group_text(self, group) -> str:
        device_patient = group.patient
        if device_patient is None:
            return "기기 환자 정보 없음"
        parts = [f"기기 환자 {device_patient.number}", device_patient.name or "(이름 없음)"]
        parts += [part for part in (device_patient.birth_date, device_patient.sex) if part]
        text = " · ".join(parts)
        born = device_birth_date(device_patient)
        if born and self._patient.birth_date and born != self._patient.birth_date:
            text += f"  ⚠ 생년월일이 화면의 환자({self._patient.birth_date.isoformat()})와 다릅니다"
        return text

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._filling:
            return
        key = next((key for key, shown in self._items.items() if shown is item), None)
        if key is None:
            return
        target = self._chosen if column == self.TEST else self._aided if column == self.AIDED else None
        if target is None:
            return
        if item.checkState(column) == Qt.CheckState.Checked:
            target.add(key)
        else:
            target.discard(key)
        self.problem_label.setText("")


class UpdateDialog(QDialog):
    """Looks for a newer version, installs it when asked, and offers to start it.

    It ends accepted when the operator wants the program started again with the new version.
    """

    def __init__(self, parent: QWidget | None, updates=update_module, background: Background = run_in_background) -> None:
        super().__init__(parent)
        self._updates = updates
        self._background = background
        self._release = None
        self._progress = (0, 0)
        self._busy = False
        self.setWindowTitle("업데이트")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.message)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.install_button = QPushButton("지금 설치")
        self.install_button.clicked.connect(self.install)
        self.restart_button = QPushButton("다시 켜기")
        self.restart_button.clicked.connect(self.accept)
        self.close_button = QPushButton("닫기")
        self.close_button.clicked.connect(self.reject)
        for button in (self.install_button, self.restart_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.install_button.setVisible(False)
        self.restart_button.setVisible(False)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(150)
        self._progress_timer.timeout.connect(self._show_progress)
        self.look()

    def look(self) -> None:
        self.message.setText("새 버전이 있는지 확인하는 중입니다...")

        def done(release, error) -> None:
            if error is not None:
                self.message.setText(f"⚠ {error}")
            elif not self._updates.is_newer(release.version):
                self.message.setText(f"지금 쓰는 v{__version__} 이 최신 버전입니다.")
            elif self._updates.program_file() is None:
                self.message.setText(f"새 버전 v{release.version} 이 있습니다. (지금 v{__version__})\n\n"
                                     "Python 으로 설치한 프로그램은 명령 프롬프트에서 아래 명령으로 업데이트합니다.\n\n"
                                     f"{self._updates.PYTHON_COMMAND}")
            else:
                self._release = release
                self.message.setText(f"새 버전 v{release.version} 이 있습니다. (지금 v{__version__})\n\n"
                                     "[지금 설치]를 누르면 새 버전을 받아 바꿔 넣습니다. 받는 동안에도 검사 결과는 그대로 받습니다.")
                self.install_button.setVisible(True)

        self._background(self, self._updates.latest_release, done)

    def install(self) -> None:
        release = self._release
        self._busy = True
        self._progress = (0, 0)
        self.install_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.message.setText(f"v{release.version} 을 받는 중입니다...")
        self._progress_timer.start()

        def note_progress(received: int, total: int) -> None:  # called on the downloading thread
            self._progress = (received, total)

        def done(_, error) -> None:
            self._busy = False
            self._progress_timer.stop()
            self.progress_bar.setVisible(False)
            self.close_button.setEnabled(True)
            if error is not None:
                self.message.setText(f"⚠ {error}")
                self.install_button.setEnabled(True)
                return
            self.install_button.setVisible(False)
            self.restart_button.setVisible(True)
            self.close_button.setText("나중에")
            self.message.setText(f"✔ v{release.version} 을 받았습니다.\n\n[다시 켜기]를 누르면 프로그램이 새 버전으로 다시 켜집니다. "
                                 "다시 켜지는 동안에는 장비에서 결과를 보내지 마세요.\n"
                                 "[나중에]를 누르면 다음에 프로그램을 켤 때부터 새 버전입니다.")

        self._background(self, lambda: self._updates.install(release, self._updates.program_file(), note_progress), done)

    def reject(self) -> None:
        if not self._busy:  # the program file must not be left half replaced
            super().reject()

    def _show_progress(self) -> None:
        received, total = self._progress
        self.progress_bar.setRange(0, 100 if total else 0)
        if total:
            self.progress_bar.setValue(received * 100 // total)


class MainWindow(QMainWindow):
    port_wanted = Signal(str)
    """Emitted with the port chosen in the settings (empty for finding one) when that changes."""

    def __init__(self, directory: Path, settings: Settings | None = None, source: PatientSource | None = None,
                 now: Callable[[], datetime] = datetime.now,
                 confirm: Callable[[str], bool] | None = None,
                 confirm_patient: Callable[[Patient], bool] | None = None,
                 background: Background = run_in_background,
                 send: Callable[..., dict] = send_sheet,
                 settings_directory: Path | None = None, updates=update_module,
                 directory_follows_settings: bool = False,
                 hearing_opener: Callable[[str], Callable[[], object]] = hearing_device.opener_for,
                 choose_hearing: Callable[..., tuple | None] | None = None,
                 fetch_hearing: Callable[..., object] = hearing_device.fetch,
                 run_editor: Callable[[QDialog], object] | None = None) -> None:
        super().__init__()
        self._directory = _full(directory)
        self._directory_follows_settings = directory_follows_settings
        self._updates = updates
        self._settings = settings or Settings()
        self._settings_directory = settings_directory
        self._source = source if source is not None else records_from_settings(self._settings)
        self._now = now
        self._confirm = confirm or self._ask
        self._confirm_patient = confirm_patient or self._ask_patient
        self._background = background
        self._send = send
        self._connection = "포트에 연결하지 않았습니다."
        self._measurement: Measurement | None = None
        self._received_at: datetime | None = None
        self._sheet: Path | None = None
        self._sheet_patient = Patient()
        self._sheet_note = ""
        self._sheet_remarks = ""
        self._sheet_tested_at: datetime | None = None
        self._sheet_clinic = self._settings.clinic
        self._problem = ""
        self._problem_is_about_edits = False
        self._lost: tuple[Path, str, datetime, str] | None = None
        self._last_save_error = ""
        self._filed = ""
        self._filing_problem = ""
        self._redraw_after_sending = False
        self._close_agreed = False
        self._patient_is_new = False
        self._orders: list[Order] = []
        self._loading_orders = False
        self._sending = False
        self._going: Path | None = None
        self._outbox: list[tuple] = []
        self._sent: dict | None = None
        self._hearing_opener = hearing_opener
        self._choose_hearing = choose_hearing or self._ask_hearing
        self._fetch_hearing = fetch_hearing
        self._run_editor = run_editor or (lambda dialog: dialog.exec())
        self._receiving = False
        self._hearing_progress = 0
        self._hearing_text = ""
        self._no_wheel = NoWheel(self)
        self.setWindowTitle(f"TSM-300 임피던스 청력검사 v{__version__}")
        self.resize(1240, 840)
        self._build()
        self._orders_timer = QTimer(self)
        self._orders_timer.setInterval(ORDERS_REFRESH_MS)
        self._orders_timer.timeout.connect(self.refresh_orders)
        self._orders_timer.start()
        self._hearing_timer = QTimer(self)
        self._hearing_timer.setInterval(HEARING_PROGRESS_MS)
        self._hearing_timer.timeout.connect(self._show_hearing)
        self._show_source()
        self._show_hearing()
        self._show_state()

    # ----- layout -------------------------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        columns = QHBoxLayout(central)
        columns.setContentsMargins(14, 14, 14, 14)
        columns.setSpacing(14)

        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(12)
        self.orders_box = self._build_orders()
        side.addWidget(self.orders_box, stretch=1)
        side.addWidget(self._build_patient_form())
        side.addWidget(self._build_note())
        self._side_filler = QWidget()
        self._side_filler.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        side.addWidget(self._side_filler)
        side_holder = QWidget()
        side_holder.setLayout(side)
        side_holder.setFixedWidth(430)
        columns.addWidget(side_holder)

        main = QVBoxLayout()
        main.setSpacing(10)
        top = QHBoxLayout()
        self.connection_label = QLabel(self._connection)
        self.connection_label.setStyleSheet("color: #666666;")
        top.addWidget(self.connection_label, stretch=1)
        self.hearing_button = QPushButton(HEARING_LABEL)
        self.hearing_button.setStyleSheet("font-weight: 700; padding: 6px 14px;")
        self.hearing_button.setToolTip("청력검사기(MRS Resonance)에 저장된 순음 · 어음 · 음장 검사를 읽어 와 결과지로 만듭니다.")
        self.hearing_button.clicked.connect(self.receive_hearing)
        top.addWidget(self.hearing_button)
        self.manual_button = QPushButton(MANUAL_LABEL)
        self.manual_button.setToolTip("청력검사기 없이 순음 · 어음 · 음장 값을 손으로 넣어 결과지를 만듭니다.")
        self.manual_button.clicked.connect(self.enter_hearing_by_hand)
        top.addWidget(self.manual_button)
        update_button = QPushButton("업데이트")
        update_button.clicked.connect(self.open_update)
        top.addWidget(update_button)
        settings_button = QPushButton("설정")
        settings_button.clicked.connect(self.open_settings)
        top.addWidget(settings_button)
        main.addLayout(top)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("font-size: 15px; font-weight: 600; padding: 10px 12px;"
                                       " background: #f3f3f3; border-radius: 6px;")
        main.addWidget(self.state_label)
        self.hearing_label = QLabel()
        self.hearing_label.setWordWrap(True)
        self.hearing_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hearing_label.setStyleSheet("padding: 0 12px;")
        main.addWidget(self.hearing_label)
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.problem_label.setStyleSheet("color: #8c1d18; background: #fdecea; padding: 8px 12px; border-radius: 6px;")
        main.addWidget(self.problem_label)
        self.clinic_hint = _note(NO_CLINIC_HINT)
        self.clinic_hint.setContentsMargins(12, 0, 12, 0)
        main.addWidget(self.clinic_hint)
        self.capture_label = QLabel()
        self.capture_label.setWordWrap(True)
        self.capture_label.setStyleSheet("padding: 0 12px;")
        main.addWidget(self.capture_label)
        self.preview = SheetPreview()
        main.addWidget(self.preview, stretch=1)
        self.filing_label = QLabel()
        self.filing_label.setWordWrap(True)
        self.filing_label.setStyleSheet("padding: 0 12px;")
        self.filing_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        main.insertWidget(main.count() - 1, self.filing_label)
        actions = QHBoxLayout()
        self.output_button = QPushButton(OUTPUT_LABEL)
        self.output_button.setStyleSheet("font-weight: 700; padding: 6px 14px;")
        self.output_button.setToolTip("결과지를 인쇄하고, 상쾌한캡처로 보내고, 결과지와 로데이터를 날짜별 보관 폴더에 정리해 둡니다.")
        self.output_button.clicked.connect(self.output_sheet)
        self.print_button = QPushButton("인쇄...")
        self.print_button.setToolTip("결과지만 인쇄합니다.")
        self.print_button.clicked.connect(self.print_sheet)
        self.send_button = QPushButton("상쾌한캡처로 보내기")
        self.send_button.setToolTip("결과지만 상쾌한캡처로 보냅니다.")
        self.send_button.clicked.connect(self.send_to_capture)
        self.file_button = QPushButton("컴퓨터에 보관")
        self.file_button.setToolTip("결과지(JPG)와 로데이터(JSON, BIN)만 날짜별 보관 폴더에 정리해 둡니다.")
        self.file_button.clicked.connect(self.file_sheet)
        self.open_button = QPushButton("결과지 열기")
        self.open_button.clicked.connect(self._open_sheet)
        for button in (self.output_button, self.print_button, self.send_button, self.file_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.open_button)
        main.addLayout(actions)
        folders = QHBoxLayout()
        earlier_button = QPushButton("지난 결과 열기...")
        earlier_button.clicked.connect(self._choose_result)
        folder_button = QPushButton("저장 폴더 열기")
        folder_button.setToolTip("받은 결과가 모두 자동으로 저장되는 폴더입니다.")
        folder_button.clicked.connect(self._open_folder)
        filed_button = QPushButton("보관 폴더 열기")
        filed_button.setToolTip("[출력]이나 [컴퓨터에 보관]을 누른 결과가 날짜별로 정리되는 폴더입니다.")
        filed_button.clicked.connect(self._open_archive)
        folders.addStretch(1)
        for button in (earlier_button, folder_button, filed_button):
            folders.addWidget(button)
        main.addLayout(folders)
        columns.addLayout(main, stretch=1)

    def _build_orders(self) -> QGroupBox:
        box = QGroupBox("오늘 검사 오더")
        layout = QVBoxLayout(box)
        self.orders_table = QTableWidget(0, len(ORDER_COLUMNS))
        self.orders_table.setHorizontalHeaderLabels(ORDER_COLUMNS)
        self.orders_table.verticalHeader().setVisible(False)
        self.orders_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.orders_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.orders_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.orders_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.orders_table.cellClicked.connect(self._order_chosen)
        layout.addWidget(self.orders_table, stretch=1)
        self.orders_note = QLabel()
        self.orders_note.setWordWrap(True)
        self.orders_note.setStyleSheet("color: #666666;")
        layout.addWidget(self.orders_note)
        self.refresh_button = QPushButton("새로 읽기")
        self.refresh_button.clicked.connect(self.refresh_orders)
        layout.addWidget(self.refresh_button)
        return box

    def _build_patient_form(self) -> QGroupBox:
        box = QGroupBox("환자 정보")
        layout = QVBoxLayout(box)
        form = QFormLayout()
        self.chart_edit = QLineEdit()
        self.find_button = QPushButton("조회")
        self.find_button.clicked.connect(self.find_patient)
        self.chart_edit.returnPressed.connect(self.find_patient)
        chart_row = QHBoxLayout()
        chart_row.addWidget(self.chart_edit)
        chart_row.addWidget(self.find_button)
        form.addRow("차트번호", chart_row)
        self.name_edit = QLineEdit()
        form.addRow("이름", self.name_edit)
        self.sex_combo = QComboBox()
        self.sex_combo.addItems(["", "남", "여"])
        self.sex_combo.installEventFilter(self._no_wheel)
        form.addRow("성별", self.sex_combo)
        self.birth_edit = QLineEdit()
        self.birth_edit.setPlaceholderText("예: 1980-01-31 또는 19800131")
        form.addRow("생년월일", self.birth_edit)
        layout.addLayout(form)
        self.form_note = QLabel()
        self.form_note.setWordWrap(True)
        self.form_note.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.form_note)
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.clicked.connect(self.save_with_patient)
        layout.addWidget(self.save_button)
        clear_button = QPushButton("새 환자 (지우기)")
        clear_button.clicked.connect(self.clear_patient)
        layout.addWidget(clear_button)
        self.source_note = QLabel(NO_SOURCE_NOTE)
        self.source_note.setWordWrap(True)
        self.source_note.setStyleSheet("color: #666666;")
        layout.addWidget(self.source_note)
        for edit in (self.chart_edit, self.name_edit, self.birth_edit):
            edit.textEdited.connect(self._patient_edited)
        self.sex_combo.activated.connect(self._patient_edited)
        return box

    def _build_note(self) -> QGroupBox:
        box = QGroupBox("화면의 결과지 고치기")
        layout = QVBoxLayout(box)
        form = QFormLayout()
        self.tested_edit = QDateTimeEdit()
        self.tested_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.tested_edit.setCalendarPopup(True)
        self.tested_edit.setMinimumDateTime(QDateTime(EARLIEST_TEST))
        self.tested_edit.installEventFilter(self._no_wheel)
        self.tested_edit.dateTimeChanged.connect(self._show_state)
        form.addRow("검사일시", self.tested_edit)
        layout.addLayout(form)
        layout.addWidget(QLabel("메모"))
        self.note_edit = QPlainTextEdit()
        self.note_edit.setFixedHeight(84)
        self.note_edit.setPlaceholderText("결과를 받은 뒤에 쓸 수 있습니다")
        self.note_edit.textChanged.connect(self._show_state)
        layout.addWidget(self.note_edit)
        self.note_hint = QLabel()
        self.note_hint.setWordWrap(True)
        self.note_hint.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.note_hint)
        layout.addWidget(QLabel(REMARKS_LABEL))
        self.remarks_edit = QPlainTextEdit()
        self.remarks_edit.setFixedHeight(56)
        self.remarks_edit.setPlaceholderText("인쇄하지 않고 저장만 합니다. 직원끼리 알아 둘 것을 적습니다")
        self.remarks_edit.textChanged.connect(self._show_state)
        layout.addWidget(self.remarks_edit)
        buttons = QHBoxLayout()
        self.note_button = QPushButton(EDIT_LABEL)
        self.note_button.clicked.connect(self.save_edits)
        buttons.addWidget(self.note_button, stretch=1)
        self.values_button = QPushButton(VALUES_LABEL)
        self.values_button.setToolTip("청력검사 결과지의 역치 · 차폐 · 어음 값을 고칩니다. 고친 값은 결과지에 그대로 인쇄됩니다.")
        self.values_button.clicked.connect(self.edit_hearing_values)
        buttons.addWidget(self.values_button)
        layout.addLayout(buttons)
        return box

    # ----- settings -----------------------------------------------------------------------

    def open_settings(self) -> None:
        fixed_folder = None if self._directory_follows_settings else self._directory
        dialog = SettingsDialog(self, self._settings, self._background, fixed_folder=fixed_folder,
                                hearing_opener=self._hearing_opener)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.apply_settings(dialog.settings())
        except OSError as error:
            QMessageBox.warning(self, "설정", f"설정을 저장하지 못했습니다. 바꾼 내용은 적용하지 않았습니다.\n\n{error}")

    def apply_settings(self, settings: Settings) -> None:
        """Saves the settings and switches the clinic's details, the device's port, the results
        folder, patient lookup and sending over to them. Raises OSError, with nothing changed,
        when the settings cannot be saved."""
        previous = self._settings
        # The dialog started from the settings as they were when it opened. What the program
        # has learned by itself since then is not the dialog's to undo.
        settings = replace(settings, port_heard=previous.port_heard, port_heard_id=previous.port_heard_id)
        settings_file.save(settings, self._settings_directory)
        if isinstance(self._source, Records):
            self._source.close()
        self._settings = settings
        self._source = records_from_settings(settings)
        if self._directory_follows_settings:
            self._directory = _full(settings_file.results_directory(settings.results_dir))
        if settings.port != previous.port:
            self.port_wanted.emit(settings.port)
        if settings.clinic != previous.clinic and self._sheet is not None and not self._in_capture():
            if self._sending and self._going == self._sheet:
                self._redraw_after_sending = True  # not under a sheet that is on its way; see when it has arrived
            else:
                self._sheet_clinic = settings.clinic  # also if the sheet cannot be rewritten now: the next save carries it
                self._save_again(self._sheet_patient, self._note_text(), send=False, tested_at=self._tested_value())
        self._show_source()
        self._show_hearing()
        self._show_state()

    def remember_port(self, port: str, adapter: str = "") -> None:
        """Notes the port and the adapter the device has just been heard on, which next time
        tells its cable from the cables of other instruments."""
        if not port or (port, adapter) == (self._settings.port_heard, self._settings.port_heard_id):
            return
        if not adapter and port == self._settings.port_heard:
            return  # the same port, its adapter unknown this time: what is known of it is not thrown away
        self._settings = replace(self._settings, port_heard=port, port_heard_id=adapter)
        try:
            settings_file.save(self._settings, self._settings_directory)
        except OSError:
            pass  # it only saves a choice in the settings; the device is connected either way

    def open_update(self) -> None:
        if UpdateDialog(self, self._updates, self._background).exec() == QDialog.DialogCode.Accepted:
            self.restart_updated()

    def restart_updated(self) -> None:
        """Starts the newly installed program and closes this one, losing nothing typed."""
        if not self._may_close():
            return
        try:
            self._updates.restart(self._updates.program_file(), sys.argv[1:])
        except OSError as error:
            QMessageBox.warning(self, "업데이트", f"새 버전을 켜지 못했습니다. 프로그램을 닫고 직접 다시 켜 주세요.\n\n{error}")
            return
        self._close_agreed = True
        self.close()

    def _show_source(self) -> None:
        has_source = self._source is not None
        self.orders_box.setVisible(has_source)
        self._side_filler.setVisible(not has_source)
        self.find_button.setVisible(has_source)
        self.source_note.setVisible(not has_source)
        self._orders = []
        self.orders_table.setRowCount(0)
        self.refresh_orders()

    # ----- patient ------------------------------------------------------------------------

    def patient(self) -> Patient:
        """The patient in the form. Raises ValueError when the birth date cannot be read."""
        return Patient(
            chart_no=self.chart_edit.text().strip(),
            name=self.name_edit.text().strip(),
            sex=self.sex_combo.currentText(),
            birth_date=parse_birth_date(self.birth_edit.text()),
        )

    def set_patient(self, patient: Patient) -> None:
        self._fill_form(patient)
        self._patient_edited()

    def clear_patient(self) -> None:
        self.set_patient(Patient())
        self.orders_table.clearSelection()
        self.chart_edit.setFocus()

    def find_patient(self) -> None:
        chart_no = self.chart_edit.text().strip()
        if self._source is None or not chart_no:
            return
        source = self._source
        self.form_note.setText("조회하는 중...")

        def done(found, error) -> None:
            if isinstance(error, LookupError):
                self.form_note.setText(str(error))
            elif error is not None:
                self.form_note.setText(f"조회하지 못했습니다: {error}")
            elif found is None:
                self.form_note.setText(f"차트번호 {chart_no} 환자를 찾지 못했습니다.")
            else:
                self.form_note.setText("")
                self._offer(found)

        self._background(self, lambda: source.find(chart_no), done)

    def refresh_orders(self) -> None:
        if self._source is None or self._loading_orders:
            return
        source = self._source
        self._loading_orders = True
        if not self._orders:
            self.orders_note.setText("오더 목록을 읽는 중...")

        def done(orders, error) -> None:
            self._loading_orders = False
            if source is not self._source:
                return
            if error is not None:
                self._orders = []
                self.orders_table.setRowCount(0)
                self.orders_note.setText(f"오더 목록을 가져오지 못했습니다: {error}")
                return
            self._orders = orders
            self._show_orders()

        self._background(self, source.todays_orders, done)

    def _show_orders(self) -> None:
        today = self._now().date()
        done = tested_on(self._directory, today)
        hearing = self._settings.hearing_on
        chosen = self.chart_edit.text().strip()
        self.orders_table.setRowCount(len(self._orders))
        waiting = 0
        for row, order in enumerate(self._orders):
            patient = order.patient
            age = patient.age_on(today)
            who = " ".join(part for part in (f"{age}세" if age is not None else "", patient.sex) if part)
            kinds = exams_of(order.codes)
            finished = [kind for kind in kinds if patient.chart_no in done.get(kind, set())]
            tested = len(finished) == len(kinds)
            if hearing:  # which of the tests ordered, and which of them are done
                mark = " · ".join(EXAM_NAMES[kind] + (" ✓" if kind in finished else "") for kind in kinds)
            else:
                mark = "✓" if tested else ""
            waiting += order.selectable and not tested
            cells = (order.time, patient.chart_no, patient.name, who, order.room, mark)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if order.label:
                    item.setToolTip(order.label)
                if not order.selectable:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                if tested or not order.selectable:
                    item.setForeground(DONE_INK)
                self.orders_table.setItem(row, column, item)
            if order.selectable and chosen and patient.chart_no == chosen:
                self.orders_table.selectRow(row)
        if self._orders:
            self.orders_note.setText(
                f"{len(self._orders)}명 (검사 전 {waiting}명) · {self._now():%H:%M:%S} 읽음 · 30초마다 다시 읽습니다")
        else:
            what = "임피던스 · 청력검사" if hearing else "임피던스 검사"
            self.orders_note.setText(f"오늘 {what} 오더가 없습니다. · {self._now():%H:%M:%S} 읽음")

    def _order_chosen(self, row: int, _column: int) -> None:
        order = self._orders[row]
        if order.selectable:
            self._offer(order.patient)

    def _offer(self, patient: Patient) -> None:
        """Puts a patient from the records into the form once the operator confirms who it is."""
        if self._confirm_patient(patient):
            self.set_patient(patient)
            self._select_order_row(patient.chart_no)
        else:
            self.orders_table.clearSelection()

    def _select_order_row(self, chart_no: str) -> None:
        self.orders_table.clearSelection()
        for row, order in enumerate(self._orders):
            if order.selectable and order.patient.chart_no == chart_no:
                self.orders_table.selectRow(row)

    def _patient_edited(self, *_) -> None:
        self._patient_is_new = True
        self._show_state()

    # ----- results ------------------------------------------------------------------------

    def show_connection(self, text: str) -> None:
        self._connection = text
        self.connection_label.setText(text)

    def handle_event(self, event) -> None:
        if isinstance(event, Measurement):
            self._measurement_received(event)
        elif isinstance(event, PowerOn):
            self.connection_label.setText(f"{self._connection} · 장비 켜짐 ({event.model})")
        elif isinstance(event, Corrupt):
            self.state_label.setText("전송을 해독하지 못했습니다. 장비에서 → 키를 다시 눌러 주세요.")

    def save_with_patient(self) -> None:
        """Saves the shown result again under the patient now in the form, replacing the earlier files.

        Attaching a patient to a result that has none needs no question. Replacing one
        patient with another could put the wrong name on a result, so that is confirmed first.
        """
        if self._measurement is None or self._sheet is None or self._sending:
            return
        try:
            patient = self.patient()
        except ValueError:
            self._show_state()
            return
        if patient.is_empty or patient == self._sheet_patient:
            return
        if not self._sheet_patient.is_empty:
            question = (f"직전 결과지는 {_who(self._sheet_patient)} 환자로 저장되어 있습니다.\n"
                        f"{_who(patient)} 환자의 결과로 바꿀까요?")
            if self._in_capture():
                question += (f"\n\n이 결과지는 이미 상쾌한캡처에 {_who(self._sheet_patient)} 환자로 들어가 있습니다. "
                             "그쪽 것은 상쾌한캡처 사진 이력에서 '중이검사' 카드를 오른쪽 클릭해 [삭제…]로 지워 주세요.")
            asked_about = self._sheet
            if not self._confirm(question):
                return
            if self._sheet != asked_about:  # another result arrived while the question was open
                self._problem = ("⚠ 답하는 동안 새 결과가 도착해서 환자를 바꾸지 않았습니다. "
                                 "앞 결과지는 [지난 결과 열기...]로 열어 다시 바꿔 주세요.")
                self._problem_is_about_edits = False
                self._show_state()
                return
        was_new, self._patient_is_new = self._patient_is_new, False
        if not self._save_again(patient, self._note_text(), send=self._settings.cap_on, tested_at=self._tested_value()):
            self._patient_is_new = was_new
            self._show_state()

    def save_edits(self) -> None:
        """Prints the corrected test time and the memo on the shown sheet. A sheet already sent
        is not sent again by itself, because 상쾌한캡처 would keep both copies."""
        if self._measurement is None or self._sheet is None or self._sending or not self._edited():
            return
        self._save_again(self._sheet_patient, self._note_text(), tested_at=self._tested_value(),
                         send=self._settings.cap_on and not self._in_capture(), only_edits=True)

    def output_sheet(self) -> None:
        """Does all a finished result needs, in one go: prints the sheet, files it with its raw
        data on this PC and sends it to 상쾌한캡처. Each step also has a button of its own."""
        if self._sheet is None or self._sending or not self._keep_unsaved_edits():
            return
        sheet = self._sheet  # another result may arrive and take the screen while a question or the printer is open
        if self._sheet_patient.is_empty and not self._confirm(
                "환자 정보가 없는 결과지입니다.\n환자 정보 없이 출력할까요? 상쾌한캡처로는 보내지 않습니다."):
            return
        printed = self._sheet == sheet and self._print(sheet)
        if self._sheet != sheet:
            done = "앞 결과지는 인쇄만 했습니다. 보관과 보내기는" if printed else "앞 결과지는 출력하지 않았습니다. 출력은"
            self._problem = f"⚠ 출력하는 동안 새 결과가 도착했습니다. {done} [지난 결과 열기...]로 그 결과를 열어서 해 주세요."
            self._problem_is_about_edits = False
            self._show_state()
            return
        if not printed:
            return
        self._file()
        if self._settings.capture_ready and not self._in_capture():
            self._send_when_possible()

    def print_sheet(self) -> None:
        """Prints the shown sheet, with whatever was being corrected on it saved first."""
        if self._sheet is not None and self._keep_unsaved_edits():
            self._print(self._sheet)

    def file_sheet(self) -> None:
        """Files the shown sheet and its raw data in the archive folder, by date."""
        if self._sheet is not None and not self._sending and self._keep_unsaved_edits():
            self._file()

    def _print(self, sheet: Path) -> bool:
        """Prints ``sheet`` on the printer the operator chooses. False when nothing was printed."""
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setPageOrientation(QPageLayout.Orientation.Portrait)
        printer.setDocName(sheet.stem)
        if not self._choose_printer(printer):
            return False
        try:
            paint_sheet(sheet, printer)
        except OSError as error:
            QMessageBox.warning(self, "인쇄", f"인쇄하지 못했습니다.\n\n{error}")
            return False
        return True

    def _file(self) -> bool:
        """Puts the tidy copy of the shown result in the archive, in place of an earlier copy of it."""
        try:
            where = file_result(self._sheet, self._archive(), self._sheet_patient, self._sheet_tested_at, self._filed)
        except (OSError, ValueError) as error:
            self._filing_problem = f"⚠ 컴퓨터에 보관하지 못했습니다: {error}"
            self._show_filing()
            return False
        self._filed, self._filing_problem = where, ""
        try:
            note_in_record(self._sheet, filed=where)
        except (OSError, ValueError):
            pass  # the copy is made and this window knows where; the record is told at the next save
        self._show_filing()
        return True

    def _refile(self) -> None:
        """Brings the tidy copy up to date after the result it is a copy of has changed."""
        if self._filed and self._sheet is not None:
            self._file()

    def _archive(self) -> Path:
        return archive_directory(self._directory, self._settings.archive_dir)

    def open_result(self, path: Path) -> None:
        """Shows a result saved earlier, so that its memo or patient can be corrected."""
        if not self._keep_unsaved_edits():  # first, or the result on screen would be read back without them
            return
        try:
            saved = load_result(_full(path))
            if saved.filed_from is not None:
                saved = self._behind_the_copy(saved)
        except (ValueError, OSError) as error:
            self._problem, self._problem_is_about_edits = f"⚠ {error}", False
            self._show_state()
            return
        self._measurement, self._received_at = saved.measurement, saved.received_at
        self._sheet, self._sheet_patient, self._sheet_note = saved.sheet, saved.patient, saved.note
        self._sheet_remarks = saved.remarks
        self._sheet_tested_at = _minute(saved.tested_at)
        # A sheet keeps the clinic it was printed with. One printed with none (or saved before
        # that was written down) gets the clinic in the settings when it is next rewritten.
        printed_with = saved.clinic is not None and not saved.clinic.is_empty
        self._sheet_clinic = saved.clinic if printed_with else self._settings.clinic
        self._sent = dict(saved.capture[-1]) if saved.capture else None
        self._filed, self._filing_problem = saved.filed, ""
        self._problem, self._problem_is_about_edits = "", False
        self._fill_form(saved.patient)
        self._patient_is_new = False
        self._show_edits(saved.note, self._sheet_tested_at, saved.remarks)
        if self._lost is not None and self._lost[0] == saved.sheet:
            _, note, tested_at, remarks = self._lost
            self._lost = None
            self._show_edits(note, tested_at, remarks)
            self._problem = "저장하지 못했던 검사일시와 메모를 다시 채웠습니다. [검사일시 · 메모 저장]을 눌러 주세요."
            self._problem_is_about_edits = True
        self.preview.show_sheet(self._sheet)
        self._show_capture()
        self._show_filing()
        self._show_state()

    def _behind_the_copy(self, copy):
        """The result a tidy copy was made from, which is the one to correct, send and print.

        When that result has gone from the results folder, it is made again there from the
        copy, and the copy is filed again so that it points at it.
        """
        for origin in (copy.filed_from, self._directory / copy.filed_from.name):  # where it was, or moved with the folder
            if origin.with_suffix(".json").exists() and origin.with_suffix(".bin").exists():
                return load_result(_full(origin))
        sheet = save_result(copy.measurement, self._directory, copy.received_at, copy.patient, copy.note, copy.capture,
                            tested_at=copy.tested_at, clinic=copy.clinic or Clinic(), filed=copy.filed,
                            remarks=copy.remarks)
        where = file_result(sheet, self._archive(), copy.patient, copy.tested_at, str(copy.sheet.with_suffix("")))
        note_in_record(sheet, filed=where)
        return load_result(sheet)

    def _choose_result(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "지난 결과 열기", str(self._directory), "결과 (*.json)")
        if chosen:
            self.open_result(Path(chosen))

    def send_to_capture(self) -> None:
        """Sends the shown sheet to 상쾌한캡처, which checks the chart number and name against the records."""
        if not self._can_send() or not self._keep_unsaved_edits():
            return
        internal_number = self._source.internal_number(self._sheet_patient.chart_no) if self._source is not None else ""
        self._start_sending((self._sheet, self._sheet_patient, self._sheet_tested_at.date(), internal_number))

    def _start_sending(self, item: tuple) -> None:
        sheet, patient, tested_on, internal_number = item
        settings = self._settings
        self._sending, self._going = True, sheet
        if sheet == self._sheet:
            self.capture_label.setText("상쾌한캡처로 보내는 중...")
        self._show_state()

        def done(outcome, error) -> None:
            self._sending, self._going = False, None
            redraw, self._redraw_after_sending = self._redraw_after_sending, False
            if sheet == self._sheet:
                self._sent = outcome if error is None else {"ok": False, "code": "error", "error": str(error), "retry": True}
                self._show_capture()
                if redraw and not self._in_capture():  # it did not arrive, so it can still take the new clinic
                    self._sheet_clinic = self._settings.clinic
                    self._save_again(self._sheet_patient, self._note_text(), send=False, tested_at=self._tested_value())
                else:
                    self._refile()  # the copy's record then says what became of the sending
            else:
                try:
                    refresh_copy(sheet)
                except (OSError, ValueError):
                    pass  # the result's own record holds the outcome; the copy catches up when it is next filed
            self._show_state()
            self._show_orders()
            while self._outbox and not self._sending:
                waiting = self._outbox.pop(0)
                if waiting[0].exists():  # not one that was renamed for another patient while it waited
                    self._start_sending(waiting)

        self._background(self, lambda: self._send(sheet, patient, tested_on, settings, internal_number), done)

    def _send_when_possible(self) -> None:
        """Sends the shown sheet now, or after the sheets that are still on their way: results
        that arrive faster than they can be sent wait their turn, and none is left out."""
        if not self._sending:
            self.send_to_capture()
        elif self._sheet_patient.chart_no and self._sheet_patient.name and self._settings.capture_ready:
            internal_number = self._source.internal_number(self._sheet_patient.chart_no) if self._source is not None else ""
            self._outbox = [item for item in self._outbox if item[0] != self._sheet]
            self._outbox.append((self._sheet, self._sheet_patient, self._sheet_tested_at.date(), internal_number))

    def _in_capture(self) -> bool:
        """Whether 상쾌한캡처 holds a copy of the shown sheet, as it was sent or as it was before a correction."""
        return bool(self._sent and (self._sent.get("ok") or self._sent.get("code") == EDITED_AFTER_SENDING))

    def _can_send(self) -> bool:
        return (self._sheet is not None and not self._sending and self._settings.capture_ready
                and bool(self._sheet_patient.chart_no and self._sheet_patient.name)
                and not (self._sent and self._sent.get("ok")))

    def _measurement_received(self, measurement: Measurement) -> None:
        if measurement.left is None and measurement.right is None:
            self.state_label.setText(
                f"장비의 ID {measurement.memory_id} 에는 저장된 검사가 없습니다. 검사 후 다시 전송해 주세요.")
            return
        if isinstance(self._measurement, Measurement) and measurement.raw == self._measurement.raw:
            self.state_label.setText("같은 결과를 다시 받았습니다. 이미 저장되어 있습니다.")
            return
        try:
            patient = self.patient() if self._patient_is_new else Patient()
        except ValueError:
            patient = Patient()
        self._result_arrived(measurement, patient, self._now())

    def _result_arrived(self, measurement: Measurement | HearingResult, patient: Patient, received_at: datetime,
                        tested_at: datetime | None = None, note: str = "", remarks: str = "",
                        by_hand: bool = False) -> bool:
        """Saves a result that has just come from a device, or been typed in by hand, and shows it.
        False when it could not be saved; ``by_hand`` leaves saying why to the caller (``_last_save_error``)."""
        lost = (None if self._keep_unsaved_edits()
                else (self._sheet, self._note_text(), self._tested_value(), self._remarks_text()))
        tested_at = _minute(tested_at or received_at)
        try:
            sheet = save_result(measurement, self._directory, received_at, patient, note,
                                tested_at=tested_at, clinic=self._settings.clinic, remarks=remarks)
        except Exception as error:  # a full disk, a folder that has gone, a sheet that cannot be drawn
            if by_hand:
                self._log_unexpected(error)
                self._last_save_error = f"결과지를 저장하지 못했습니다: {error}"
            else:
                self._could_not_save(measurement, received_at, error)
            return False
        self._measurement, self._received_at = measurement, received_at
        self._sheet_clinic = self._settings.clinic
        self._sent = None
        self._filed, self._filing_problem = "", ""
        self._patient_is_new = False
        self._show_edits(note, tested_at, remarks)
        self._saved(sheet, patient, note, tested_at, remarks)
        if lost is not None:
            self._lost = lost
            self._show_state()
        if self._settings.cap_on:
            self._send_when_possible()
        return True

    def _lost_text(self) -> str:
        """What is said while corrections to an earlier result are waiting to be saved: they could
        not be written when another result took the screen. They are kept, shown here, and put
        back in their boxes when that result is opened again."""
        if self._lost is None:
            return ""
        sheet, note, tested_at, remarks = self._lost
        return (f"⚠ 앞 결과지({sheet.name})에 고치던 내용을 저장하지 못했습니다. [지난 결과 열기...]로 그 결과를 열면 "
                f"아래 내용이 다시 채워집니다.\n검사일시: {tested_at:%Y-%m-%d %H:%M} · 메모: {note or '(없음)'}"
                + (f" · 특이사항: {remarks}" if remarks else ""))

    def _could_not_save(self, measurement: Measurement | HearingResult, received_at: datetime, error: Exception) -> None:
        """Keeps what the device sent and says what to do. The result shown before stays as it was,
        and the patient in the form stays in line, so sending again from the device is all it takes."""
        folder = (self._settings_directory or settings_file.settings_directory()) / "unsaved"
        kept = ""
        try:
            folder.mkdir(parents=True, exist_ok=True)
            raw = folder / f"{received_at:%Y%m%d_%H%M%S}.bin"
            raw.write_bytes(measurement.raw)
            kept = f"\n장비가 보낸 데이터는 {raw} 에 보관했습니다."
        except OSError:
            pass
        if not isinstance(error, OSError):
            write_error_log(folder.parent, "".join(traceback.format_exception(type(error), error, error.__traceback__)))
        again = ("[청력검사기에서 받기]를 다시 눌러 주세요" if isinstance(measurement, HearingResult)
                 else "장비에서 → 키를 다시 눌러 주세요")
        self._problem = (f"⚠ 방금 받은 결과를 저장하지 못했습니다: {error}\n저장 폴더({self._directory})를 확인한 뒤 "
                         f"{again}.{kept}")
        self._problem_is_about_edits = False
        self._show_state()

    def _save_again(self, patient: Patient, note: str, send: bool, tested_at: datetime | None = None,
                    only_edits: bool = False, remarks: str | None = None,
                    measurement: Measurement | HearingResult | None = None) -> bool:
        """Rewrites the shown result with these details, keeping the record of what was sent.

        Returns False, with the files as they were, when they cannot be rewritten; what went
        wrong is then in ``_last_save_error``. ``only_edits`` says the save is of the test time,
        memo and remarks alone: a warning about such a save goes away when the operator takes
        those back. ``remarks`` are those in their box unless given; ``measurement`` is the
        shown result with its values corrected, which takes its place once it is saved.
        """
        tested_at = tested_at or self._sheet_tested_at
        remarks = self._remarks_text() if remarks is None else remarks
        measurement = measurement or self._measurement
        clinic = self._sheet_clinic
        try:
            history = load_result(self._sheet).capture
        except ValueError:
            history = ()
        sent = self._sent
        if self._sent and self._sent.get("ok"):
            sent = {"ok": False, "code": EDITED_AFTER_SENDING, "error": "보낸 뒤 결과지를 고침", "retry": True,
                    "at": self._now().isoformat(timespec="seconds")}
            history += (sent,)
        elif patient != self._sheet_patient and not self._in_capture():
            sent = None
        try:
            sheet = save_result(measurement, self._sheet.parent, self._received_at, patient, note, history,
                                tested_at=tested_at, clinic=clinic, replacing=self._sheet, filed=self._filed,
                                remarks=remarks)
        except Exception as error:  # most often the sheet is open in another program, which holds the file
            self._log_unexpected(error)
            self._last_save_error = f"고친 내용을 저장하지 못했습니다: {error}"
            self._problem = (f"⚠ 고친 내용을 저장하지 못했습니다: {error}\n"
                             "결과지를 다른 프로그램에서 열어 두었다면 닫고 다시 저장해 주세요.")
            # A warning about a memo or test time goes when those are taken back. One about a
            # patient or clinic that could not be put on the sheet stays until a save succeeds.
            self._problem_is_about_edits = only_edits
            self._show_state()
            return False
        self._sent = sent
        self._measurement = measurement
        self._saved(sheet, patient, note, tested_at, remarks)
        self._refile()
        if send:
            self._send_when_possible()
        return True

    def _log_unexpected(self, error: Exception) -> None:
        """Writes down an error that is not of the expected kinds (a file held open, a full disk)."""
        if not isinstance(error, OSError):
            folder = self._settings_directory or settings_file.settings_directory()
            write_error_log(folder, "".join(traceback.format_exception(type(error), error, error.__traceback__)))

    def _saved(self, sheet: Path, patient: Patient, note: str, tested_at: datetime, remarks: str = "") -> None:
        self._sheet, self._sheet_patient, self._sheet_note, self._sheet_tested_at = sheet, patient, note, tested_at
        self._sheet_remarks = remarks
        self._problem, self._problem_is_about_edits = "", False
        self.preview.show_sheet(sheet)
        self._show_capture()
        self._show_filing()
        self._show_state()
        self._show_orders()

    def _keep_unsaved_edits(self) -> bool:
        """Saves a test time or memo changed for the shown result before something else takes its place.

        Returns False when there was something to save and it could not be saved.
        """
        if self._measurement is None or self._sheet is None or not self._edited():
            return True
        return self._save_again(self._sheet_patient, self._note_text(), send=False, tested_at=self._tested_value(),
                                only_edits=True)

    def _may_close(self) -> bool:
        """Saves what was being corrected, and asks before anything would be lost with the window:
        corrections that cannot be saved, and sheets that have not finished going to 상쾌한캡처."""
        if not self._keep_unsaved_edits() and not self._confirm(
                "고치던 검사일시 · 메모 · 특이사항을 저장하지 못했습니다.\n결과지를 다른 프로그램에서 열어 두었다면 닫고 다시 해 주세요.\n\n"
                "저장하지 않고 프로그램을 닫을까요?"):
            return False
        if self._receiving and not self._confirm(
                "청력검사기에서 검사를 받는 중입니다.\n지금 닫으면 받던 검사는 결과지로 만들지 않습니다. "
                "기기의 검사는 그대로 있으니 다음에 다시 받으면 됩니다.\n\n지금 닫을까요?"):
            return False
        unsent = len(self._outbox) + (1 if self._sending else 0)
        if unsent and not self._confirm(
                f"상쾌한캡처로 아직 다 보내지 못한 결과지가 {unsent}건 있습니다.\n지금 닫으면 보내지지 않을 수 있습니다. "
                "나중에 [지난 결과 열기...]로 열어 다시 보낼 수 있습니다.\n\n지금 닫을까요?"):
            return False
        return True

    def closeEvent(self, event) -> None:
        if not self._close_agreed and not self._may_close():
            event.ignore()
            return
        super().closeEvent(event)

    def _edited(self) -> bool:
        return (self._note_text() != self._sheet_note or self._tested_value() != self._sheet_tested_at
                or self._remarks_text() != self._sheet_remarks)

    def _show_edits(self, note: str, tested_at: datetime, remarks: str = "") -> None:
        """Puts a result's memo, remarks and test time in the boxes where they are corrected."""
        self.tested_edit.setDateTimeRange(
            QDateTime(min(tested_at, EARLIEST_TEST)),
            QDateTime(max(self._now(), tested_at).replace(hour=23, minute=59, second=0)))
        self.tested_edit.setDateTime(QDateTime(tested_at))
        self.note_edit.setPlainText(note)
        self.remarks_edit.setPlainText(remarks)

    def _tested_value(self) -> datetime | None:
        """The test time in its box, or None while no result is shown."""
        if self._sheet_tested_at is None:
            return None
        return _minute(self.tested_edit.dateTime().toPython())

    def _note_text(self) -> str:
        return self.note_edit.toPlainText().strip()

    def _remarks_text(self) -> str:
        return self.remarks_edit.toPlainText().strip()

    def _fill_form(self, patient: Patient) -> None:
        self.chart_edit.setText(patient.chart_no)
        self.name_edit.setText(patient.name)
        self.sex_combo.setCurrentText(patient.sex)
        self.birth_edit.setText(patient.birth_date.isoformat() if patient.birth_date else "")

    # ----- display ------------------------------------------------------------------------

    def _show_state(self) -> None:
        try:
            patient = self.patient()
            if self.form_note.text().startswith(("생년월일", "없는 날짜")):
                self.form_note.setText("")
        except ValueError as error:
            patient = Patient()
            self.form_note.setText(str(error))
        has_result = self._measurement is not None
        saved_for = self._sheet_patient
        if self._problem_is_about_edits and not self._edited():  # what could not be saved has been taken back
            self._problem, self._problem_is_about_edits = "", False
        next_up = not patient.is_empty and self._patient_is_new
        self.open_button.setEnabled(has_result)
        self.print_button.setEnabled(has_result)
        # Filing writes to the result's record, and so does a sending that is under way.
        self.file_button.setEnabled(has_result and not self._sending)
        self.output_button.setEnabled(has_result and not self._sending)
        self.output_button.setText(OUTPUT_LABEL if self._settings.capture_ready else OUTPUT_LABEL_WITHOUT_SENDING)
        self.note_edit.setEnabled(has_result and not self._sending)
        self.remarks_edit.setEnabled(has_result and not self._sending)
        self.tested_edit.setEnabled(has_result and not self._sending)
        hearing = isinstance(self._measurement, HearingResult)
        self.values_button.setVisible(self._settings.hearing_on or hearing)
        self.values_button.setEnabled(hearing and self._sheet is not None and not self._sending)
        self.note_button.setEnabled(has_result and not self._sending and self._edited())
        self.clinic_hint.setVisible(not self._settings.clinic.name)
        self.note_hint.setText("메모가 길어 결과지에는 앞부분만 인쇄됩니다." if wrap_note(self._note_text())[1] else "")
        self.send_button.setVisible(self._settings.capture_ready)
        self.send_button.setEnabled(self._can_send())
        self.save_button.setText(SAVE_LABEL)
        self.save_button.setEnabled(False)
        if not has_result:
            text = WAITING_WITH_HEARING if self._settings.hearing_on else WAITING
            if next_up:
                text = f"다음 검사는 {_who(patient)} 환자로 저장됩니다. " + text
        elif saved_for.is_empty:
            self.save_button.setEnabled(not patient.is_empty)
            if patient.is_empty:
                text = "⚠ 환자 정보 없이 저장했습니다. 환자 정보를 넣고 [이 환자 정보로 저장]을 누르세요."
            elif next_up:
                text = (f"⚠ 환자 정보 없이 저장했습니다. {_who(patient)} 환자의 결과가 맞으면 "
                        "[이 환자 정보로 저장]을 누르세요.")
            else:
                text = (f"⚠ 환자 정보 없이 저장했습니다. 화면의 {_who(patient)} 환자는 앞 검사에 쓴 정보입니다. "
                        "같은 환자면 [이 환자 정보로 저장]을 누르고, 다른 환자면 정보를 바꾼 뒤 누르세요.")
        elif patient.is_empty:
            text = f"✔ {_who(saved_for)} 환자 결과로 저장했습니다. 다음 환자 정보를 넣어 주세요."
        elif patient == saved_for:
            text = f"✔ {_who(saved_for)} 환자 결과로 저장했습니다."
        else:
            self.save_button.setText(REPLACE_LABEL)
            self.save_button.setEnabled(not self._sending)
            text = (f"✔ 직전 결과는 {_who(saved_for)} 환자로 저장되어 있습니다. "
                    f"다음 검사는 {_who(patient)} 환자로 저장됩니다.")
        self.state_label.setText(text)
        problems = "\n".join(part for part in (self._problem, self._lost_text()) if part)
        self.problem_label.setText(problems)
        self.problem_label.setVisible(bool(problems))

    def _show_capture(self) -> None:
        sent = self._sent
        if sent is None:
            text = ""
            if self._sheet is None or not self._settings.capture_ready or self._sheet_patient.is_empty:
                pass
            elif not (self._sheet_patient.chart_no and self._sheet_patient.name):
                text = "상쾌한캡처로 보내려면 차트번호와 이름이 모두 필요합니다."
        elif sent.get("ok") and sent.get("expected_chartId"):
            text = (f"⚠ 상쾌한캡처에 저장했지만 확인이 필요합니다: 조회한 환자의 내부번호는 {sent['expected_chartId']} 인데 "
                    f"상쾌한캡처는 {sent.get('chartId')} 환자로 저장했습니다. 상쾌한캡처에서 맞는 환자인지 봐 주세요.")
        elif sent.get("ok"):
            again = " (이미 들어가 있던 결과지)" if sent.get("duplicate") else ""
            text = f"✔ 상쾌한캡처에 저장했습니다{again}: {sent.get('imageDataName') or ''}"
        elif sent.get("code") == EDITED_AFTER_SENDING:
            text = ("⚠ 보낸 뒤에 결과지를 고쳤습니다. 상쾌한캡처에는 고치기 전 것이 들어 있습니다. [상쾌한캡처로 보내기]를 누르면 "
                    "고친 결과지가 새 장으로 들어가니, 먼저 것은 상쾌한캡처 사진 이력에서 [삭제…]로 지워 주세요.")
        else:
            later = " 잠시 뒤 [상쾌한캡처로 보내기]를 눌러 다시 보낼 수 있습니다." if sent.get("retry") else ""
            text = f"⚠ 상쾌한캡처에 보내지 못했습니다: {sent.get('error')} ({sent.get('code')}).{later}"
        self.capture_label.setText(text)

    def _show_filing(self) -> None:
        if self._filing_problem:
            text = self._filing_problem
        elif self._filed and self._sheet is not None:
            text = f"✔ 컴퓨터에 보관했습니다: {filed_sheet(self._filed)}"
        else:
            text = ""
        self.filing_label.setText(text)
        self.filing_label.setVisible(bool(text))

    def _open_archive(self) -> None:
        """Opens the day's folder of the shown result's tidy copy, or the archive itself."""
        folder = filed_sheet(self._filed).parent if self._filed else self._archive()
        if not folder.exists():
            folder = self._archive()
            folder.mkdir(parents=True, exist_ok=True)
        open_file(folder)

    def _open_sheet(self) -> None:
        if self._sheet is not None:
            open_file(self._sheet)

    def _open_folder(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        open_file(self._directory)

    def _choose_printer(self, printer: QPrinter) -> bool:
        return QPrintDialog(printer, self).exec() == QDialog.DialogCode.Accepted

    def _ask(self, question: str) -> bool:
        answer = QMessageBox.question(self, "확인", question)
        return answer == QMessageBox.StandardButton.Yes

    def _ask_patient(self, patient: Patient) -> bool:
        return PatientConfirm(self, patient, self._now().date()).exec() == QDialog.DialogCode.Accepted

    # ----- the audiometer -----------------------------------------------------------------

    def receive_hearing(self) -> None:
        """Reads every test in the audiometer's memory on a thread of its own, then offers the new ones."""
        if self._receiving or not self._settings.hearing_on:
            return
        opener = self._hearing_opener(self._settings.hearing_link)
        log_folder = self._directory / hearing_device.LOG_FOLDER if self._settings.hearing_log else None
        self._receiving, self._hearing_progress = True, 0
        self._hearing_text = "청력검사기에서 받는 중입니다..."
        self._hearing_timer.start()
        self._show_hearing()

        def note_progress(count: int) -> None:  # called on the receiving thread
            self._hearing_progress = count

        def done(fetched, error) -> None:
            self._receiving = False
            self._hearing_timer.stop()
            if error is not None:
                self._hearing_text = f"⚠ 청력검사기에서 받지 못했습니다. {error}"
                self._show_hearing()
                return
            self._hearing_fetched(fetched)

        self._background(self, lambda: self._fetch_hearing(opener, log_folder, note_progress), done)

    def _hearing_fetched(self, fetched) -> None:
        """Lets the operator choose the tests of the patient in the window, and saves them as one sheet."""
        received_at = self._now()
        seen_tests = self._seen_tests()
        seen = seen_tests.load()
        try:
            patient = self.patient()
        except ValueError:
            patient = Patient()
        logged = f"\n주고받은 기록: {fetched.log}" if fetched.log else ""
        choice = self._choose_hearing(fetched, seen, patient, self._patient_is_new, received_at)
        if choice is None:
            self._hearing_text = "받은 검사로 결과지를 만들지 않았습니다. 다시 하려면 [청력검사기에서 받기]를 누르세요." + logged
            self._show_hearing()
            return
        result, with_patient = choice
        if not self._result_arrived(result, patient if with_patient else Patient(), received_at):
            self._hearing_text = ""
            self._show_hearing()
            return
        try:
            seen_tests.add(result.fingerprints, self._sheet.name, received_at)
        except OSError as error:
            self._problem = (f"⚠ 받은 검사를 기억해 두지 못했습니다: {error}\n"
                             "다음에 받을 때 이 검사가 새 검사로 다시 보일 수 있습니다.")
            self._show_state()
        left = [test for _, test in fetched.download.new_since(seen | set(result.fingerprints)) if type_of(test)]
        self._hearing_text = f"✔ 청력검사기에서 받은 검사 {len(result.parts)}건으로 결과지를 만들었습니다."
        if left:
            self._hearing_text += (f" 아직 결과지로 만들지 않은 새 검사가 {len(left)}건 있습니다. "
                                   "다른 환자의 것이면 그 환자를 고른 뒤 다시 받으세요.")
        self._hearing_text += logged
        self._show_hearing()

    def edit_hearing_values(self) -> None:
        """Opens the shown hearing result's values for correcting. They go on the sheet as if they
        had come that way; the device's own values stay in the raw data and beside the corrections."""
        if not isinstance(self._measurement, HearingResult) or self._sheet is None or self._sending:
            return
        sheet = self._sheet

        def save(edited: Edited) -> str:
            if self._sheet != sheet:  # another result took the screen while the values were being corrected
                return self._save_values_elsewhere(sheet, edited)
            # The boxes show what is being saved first: sending saves what they hold before it goes.
            typed = (self._note_text(), self._tested_value(), self._remarks_text())
            self._show_edits(edited.note, edited.tested_at, edited.remarks)
            # A sheet that already went to 상쾌한캡처 is not sent again by itself, as with a memo.
            if not self._save_again(self._sheet_patient, edited.note, send=self._settings.cap_on and not self._in_capture(),
                                    tested_at=edited.tested_at, only_edits=True, remarks=edited.remarks,
                                    measurement=edited.result):
                self._show_edits(*typed)
                self._show_state()
                return self._last_save_error
            return ""

        self._open_editor(HearingEditor(
            self, self._measurement, patient=self._sheet_patient, tested_at=self._tested_value(),
            note=self._note_text(), remarks=self._remarks_text(), save=save, confirm=self._confirm, now=self._now))

    def enter_hearing_by_hand(self) -> None:
        """Opens an empty sheet of hearing values to type a result in without the audiometer. It is
        saved like a result received from it, for the patient in the window under the same rules."""
        if not self._settings.hearing_on:
            return
        try:
            patient = self.patient()
        except ValueError:
            patient = Patient()

        def save(edited: Edited) -> str:
            received_at = self._now()
            if not self._result_arrived(edited.result, patient if edited.with_patient else Patient(), received_at,
                                        tested_at=edited.tested_at, note=edited.note, remarks=edited.remarks,
                                        by_hand=True):
                return self._last_save_error
            self._hearing_text = "✔ 직접 입력한 값으로 청력검사 결과지를 만들었습니다."
            self._show_hearing()
            return ""

        self._open_editor(HearingEditor(self, None, patient=patient, patient_is_new=self._patient_is_new,
                                        tested_at=_minute(self._now()), save=save, confirm=self._confirm, now=self._now))

    def _open_editor(self, dialog: HearingEditor) -> None:
        try:
            self._run_editor(dialog)
        finally:
            dialog.deleteLater()

    def _save_values_elsewhere(self, sheet: Path, edited: Edited) -> str:
        """Saves corrected values to a result that is no longer on screen, keeping its patient and what was sent."""
        try:
            saved = load_result(sheet)
            history = saved.capture
            if history and history[-1].get("ok"):
                history += ({"ok": False, "code": EDITED_AFTER_SENDING, "error": "보낸 뒤 결과지를 고침", "retry": True,
                             "at": self._now().isoformat(timespec="seconds")},)
            moved = save_result(edited.result, sheet.parent, saved.received_at, saved.patient, edited.note, history,
                                tested_at=edited.tested_at, clinic=saved.clinic or Clinic(), replacing=sheet,
                                filed=saved.filed, remarks=edited.remarks)
        except Exception as error:
            self._log_unexpected(error)
            return f"고친 값을 저장하지 못했습니다: {error}"
        try:
            refresh_copy(moved)
        except (OSError, ValueError):
            pass  # the result is saved; its copy catches up when it is next filed
        if self._lost is not None and self._lost[0] == sheet:
            self._lost = None  # what could not be saved there went with these values
        self._problem = (f"⚠ 값을 고치는 동안 새 결과가 와서 화면이 바뀌었습니다. 고친 값은 앞 결과지({moved.name})에 "
                         "저장했습니다. [지난 결과 열기...]로 열어 확인해 주세요.")
        self._problem_is_about_edits = False
        self._show_state()
        return ""

    def _seen_tests(self) -> hearing_device.SeenTests:
        folder = self._settings_directory or settings_file.settings_directory()
        return hearing_device.SeenTests(folder / hearing_device.SEEN_FILE)

    def _ask_hearing(self, fetched, seen: set[str], patient: Patient, patient_is_new: bool,
                     received_at: datetime) -> tuple | None:
        dialog = HearingReceiveDialog(self, fetched, seen, patient, patient_is_new, received_at)
        return dialog.choice() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def _show_hearing(self) -> None:
        self.hearing_button.setVisible(self._settings.hearing_on)
        self.hearing_button.setEnabled(not self._receiving)
        self.manual_button.setVisible(self._settings.hearing_on)
        text = self._hearing_text
        if self._receiving and self._hearing_progress:
            text = f"청력검사기에서 받는 중입니다... 검사 {self._hearing_progress}건"
        if not self._settings.hearing_on and not self._receiving:
            text = ""
        self.hearing_label.setText(text)
        self.hearing_label.setVisible(bool(text))


def paint_sheet(sheet: Path, printer: QPrinter) -> None:
    """Draws the sheet on one page of ``printer``, as large as the paper allows. Raises OSError when it cannot."""
    image = QImage(str(sheet))
    if image.isNull():
        raise OSError(f"결과지 파일을 읽지 못했습니다: {sheet.name}")
    printer.setFullPage(True)  # the sheet has its own margins; the printer's would shrink it
    painter = QPainter()
    if not painter.begin(printer):
        raise OSError("프린터를 열지 못했습니다.")
    try:
        paper = printer.pageLayout().fullRectPixels(printer.resolution())
        size = image.size().scaled(paper.size(), Qt.AspectRatioMode.KeepAspectRatio)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRect((paper.width() - size.width()) // 2, (paper.height() - size.height()) // 2,
                                size.width(), size.height()), image)
    finally:
        painter.end()


def usb_adapters(ports: list) -> list:
    return [port for port in ports if "USB" in (port.hwid or "").upper()]


def adapter_identity(port) -> str:
    """What tells one USB serial adapter from another whichever socket it is in: its maker,
    model and serial number. Empty for a port that is not such an adapter."""
    maker, model = getattr(port, "vid", None), getattr(port, "pid", None)
    if maker is None or model is None:
        return ""
    return f"{maker:04X}:{model:04X}:{getattr(port, 'serial_number', None) or ''}"


def pick_port(wanted: str, ports: list, current: str | None = None, heard: bool = False,
              remembered: tuple[str, str] = ("", "")) -> str | None:
    """The port to read the device on, out of the ports the PC has now, or None to read none.

    A port the operator chose (``wanted``) is used when it is there and nothing takes its
    place when it is not. Left to the program, the rules protect two things: a connection
    that works, and the ports of other instruments on the same PC, which they cannot open
    while this program holds them.

    - A port the device has been ``heard`` on is kept for as long as it is there.
    - Where the device was heard before is ``remembered`` as a port name and a USB serial
      adapter (see ``adapter_identity``). That adapter is the device's cable whichever socket
      and name it has now. While it is unplugged, nothing is opened in its place.
    - A port remembered by name only (one of the PC's own, or from a version that did not
      note the adapter) is used while it is there.
    - With nothing to go by, a single USB serial adapter is taken to be the device's. Of
      several, none is opened: the operator is asked to choose. With no adapter at all, a
      port of the PC's own such as a desktop's COM1.
    - Never a Bluetooth port: the device has no radio, and opening one can hang for a long time.
    """
    names = [port.device for port in ports]
    if wanted:
        return wanted if wanted in names else None
    if heard and current in names:
        return current
    adapters = usb_adapters(ports)
    own = [port.device for port in ports if port not in adapters and "BTHENUM" not in (port.hwid or "").upper()]
    remembered_name, remembered_adapter = remembered
    if remembered_adapter:
        same = [port.device for port in adapters if adapter_identity(port) == remembered_adapter]
        if remembered_name in same:
            return remembered_name
        return same[0] if len(same) == 1 else None
    if remembered_name in own or remembered_name in [port.device for port in adapters]:
        return remembered_name
    if len(adapters) == 1:
        return adapters[0].device
    if adapters:
        return None
    return own[0] if own else None


class Device(QObject):
    """The link to the tympanometer: one reader on one port, and a look every few seconds at
    which ports there are, so a cable plugged in or moved after the program started is found."""

    status = Signal(str)
    received = Signal(object)
    heard_on = Signal(str, str)
    """Emitted the first time the device is heard on a port, with the port's name and its adapter."""

    def __init__(self, baud: int, ports: Callable[[], list] = available_ports, reader=SerialWorker,
                 remembered: tuple[str, str] = ("", ""), background: Background = run_in_background,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._baud = baud
        self._ports = ports
        self._reader = reader
        self._remembered = remembered
        self._background = background
        self._wanted = ""
        self._exactly = False
        self._port: str | None = None
        self._adapters: dict[str, str] = {}
        self._worker = None
        self._ending: list = []
        self._said = ""
        self._heard = False
        self._listing = False
        self._search = QTimer(self)
        self._search.setInterval(PORT_SEARCH_MS)
        self._search.timeout.connect(self.look_later)

    def use(self, wanted: str, exactly: bool = False) -> None:
        """Reads from the port ``wanted``, or from one the program finds when ``wanted`` is empty.

        ``exactly`` is for a port named when the program was started: it is opened whether
        or not the PC lists it, as some virtual ports are not listed.
        """
        self._wanted, self._exactly = wanted, exactly and bool(wanted)
        self._said = ""
        self.look(afresh=True)
        self._search.start()

    def stop(self) -> None:
        """Ends reading for good, waiting for the readers to finish, as the program closes."""
        self._search.stop()
        self._stop_reading()
        self._port, self._said = None, ""
        for worker in list(self._ending):
            if not worker.wait(3000):  # stuck opening a port; it must not outlive the objects it uses
                worker.terminate()
                worker.wait(1000)
        self._ending.clear()

    def look(self, afresh: bool = False) -> None:
        """Sees which ports there are and starts, moves or stops reading to match.

        ``afresh`` forgets the port in use, for when the operator has just chosen differently.
        """
        self._settle(self._ports(), afresh)

    def look_later(self) -> None:
        """The look repeated every few seconds. Listing the ports is slow on some PCs, so it
        is done away from the window, which would otherwise stutter under the operator's typing."""
        if self._exactly or self._listing:
            return
        self._listing = True

        def listed(ports, error) -> None:
            self._listing = False
            if error is None and self._search.isActive():
                self._settle(ports)

        self._background(self, self._ports, listed)

    def _settle(self, ports: list, afresh: bool = False) -> None:
        self._adapters = {listed.device: adapter_identity(listed) for listed in ports}
        if self._exactly:
            port = self._wanted
        else:
            port = pick_port(self._wanted, ports, None if afresh else self._port, self._heard and not afresh,
                             self._remembered)
        if port is None:
            missing = self._why_none(ports)
            if self._port is not None or missing != self._said:
                self._stop_reading()
                self._port, self._said = None, missing
                self.status.emit(missing)
            return
        if port == self._port:
            return
        self._stop_reading()
        self._port, self._said, self._heard = port, "", False
        self._worker = self._reader(port, self._baud)
        self._worker.status.connect(self.status)
        self._worker.received.connect(self._hear)
        self._worker.start()

    def _why_none(self, ports: list) -> str:
        if self._wanted:
            return f"{self._wanted} 포트가 보이지 않습니다. 케이블을 꽂으면 자동으로 연결합니다."
        adapters = usb_adapters(ports)
        if self._remembered[1] and not any(adapter_identity(port) == self._remembered[1] for port in adapters):
            return ("장비의 USB-시리얼 케이블이 연결되어 있지 않습니다. 꽂으면 자동으로 연결합니다. "
                    "케이블을 다른 것으로 바꿨다면 [설정] → 장비 · 저장에서 포트를 골라 주세요.")
        if adapters:
            return (f"USB-시리얼 포트({', '.join(port.device for port in adapters)}) 중 어느 것이 장비인지 알 수 없어 "
                    "열지 않았습니다. [설정] → 장비 · 저장에서 장비가 연결된 포트를 골라 주세요.")
        return NO_PORT

    def _hear(self, event) -> None:
        if isinstance(event, (Measurement, PowerOn)) and not self._heard:
            self._heard = True  # only this device speaks like that: the port is the right one
            self._remembered = (self._port, self._adapters.get(self._port, ""))
            self.heard_on.emit(*self._remembered)
        self.received.emit(event)

    def _stop_reading(self) -> None:
        """Tells the reader to end and lets it, without holding the window up: closing a port
        can take a moment, and a reader must not be destroyed while it still runs."""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.status.disconnect(self.status)
        worker.received.disconnect(self._hear)
        self._ending.append(worker)
        worker.finished.connect(lambda: self._ended(worker))
        worker.requestInterruption()
        if worker.isFinished():
            self._ended(worker)

    def _ended(self, worker) -> None:
        if worker in self._ending:
            self._ending.remove(worker)


def only_instance(folder: Path, wait_ms: int) -> QLockFile | None:
    """The lock that makes this the one running window, or None when another already holds it.

    Two windows would fight over the device's port. Waiting a little lets a program that is
    restarting itself after an update finish closing first.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock = QLockFile(str(folder / "running.lock"))
    lock.setStaleLockTime(0)  # a lock is stale when its program has gone, however recently it was taken
    if lock.tryLock(wait_ms) or lock.error() != QLockFile.LockError.LockFailedError:
        return lock  # held, or the folder cannot hold a lock at all, which must not stop the program starting
    return None


def results_folder(chosen_on_start: str | None, settings: Settings) -> tuple[Path, bool]:
    """Where results go, and whether that follows the settings from now on (not when given on starting)."""
    if chosen_on_start:
        return Path(chosen_on_start), False
    return settings_file.results_directory(settings.results_dir), True


def write_error_log(directory: Path, text: str) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "error.log", "a", encoding="utf-8") as log:
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} v{__version__}\n{text}\n")
    except OSError:
        pass


def report_crashes(directory: Path) -> None:
    """Writes unexpected errors to ``error.log`` and shows them; started from a shortcut there is no console."""

    def hook(kind, error, trace) -> None:
        write_error_log(directory, "".join(traceback.format_exception(kind, error, trace)))
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "TSM300 오류", f"예상하지 못한 오류가 났습니다.\n\n{error}\n\n"
                                 f"자세한 내용은 {directory / 'error.log'} 에 적었습니다.")

    sys.excepthook = hook


INSTANCE_WAIT_MS = 8_000
INSTANCE_CHECK_MS = 300


def run(args) -> int:
    claim_taskbar_identity()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon(str(ICON)))
    folder = settings_file.settings_directory()
    after_update = os.environ.pop(update_module.RESTARTED, None) is not None
    lock = only_instance(folder, INSTANCE_WAIT_MS if after_update else INSTANCE_CHECK_MS)
    if lock is None:
        QMessageBox.information(None, "TSM300", ALREADY_RUNNING)
        return 1
    usual_hook = sys.excepthook
    report_crashes(folder)
    try:
        return _run_window(app, args)
    finally:
        sys.excepthook = usual_hook
        lock.unlock()


def _run_window(app: QApplication, args) -> int:
    prepare()
    settings = settings_file.load()
    directory, follows_settings = results_folder(args.out, settings)
    window = MainWindow(directory, settings, directory_follows_settings=follows_settings)
    device = Device(args.baud, remembered=(settings.port_heard, settings.port_heard_id))
    device.status.connect(window.show_connection)
    device.received.connect(window.handle_event)
    device.heard_on.connect(window.remember_port)
    window.port_wanted.connect(device.use)
    device.use(args.port or settings.port, exactly=bool(args.port))
    window.show()
    program = update_module.program_file()
    if program is not None:  # just after an update the replaced file is still in use by the program that started this one
        QTimer.singleShot(20_000, lambda: update_module.clean_up(program))
    code = app.exec()
    device.stop()
    window.close()
    window.deleteLater()
    app.processEvents()
    return code


def _full(path: Path) -> Path:
    """The path spelled out in full. Sheets are told apart by their paths, and the same sheet
    reached through a folder given in short and through a file picker must compare equal."""
    return Path(os.path.abspath(path))


def _minute(moment: datetime) -> datetime:
    """Test times are shown, corrected and compared to the minute."""
    return moment.replace(second=0, microsecond=0)


def _who(patient: Patient) -> str:
    return " ".join(part for part in (patient.name, f"({patient.chart_no})" if patient.chart_no else "") if part)


def _details(patient: Patient, today) -> str:
    age = patient.age_on(today)
    sex_age = " ".join(part for part in (patient.sex, f"{age}세" if age is not None else "") if part)
    birth = patient.birth_date.isoformat() if patient.birth_date else ""
    return " · ".join(part for part in (birth, sex_age) if part)
