"""The operator's window: choose or type the patient, receive results, save and send the result sheet."""

from __future__ import annotations

import sys
import threading
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import serial
from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__, links
from . import update as update_module
from . import settings as settings_file
from .console import available_ports, open_port
from .listen import RETRY_AFTER, open_file, read_events
from .patient import Order, Patient, PatientSource, parse_birth_date
from .protocol import Corrupt, Measurement, PowerOn
from .records import Records, charts_tested_on, records_from_settings, send_sheet
from .report import prepare, wrap_note
from .settings import Settings
from .shortcut import ICON, claim_taskbar_identity
from .store import delete_result, load_result, save_result

NO_SOURCE_NOTE = "차트번호 조회와 오늘 검사 오더 목록은 [설정]에서 환자 조회를 연결하면 쓸 수 있습니다. 지금은 직접 입력해 주세요."
WAITING = "검사 결과를 기다리는 중입니다. 검사 후 장비의 → 키를 누르세요."
SAVE_LABEL = "이 환자 정보로 저장"
REPLACE_LABEL = "직전 결과지의 환자 바꾸기..."
EDITED_AFTER_SENDING = "edited"
ORDER_COLUMNS = ("접수", "차트번호", "이름", "나이·성별", "진료실", "검사")
ORDERS_REFRESH_MS = 30_000
DONE_INK = QColor("#9a9a9a")

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
    """Where patients are looked up and where result sheets are sent."""

    def __init__(self, parent: QWidget | None, settings: Settings, background: Background = run_in_background) -> None:
        super().__init__(parent)
        self._original = settings
        self._background = background
        self.setWindowTitle("설정")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        lookup = QGroupBox("환자 조회")
        form = QFormLayout(lookup)
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
        layout.addWidget(lookup)

        capture = QGroupBox("상쾌한캡처 (결과지 보내기)")
        form = QFormLayout(capture)
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
        layout.addWidget(capture)

        buttons = QDialogButtonBox()
        buttons.addButton("저장", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.via_combo.currentIndexChanged.connect(self._show_fields)
        self._show_fields()

    def settings(self) -> Settings:
        """The settings as edited in the dialog."""
        return replace(
            self._original,
            emr_via=self.via_combo.currentData(),
            emr_dsn=self.dsn_edit.text().strip(),
            emr_server=self.server_edit.text().strip(),
            emr_token=self.token_edit.text().strip(),
            cap_url=self.cap_url_edit.text().strip(),
            cap_key=self.cap_key_edit.text().strip(),
            cap_on=self.cap_on_check.isChecked(),
        )

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

    def _show_fields(self) -> None:
        via = self.via_combo.currentData()
        self.dsn_edit.setEnabled(via == "odbc")
        self.server_edit.setEnabled(via == "server")
        self.token_edit.setEnabled(via == "server")
        self.lookup_test_button.setEnabled(bool(via))


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
    def __init__(self, directory: Path, settings: Settings | None = None, source: PatientSource | None = None,
                 now: Callable[[], datetime] = datetime.now,
                 confirm: Callable[[str], bool] | None = None,
                 confirm_patient: Callable[[Patient], bool] | None = None,
                 background: Background = run_in_background,
                 send: Callable[..., dict] = send_sheet,
                 settings_directory: Path | None = None, updates=update_module) -> None:
        super().__init__()
        self._directory = directory
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
        self._patient_is_new = False
        self._orders: list[Order] = []
        self._loading_orders = False
        self._sending = False
        self._sent: dict | None = None
        self.setWindowTitle(f"TSM-300 임피던스 청력검사 v{__version__}")
        self.resize(1240, 840)
        self._build()
        self._orders_timer = QTimer(self)
        self._orders_timer.setInterval(ORDERS_REFRESH_MS)
        self._orders_timer.timeout.connect(self.refresh_orders)
        self._orders_timer.start()
        self._show_source()
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
        self.capture_label = QLabel()
        self.capture_label.setWordWrap(True)
        self.capture_label.setStyleSheet("padding: 0 12px;")
        main.addWidget(self.capture_label)
        self.preview = SheetPreview()
        main.addWidget(self.preview, stretch=1)
        buttons = QHBoxLayout()
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.clicked.connect(self.save_with_patient)
        self.send_button = QPushButton("상쾌한캡처로 보내기")
        self.send_button.clicked.connect(self.send_to_capture)
        self.open_button = QPushButton("결과지 열기")
        self.open_button.clicked.connect(self._open_sheet)
        earlier_button = QPushButton("지난 결과 열기...")
        earlier_button.clicked.connect(self._choose_result)
        folder_button = QPushButton("저장 폴더 열기")
        folder_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(earlier_button)
        buttons.addWidget(folder_button)
        main.addLayout(buttons)
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
        form.addRow("성별", self.sex_combo)
        self.birth_edit = QLineEdit()
        self.birth_edit.setPlaceholderText("예: 1980-01-31 또는 19800131")
        form.addRow("생년월일", self.birth_edit)
        layout.addLayout(form)
        self.form_note = QLabel()
        self.form_note.setWordWrap(True)
        self.form_note.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.form_note)
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
        box = QGroupBox("메모 (화면의 결과지에 인쇄됩니다)")
        layout = QVBoxLayout(box)
        self.note_edit = QPlainTextEdit()
        self.note_edit.setFixedHeight(96)
        self.note_edit.setPlaceholderText("결과를 받은 뒤에 쓸 수 있습니다")
        self.note_edit.textChanged.connect(self._show_state)
        layout.addWidget(self.note_edit)
        self.note_hint = QLabel()
        self.note_hint.setWordWrap(True)
        self.note_hint.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.note_hint)
        self.note_button = QPushButton("메모 저장")
        self.note_button.clicked.connect(self.save_note)
        layout.addWidget(self.note_button)
        return box

    # ----- settings -----------------------------------------------------------------------

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self._settings, self._background)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.apply_settings(dialog.settings())

    def apply_settings(self, settings: Settings) -> None:
        """Saves the settings and switches patient lookup and sending over to them."""
        settings_file.save(settings, self._settings_directory)
        if isinstance(self._source, Records):
            self._source.close()
        self._settings = settings
        self._source = records_from_settings(settings)
        self._show_source()
        self._show_state()

    def open_update(self) -> None:
        if UpdateDialog(self, self._updates, self._background).exec() == QDialog.DialogCode.Accepted:
            self.restart_updated()

    def restart_updated(self) -> None:
        """Starts the newly installed program and closes this one, losing nothing typed."""
        self._keep_unsaved_note()
        try:
            self._updates.restart(self._updates.program_file(), sys.argv[1:])
        except OSError as error:
            QMessageBox.warning(self, "업데이트", f"새 버전을 켜지 못했습니다. 프로그램을 닫고 직접 다시 켜 주세요.\n\n{error}")
            return
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
        done_charts = charts_tested_on(self._directory, today)
        chosen = self.chart_edit.text().strip()
        self.orders_table.setRowCount(len(self._orders))
        for row, order in enumerate(self._orders):
            patient = order.patient
            age = patient.age_on(today)
            who = " ".join(part for part in (f"{age}세" if age is not None else "", patient.sex) if part)
            tested = patient.chart_no in done_charts
            cells = (order.time, patient.chart_no, patient.name, who, order.room, "✓" if tested else "")
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not order.selectable:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                if tested or not order.selectable:
                    item.setForeground(DONE_INK)
                self.orders_table.setItem(row, column, item)
            if order.selectable and chosen and patient.chart_no == chosen:
                self.orders_table.selectRow(row)
        waiting = sum(1 for order in self._orders if order.selectable and order.patient.chart_no not in done_charts)
        if self._orders:
            self.orders_note.setText(
                f"{len(self._orders)}명 (검사 전 {waiting}명) · {self._now():%H:%M:%S} 읽음 · 30초마다 다시 읽습니다")
        else:
            self.orders_note.setText(f"오늘 임피던스 검사 오더가 없습니다. · {self._now():%H:%M:%S} 읽음")

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
            if self._sent and self._sent.get("ok"):
                question += (f"\n\n이 결과지는 이미 상쾌한캡처에 {_who(self._sheet_patient)} 환자로 들어가 있습니다. "
                             "그쪽 것은 상쾌한캡처 사진 이력에서 '중이검사' 카드를 오른쪽 클릭해 [삭제…]로 지워 주세요.")
            if not self._confirm(question):
                return
        self._patient_is_new = False
        self._save_again(patient, self._note_text(), send=self._settings.cap_on)

    def save_note(self) -> None:
        """Prints the memo on the shown sheet. A sheet already sent is not sent again by itself,
        because 상쾌한캡처 would keep both copies."""
        if self._measurement is None or self._sheet is None or self._sending:
            return
        note = self._note_text()
        if note == self._sheet_note:
            return
        was_sent = bool(self._sent and self._sent.get("ok"))
        self._save_again(self._sheet_patient, note, send=self._settings.cap_on and not was_sent)

    def open_result(self, path: Path) -> None:
        """Shows a result saved earlier, so that its memo or patient can be corrected."""
        try:
            saved = load_result(path)
        except ValueError as error:
            self.state_label.setText(f"⚠ {error}")
            return
        self._keep_unsaved_note()
        self._measurement, self._received_at = saved.measurement, saved.received_at
        self._sheet, self._sheet_patient, self._sheet_note = saved.sheet, saved.patient, saved.note
        self._sent = dict(saved.capture[-1]) if saved.capture else None
        self._fill_form(saved.patient)
        self._patient_is_new = False
        self.note_edit.setPlainText(saved.note)
        self.preview.show_sheet(self._sheet)
        self._show_capture()
        self._show_state()

    def _choose_result(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "지난 결과 열기", str(self._directory), "결과 (*.json)")
        if chosen:
            self.open_result(Path(chosen))

    def send_to_capture(self) -> None:
        """Sends the shown sheet to 상쾌한캡처, which checks the chart number and name against the records."""
        if not self._can_send():
            return
        sheet, patient, tested_on, settings = self._sheet, self._sheet_patient, self._received_at.date(), self._settings
        internal_number = self._source.internal_number(patient.chart_no) if self._source is not None else ""
        self._sending = True
        self.capture_label.setText("상쾌한캡처로 보내는 중...")
        self._show_state()

        def done(outcome, error) -> None:
            self._sending = False
            if sheet == self._sheet:
                self._sent = outcome if error is None else {"ok": False, "code": "error", "error": str(error), "retry": True}
                self._show_capture()
            self._show_state()
            self._show_orders()

        self._background(self, lambda: self._send(sheet, patient, tested_on, settings, internal_number), done)

    def _can_send(self) -> bool:
        return (self._sheet is not None and not self._sending and self._settings.capture_ready
                and bool(self._sheet_patient.chart_no and self._sheet_patient.name)
                and not (self._sent and self._sent.get("ok")))

    def _measurement_received(self, measurement: Measurement) -> None:
        if measurement.left is None and measurement.right is None:
            self.state_label.setText(
                f"장비의 ID {measurement.memory_id} 에는 저장된 검사가 없습니다. 검사 후 다시 전송해 주세요.")
            return
        if self._measurement is not None and measurement.raw == self._measurement.raw:
            self.state_label.setText("같은 결과를 다시 받았습니다. 이미 저장되어 있습니다.")
            return
        try:
            patient = self.patient() if self._patient_is_new else Patient()
        except ValueError:
            patient = Patient()
        self._keep_unsaved_note()
        self._measurement = measurement
        self._received_at = self._now()
        self._sent = None
        self._patient_is_new = False
        self.note_edit.setPlainText("")
        self._saved(save_result(measurement, self._directory, self._received_at, patient), patient, "")
        if self._settings.cap_on:
            self.send_to_capture()

    def _save_again(self, patient: Patient, note: str, send: bool) -> None:
        """Rewrites the shown result with these details, keeping the record of what was sent."""
        try:
            history = load_result(self._sheet).capture
        except ValueError:
            history = ()
        if self._sent and self._sent.get("ok"):
            edited = {"ok": False, "code": EDITED_AFTER_SENDING, "error": "보낸 뒤 결과지를 고침", "retry": True,
                      "at": self._now().isoformat(timespec="seconds")}
            history += (edited,)
            self._sent = edited
        elif patient != self._sheet_patient:
            self._sent = None
        directory = self._sheet.parent
        delete_result(self._sheet)
        self._saved(save_result(self._measurement, directory, self._received_at, patient, note, history), patient, note)
        if send:
            self.send_to_capture()

    def _saved(self, sheet: Path, patient: Patient, note: str) -> None:
        self._sheet, self._sheet_patient, self._sheet_note = sheet, patient, note
        self.preview.show_sheet(sheet)
        self._show_capture()
        self._show_state()
        self._show_orders()

    def _keep_unsaved_note(self) -> None:
        """Saves a memo typed for the shown result before another result takes its place."""
        if self._measurement is not None and self._sheet is not None and self._note_text() != self._sheet_note:
            self._save_again(self._sheet_patient, self._note_text(), send=False)

    def _note_text(self) -> str:
        return self.note_edit.toPlainText().strip()

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
        next_up = not patient.is_empty and self._patient_is_new
        self.open_button.setEnabled(has_result)
        self.note_edit.setEnabled(has_result)
        self.note_button.setEnabled(has_result and not self._sending and self._note_text() != self._sheet_note)
        self.note_hint.setText("메모가 길어 결과지에는 앞부분만 인쇄됩니다." if wrap_note(self._note_text())[1] else "")
        self.send_button.setVisible(self._settings.capture_ready)
        self.send_button.setEnabled(self._can_send())
        self.save_button.setText(SAVE_LABEL)
        self.save_button.setEnabled(False)
        if not has_result:
            text = WAITING
            if next_up:
                text = f"다음 검사는 {_who(patient)} 환자로 저장됩니다. " + WAITING
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

    def _open_sheet(self) -> None:
        if self._sheet is not None:
            open_file(self._sheet)

    def _open_folder(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        open_file(self._directory)

    def _ask(self, question: str) -> bool:
        answer = QMessageBox.question(self, "환자 바꾸기", question)
        return answer == QMessageBox.StandardButton.Yes

    def _ask_patient(self, patient: Patient) -> bool:
        return PatientConfirm(self, patient, self._now().date()).exec() == QDialog.DialogCode.Accepted


def report_crashes(directory: Path) -> None:
    """Writes unexpected errors to ``error.log`` and shows them; started from a shortcut there is no console."""

    def hook(kind, error, trace) -> None:
        text = "".join(traceback.format_exception(kind, error, trace))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / "error.log", "a", encoding="utf-8") as log:
                log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} v{__version__}\n{text}\n")
        except OSError:
            pass
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "TSM300 오류", f"예상하지 못한 오류가 났습니다.\n\n{error}\n\n"
                                 f"자세한 내용은 {directory / 'error.log'} 에 적었습니다.")

    sys.excepthook = hook


def run(args) -> int:
    claim_taskbar_identity()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon(str(ICON)))
    usual_hook = sys.excepthook
    report_crashes(settings_file.settings_directory())
    try:
        return _run_window(app, args)
    finally:
        sys.excepthook = usual_hook


def _run_window(app: QApplication, args) -> int:
    prepare()
    settings = settings_file.load()
    window = MainWindow(Path(args.out), settings)
    port = args.port or _pick_port(settings)
    worker = None
    if port:
        worker = SerialWorker(port, args.baud)
        worker.status.connect(window.show_connection)
        worker.received.connect(window.handle_event)
        worker.start()
    else:
        window.show_connection("시리얼 포트를 찾지 못했습니다. 케이블을 연결하고 프로그램을 다시 켜 주세요.")
    window.show()
    program = update_module.program_file()
    if program is not None:  # just after an update the replaced file is still in use by the program that started this one
        QTimer.singleShot(20_000, lambda: update_module.clean_up(program))
    code = app.exec()
    if worker is not None:
        worker.requestInterruption()
        worker.wait(3000)
    window.close()
    window.deleteLater()
    app.processEvents()
    return code


def _pick_port(settings: Settings) -> str | None:
    """The port used last time if it is still there, otherwise the first USB serial adapter."""
    ports = available_ports()
    names = [port.device for port in ports]
    if settings.port in names:
        return settings.port
    usb = [port.device for port in ports if "USB" in (port.hwid or "").upper()]
    chosen = (usb or names or [None])[0]
    if chosen:
        settings.port = chosen
        settings_file.save(settings)
    return chosen


def _who(patient: Patient) -> str:
    return " ".join(part for part in (patient.name, f"({patient.chart_no})" if patient.chart_no else "") if part)


def _details(patient: Patient, today) -> str:
    age = patient.age_on(today)
    sex_age = " ".join(part for part in (patient.sex, f"{age}세" if age is not None else "") if part)
    birth = patient.birth_date.isoformat() if patient.birth_date else ""
    return " · ".join(part for part in (birth, sex_age) if part)
