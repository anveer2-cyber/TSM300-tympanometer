"""The operator's window: choose or type the patient, receive results, save the result sheet."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

import serial
from PySide6.QtCore import QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from . import __version__
from .console import available_ports, open_port
from .listen import RETRY_AFTER, open_file, read_events
from .patient import Order, Patient, PatientSource, parse_birth_date
from .protocol import Corrupt, Measurement, PowerOn
from .report import prepare
from .store import delete_result, save_result

NO_SOURCE_NOTE = "차트번호 조회와 오늘 검사 오더 목록은 병원 전산 연동 후에 쓸 수 있습니다. 지금은 직접 입력해 주세요."
WAITING = "검사 결과를 기다리는 중입니다. 검사 후 장비의 → 키를 누르세요."
SAVE_LABEL = "이 환자 정보로 저장"
REPLACE_LABEL = "직전 결과지의 환자 바꾸기..."


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


class MainWindow(QMainWindow):
    def __init__(self, directory: Path, source: PatientSource | None = None,
                 now: Callable[[], datetime] = datetime.now,
                 confirm: Callable[[str], bool] | None = None) -> None:
        super().__init__()
        self._directory = directory
        self._source = source
        self._now = now
        self._confirm = confirm or self._ask
        self._connection = "포트에 연결하지 않았습니다."
        self._measurement: Measurement | None = None
        self._received_at: datetime | None = None
        self._sheet: Path | None = None
        self._sheet_patient = Patient()
        self._patient_is_new = False
        self._orders: list[Order] = []
        self.setWindowTitle(f"TSM-300 임피던스 청력검사 v{__version__}")
        self.resize(1180, 820)
        self._build()
        self.refresh_orders()
        self._show_state()

    # ----- layout -------------------------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        columns = QHBoxLayout(central)
        columns.setContentsMargins(14, 14, 14, 14)
        columns.setSpacing(14)

        side = QVBoxLayout()
        side.setSpacing(12)
        side.addWidget(self._build_orders(), stretch=1)
        side.addWidget(self._build_patient_form())
        if self._source is None:
            side.addStretch(1)
        side_holder = QWidget()
        side_holder.setLayout(side)
        side_holder.setFixedWidth(360)
        columns.addWidget(side_holder)

        main = QVBoxLayout()
        main.setSpacing(10)
        self.connection_label = QLabel(self._connection)
        self.connection_label.setStyleSheet("color: #666666;")
        main.addWidget(self.connection_label)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("font-size: 15px; font-weight: 600; padding: 10px 12px;"
                                       " background: #f3f3f3; border-radius: 6px;")
        main.addWidget(self.state_label)
        self.preview = SheetPreview()
        main.addWidget(self.preview, stretch=1)
        buttons = QHBoxLayout()
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.clicked.connect(self.save_with_patient)
        self.open_button = QPushButton("결과지 열기")
        self.open_button.clicked.connect(self._open_sheet)
        folder_button = QPushButton("저장 폴더 열기")
        folder_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(folder_button)
        main.addLayout(buttons)
        columns.addLayout(main, stretch=1)

    def _build_orders(self) -> QGroupBox:
        box = QGroupBox("오늘 검사 오더")
        box.setVisible(self._source is not None)
        layout = QVBoxLayout(box)
        self.orders_note = QLabel()
        self.orders_note.setWordWrap(True)
        self.orders_note.setStyleSheet("color: #666666;")
        layout.addWidget(self.orders_note)
        self.orders_list = QListWidget()
        self.orders_list.itemClicked.connect(self._order_chosen)
        layout.addWidget(self.orders_list, stretch=1)
        self.refresh_button = QPushButton("새로고침")
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
        self.source_note.setVisible(self._source is None)
        layout.addWidget(self.source_note)
        for edit in (self.chart_edit, self.name_edit, self.birth_edit):
            edit.textEdited.connect(self._patient_edited)
        self.sex_combo.activated.connect(self._patient_edited)
        return box

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
        self.chart_edit.setText(patient.chart_no)
        self.name_edit.setText(patient.name)
        self.sex_combo.setCurrentText(patient.sex)
        self.birth_edit.setText(patient.birth_date.isoformat() if patient.birth_date else "")
        self._patient_edited()

    def clear_patient(self) -> None:
        self.set_patient(Patient())
        self.orders_list.clearSelection()
        self.chart_edit.setFocus()

    def find_patient(self) -> None:
        chart_no = self.chart_edit.text().strip()
        if self._source is None or not chart_no:
            return
        try:
            found = self._source.find(chart_no)
        except Exception as error:  # whatever the records system raises must not close the window
            self.form_note.setText(f"조회하지 못했습니다: {error}")
            return
        if found is None:
            self.form_note.setText(f"차트번호 {chart_no} 환자를 찾지 못했습니다.")
            return
        self.set_patient(found)

    def refresh_orders(self) -> None:
        self.find_button.setVisible(self._source is not None)
        self.orders_list.clear()
        if self._source is None:
            return
        try:
            self._orders = self._source.todays_orders()
        except Exception as error:  # whatever the records system raises must not close the window
            self._orders = []
            self.orders_note.setText(f"오더 목록을 가져오지 못했습니다: {error}")
            return
        self.orders_note.setText(
            f"{len(self._orders)}명 · 환자를 누르면 아래에 채워집니다." if self._orders
            else "오늘 임피던스 검사 오더가 없습니다."
        )
        today = self._now().date()
        for order in self._orders:
            patient = order.patient
            age = patient.age_on(today)
            who = "/".join(part for part in (patient.sex, f"{age}세" if age is not None else "") if part)
            text = "  ".join(part for part in (order.time, patient.chart_no, patient.name,
                                               f"({who})" if who else "", order.label) if part)
            self.orders_list.addItem(QListWidgetItem(text))

    def _order_chosen(self, item: QListWidgetItem) -> None:
        self.set_patient(self._orders[self.orders_list.row(item)].patient)

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
        if self._measurement is None or self._sheet is None:
            return
        try:
            patient = self.patient()
        except ValueError:
            self._show_state()
            return
        if patient.is_empty or patient == self._sheet_patient:
            return
        if not self._sheet_patient.is_empty and not self._confirm(
                f"직전 결과지는 {_who(self._sheet_patient)} 환자로 저장되어 있습니다.\n"
                f"{_who(patient)} 환자의 결과로 바꿀까요?"):
            return
        delete_result(self._sheet)
        self._save(patient)

    def _measurement_received(self, measurement: Measurement) -> None:
        if measurement.left is None and measurement.right is None:
            self.state_label.setText(
                f"장비의 ID {measurement.memory_id} 에는 저장된 검사가 없습니다. 검사 후 다시 전송해 주세요.")
            return
        if self._measurement is not None and measurement.raw == self._measurement.raw:
            self.state_label.setText("같은 결과를 다시 받았습니다. 이미 저장되어 있습니다.")
            return
        self._measurement = measurement
        self._received_at = self._now()
        try:
            patient = self.patient() if self._patient_is_new else Patient()
        except ValueError:
            patient = Patient()
        self._save(patient)

    def _save(self, patient: Patient) -> None:
        self._sheet = save_result(self._measurement, self._directory, self._received_at, patient)
        self._sheet_patient = patient
        self._patient_is_new = False
        self.preview.show_sheet(self._sheet)
        self._show_state()

    # ----- display ------------------------------------------------------------------------

    def _show_state(self) -> None:
        try:
            patient = self.patient()
            self.form_note.setText("")
        except ValueError as error:
            patient = Patient()
            self.form_note.setText(str(error))
        has_result = self._measurement is not None
        saved_for = self._sheet_patient
        next_up = not patient.is_empty and self._patient_is_new
        self.open_button.setEnabled(has_result)
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
            self.save_button.setEnabled(True)
            text = (f"✔ 직전 결과는 {_who(saved_for)} 환자로 저장되어 있습니다. "
                    f"다음 검사는 {_who(patient)} 환자로 저장됩니다.")
        self.state_label.setText(text)

    def _open_sheet(self) -> None:
        if self._sheet is not None:
            open_file(self._sheet)

    def _open_folder(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        open_file(self._directory)

    def _ask(self, question: str) -> bool:
        answer = QMessageBox.question(self, "환자 바꾸기", question)
        return answer == QMessageBox.StandardButton.Yes


def run(args) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    prepare()
    window = MainWindow(Path(args.out))
    port = args.port or _pick_port(QSettings("TSM300", "TSM300"))
    worker = None
    if port:
        worker = SerialWorker(port, args.baud)
        worker.status.connect(window.show_connection)
        worker.received.connect(window.handle_event)
        worker.start()
    else:
        window.show_connection("시리얼 포트를 찾지 못했습니다. 케이블을 연결하고 프로그램을 다시 켜 주세요.")
    window.show()
    code = app.exec()
    if worker is not None:
        worker.requestInterruption()
        worker.wait(3000)
    window.close()
    window.deleteLater()
    app.processEvents()
    return code


def _pick_port(settings: QSettings) -> str | None:
    """The port used last time if it is still there, otherwise the first USB serial adapter."""
    ports = available_ports()
    names = [port.device for port in ports]
    remembered = settings.value("port")
    if remembered in names:
        return remembered
    usb = [port.device for port in ports if "USB" in (port.hwid or "").upper()]
    chosen = (usb or names or [None])[0]
    if chosen:
        settings.setValue("port", chosen)
    return chosen


def _who(patient: Patient) -> str:
    return " ".join(part for part in (patient.name, f"({patient.chart_no})" if patient.chart_no else "") if part)
