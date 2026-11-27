"""The window in which the values of a hearing result are corrected, or a whole result is typed in by hand.

Pure tone (and soundfield, UCL, MCL) values sit in one table per ear: a row per curve with its
masking row under it, a column per frequency, and the 6분법 average beside, worked out as the
operator types. Speech values sit in a table per curve and ear, a row per point measured; the
high frequencies, when the result has them, in tables like the pure tone ones. The tinnitus
tests, which the device's data does not give, are typed as free text per ear.

Nothing is changed until [저장], and then only through ``save``, which the main window gives.
What the device sent is never changed: the result keeps the corrections beside it (``record``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QDateTime, QEvent, QObject, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPlainTextEdit, QScrollArea, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..patient import Patient
from ..report import LEFT_EAR, RIGHT_EAR, wrap_note
from . import edits
from .protocol import HF_FREQUENCIES, TONE_FREQUENCIES, ToneCurve
from .pta import average
from .record import (
    AVERAGED, EAR_NAMES, HIGH_FREQUENCY, KIND_NAMES, SPEECH, SPEECH_POINTS, SRT, TINNITUS_ITEMS, TINNITUS_NAMES,
    TINNITUS_TEXT, TONE, HearingResult, Tinnitus,
)

TONE_ROWS = ("Ac", "Bc", "Spkr", "SpkrA", "AcU", "AcM")
SPEECH_KINDS = ("Ac", "Bc", "Spkr", "SpkrA")
HIGH_FREQUENCY_ROWS = ("Ac", "Spkr")
USUAL_FREQUENCIES = tuple(frequency for frequency in TONE_FREQUENCIES if frequency <= 8000)
EXTRA_FREQUENCIES = tuple(frequency for frequency in TONE_FREQUENCIES if frequency > 8000)
EARS = (("R", "오른쪽 (R)", RIGHT_EAR), ("L", "왼쪽 (L)", LEFT_EAR))
EARLIEST_TEST = datetime(2000, 1, 1)

BAD_CELL = QColor("#f9d6d2")
AVERAGE_CELL = QColor("#f3f3f3")
SECONDARY = QColor("#666666")

HOW_TO = ("칸을 누르고 dB 값을 칩니다. Tab 이나 Enter 로 옆 칸으로 갑니다. Delete 로 지웁니다.\n"
          "무반응: 숫자 뒤에 n (90n → 90↓). 칸을 골라 오른쪽 클릭해도 됩니다.\n"
          "차폐 줄: 차폐 크기(dB). 크기를 모르면 ? 를 넣습니다. 비우면 차폐하지 않은 것입니다.\n"
          "없는 줄도 채우면 결과지에 곡선이 생깁니다. 저장하면 결과지에 그대로 인쇄됩니다.")
SPEECH_HOW_TO = ("점마다 강도, 정답률, 차폐, 맞힌 단어 수와 단어 수를 넣습니다. 쓰지 않는 칸은 비워 둡니다.\n"
                 "정답률을 비우고 단어 수를 넣으면 정답률은 맞힌 수 ÷ 단어 수로 채웁니다. "
                 "결과지에는 정답률이 가장 높은 점이 나옵니다.")
TINNITUS_HOW_TO = ("이명도검사 값을 적힌 그대로 넣습니다 (예: 1500, 28 dB, 85 dB (o)). 칸마다 아무 글이나 됩니다.\n"
                   "이명 주파수와 이명 크기를 넣으면 청력도에 T 로 표시합니다. 크기는 dB HL 로 읽고, "
                   "10 dB SL 처럼 SL 이면 그 주파수의 기도 역치에 더합니다. SPL 이면 표에만 나옵니다.")
INHIBITION_CHOICES = ("", "완전 억제", "부분 억제", "억제 없음", "반동")
NOT_SAVED = "고친 값을 저장하지 않고 닫을까요?"


def who(patient: Patient) -> str:
    return " ".join(part for part in (patient.name, f"({patient.chart_no})" if patient.chart_no else "") if part)


def frequency_label(frequency: int) -> str:
    return str(frequency) if frequency < 1000 else f"{frequency / 1000:g}k"


@dataclass(frozen=True)
class Edited:
    """What the operator saved: the result with its new values, and the sheet's other details."""

    result: HearingResult
    tested_at: datetime
    note: str
    remarks: str
    with_patient: bool
    """Typed in by hand for the patient in the window (always False when correcting a saved result)."""


@dataclass(frozen=True)
class Problem:
    text: str
    widget: QWidget
    row: int = -1
    column: int = -1


class _NoWheel(QObject):
    """A test date the mouse wheel merely passed over would be printed without anyone having meant it."""

    def eventFilter(self, watched, event) -> bool:
        return event.type() == QEvent.Type.Wheel


class Grid(QTableWidget):
    """A table the operator types numbers into: Enter goes on to the next cell, Delete empties the chosen ones."""

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__(rows, columns)
        self.setEditTriggers(QAbstractItemView.EditTrigger.AnyKeyPressed | QAbstractItemView.EditTrigger.DoubleClicked
                             | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.verticalHeader().setDefaultSectionSize(22)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setMinimumSectionSize(38)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._original: dict[tuple[int, int], str] = {}
        for row in range(rows):
            for column in range(columns):
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.setItem(row, column, item)
        self.setFixedHeight(self.horizontalHeader().sizeHint().height() + 22 * rows + 4)

    def text_at(self, row: int, column: int) -> str:
        item = self.item(row, column)
        return item.text().strip() if item is not None else ""

    def put(self, row: int, column: int, text: str) -> None:
        """Puts a value in a cell as the operator would type it."""
        self.item(row, column).setText(text)

    def keep_original(self) -> None:
        self._original = {(row, column): self.text_at(row, column)
                          for row in range(self.rowCount()) for column in range(self.columnCount())}

    def original(self, row: int, column: int) -> str:
        return self._original.get((row, column), "")

    def changed(self) -> bool:
        return any(self.text_at(row, column) != text for (row, column), text in self._original.items()
                   if self.editable(row, column))

    def editable(self, row: int, column: int) -> bool:
        return bool(self.item(row, column).flags() & Qt.ItemFlag.ItemIsEditable)

    def mark(self, bad: set[tuple[int, int]]) -> None:
        for row in range(self.rowCount()):
            for column in range(self.columnCount()):
                if self.editable(row, column):
                    self.item(row, column).setBackground(QBrush(BAD_CELL) if (row, column) in bad else QBrush())

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.state() != QAbstractItemView.State.EditingState:
            for item in self.selectedItems():
                if item.flags() & Qt.ItemFlag.ItemIsEditable:
                    item.setText("")
            return
        super().keyPressEvent(event)

    def closeEditor(self, editor, hint) -> None:
        if hint == QAbstractItemDelegate.EndEditHint.SubmitModelCache:  # Enter: on to the next cell, as Tab does
            hint = QAbstractItemDelegate.EndEditHint.EditNextItem
        super().closeEditor(editor, hint)


class ToneGrid(Grid):
    """One ear's thresholds: a row for each curve and its masking under it, a column for each frequency."""

    def __init__(self, section: str, ear: str, kinds: tuple[str, ...], frequencies: tuple[int, ...],
                 with_average: bool) -> None:
        super().__init__(2 * len(kinds), len(frequencies) + (1 if with_average else 0))
        self.section, self.ear, self.kinds, self.frequencies = section, ear, kinds, frequencies
        self.with_average = with_average
        self.setHorizontalHeaderLabels([frequency_label(frequency) for frequency in frequencies]
                                       + (["6분법"] if with_average else []))
        labels = []
        for kind in kinds:
            labels += [KIND_NAMES[kind], "  차폐"]
        self.setVerticalHeaderLabels(labels)
        for row in range(1, self.rowCount(), 2):
            self.verticalHeaderItem(row).setForeground(QBrush(SECONDARY))
        if with_average:
            column = len(frequencies)
            self.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            for row in range(self.rowCount()):
                item = self.item(row, column)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setBackground(QBrush(AVERAGE_CELL))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemChanged.connect(self._item_changed)

    def row_of(self, kind: str, masking: bool = False) -> int:
        return 2 * self.kinds.index(kind) + (1 if masking else 0)

    def column_of(self, frequency: int) -> int:
        return self.frequencies.index(frequency)

    def fill(self, cells: dict) -> None:
        self.blockSignals(True)
        for kind in self.kinds:
            for frequency in self.frequencies:
                threshold, masking = edits.tone_texts(cells.get((self.section, self.ear, kind, frequency)))
                self.put(self.row_of(kind), self.column_of(frequency), threshold)
                self.put(self.row_of(kind, True), self.column_of(frequency), masking)
        self.blockSignals(False)
        self.keep_original()
        for kind in self.kinds:
            self._show_average(kind)

    def collect(self) -> tuple[dict, list[Problem]]:
        """The cells the operator changed (cell -> point, None when emptied) and what is wrong with any."""
        changes: dict = {}
        problems: list[Problem] = []
        bad: set[tuple[int, int]] = set()
        for kind in self.kinds:
            top, under = self.row_of(kind), self.row_of(kind, True)
            for frequency in self.frequencies:
                column = self.column_of(frequency)
                threshold, masking = self.text_at(top, column), self.text_at(under, column)
                if (threshold, masking) == (self.original(top, column), self.original(under, column)):
                    continue
                try:
                    changes[(self.section, self.ear, kind, frequency)] = edits.tone_point(frequency, threshold, masking)
                except edits.CellError as error:
                    row = under if error.field == "masking" else top
                    bad.add((row, column))
                    where = f"{EAR_NAMES[self.ear]} {KIND_NAMES[kind]} {frequency_label(frequency)}"
                    problems.append(Problem(f"{where}: {error}", self, row, column))
        self.mark(bad)
        return changes, problems

    def average_text(self, kind: str) -> str:
        return self.item(self.row_of(kind), len(self.frequencies)).text() if self.with_average else ""

    def toggle_no_response(self, items=None) -> None:
        for item in items if items is not None else self.selectedItems():
            if item.row() % 2 == 0 and item.flags() & Qt.ItemFlag.ItemIsEditable:
                item.setText(edits.toggled_no_response(item.text()))

    def _menu(self, position) -> None:
        menu = QMenu(self)
        silent = menu.addAction("무반응 (↓) 표시 · 풀기")
        clear = menu.addAction("지우기")
        chosen = menu.exec(self.viewport().mapToGlobal(position))
        if chosen is silent:
            self.toggle_no_response()
        elif chosen is clear:
            for item in self.selectedItems():
                if item.flags() & Qt.ItemFlag.ItemIsEditable:
                    item.setText("")

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() >= len(self.frequencies):
            return
        threshold = item.row() % 2 == 0
        try:  # what was typed, written the way the sheet prints it: 90n -> 90↓, m -> ?
            if threshold:
                read = edits.parse_threshold(item.text())
                tidy = "" if read is None else edits.format_threshold(*read)
            else:
                tidy = edits.format_masking(*edits.parse_masking(item.text()))
        except edits.CellError:
            tidy = item.text()
        if tidy != item.text():
            self.blockSignals(True)
            item.setText(tidy)
            self.blockSignals(False)
        if threshold:
            self._show_average(self.kinds[item.row() // 2])

    def _show_average(self, kind: str) -> None:
        """The row's 6분법 average as the sheet will print it, from what can be read of it now."""
        if not self.with_average or kind not in AVERAGED:
            return
        points = []
        for frequency in self.frequencies:
            try:
                point = edits.tone_point(frequency, self.text_at(self.row_of(kind), self.column_of(frequency)), "")
            except edits.CellError:
                continue
            if point is not None:
                points.append(point)
        value = average(ToneCurve("", self.ear, kind, "", "", False, tuple(points))) if points else None
        self.blockSignals(True)
        self.item(self.row_of(kind), len(self.frequencies)).setText(value.text() if value is not None else "")
        self.blockSignals(False)


class SpeechGrid(Grid):
    """One ear's points of one speech curve, in the order measured."""

    def __init__(self, ear: str, kind: str) -> None:
        super().__init__(SPEECH_POINTS, len(edits.SPEECH_FIELDS))
        self.ear, self.kind = ear, kind
        self.setHorizontalHeaderLabels(list(edits.SPEECH_HEADINGS))
        self.setVerticalHeaderLabels([str(number) for number in range(1, SPEECH_POINTS + 1)])
        self.srt_edit = QLineEdit()
        self.srt_edit.setMaximumWidth(70)
        self.srt_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._srt = ""

    def fill(self, cells: dict) -> None:
        for number in range(1, SPEECH_POINTS + 1):
            for column, text in enumerate(edits.speech_texts(cells.get((SPEECH, self.ear, self.kind, number)))):
                self.put(number - 1, column, text)
        level = cells.get((SRT, self.ear, self.kind, 0))
        self._srt = "" if level is None else str(level)
        self.srt_edit.setText(self._srt)
        self.keep_original()

    def changed(self) -> bool:
        return super().changed() or self.srt_edit.text().strip() != self._srt

    @property
    def has_values(self) -> bool:
        return bool(self._srt) or any(self._original.values())

    def collect(self) -> tuple[dict, list[Problem]]:
        changes: dict = {}
        problems: list[Problem] = []
        bad: set[tuple[int, int]] = set()
        where = f"어음 {EAR_NAMES[self.ear]} {KIND_NAMES[self.kind]}"
        for number in range(1, SPEECH_POINTS + 1):
            row = number - 1
            texts = [self.text_at(row, column) for column in range(self.columnCount())]
            if texts == [self.original(row, column) for column in range(self.columnCount())]:
                continue
            try:
                changes[(SPEECH, self.ear, self.kind, number)] = edits.speech_point(*texts)
            except edits.CellError as error:
                column = edits.SPEECH_FIELDS.index(error.field) if error.field in edits.SPEECH_FIELDS else 0
                bad.add((row, column))
                problems.append(Problem(f"{where} {number}번째 점: {error}", self, row, column))
        self.mark(bad)
        text = self.srt_edit.text().strip()
        self.srt_edit.setStyleSheet("")
        if text != self._srt:
            try:
                changes[(SRT, self.ear, self.kind, 0)] = edits.srt(text)
            except edits.CellError as error:
                self.srt_edit.setStyleSheet(f"background: {BAD_CELL.name()};")
                problems.append(Problem(f"{where} SRT: {error}", self.srt_edit))
        return changes, problems


class HearingEditor(QDialog):
    """Corrects the values of a hearing result (``result``), or types one in by hand (``manual``).

    ``save`` is given what the operator saved and answers "" when it is saved, or what went
    wrong; the window then stays open, with everything still in it. ``confirm`` asks before
    what was typed is thrown away.
    """

    def __init__(self, parent: QWidget | None, result: HearingResult | None, *, patient: Patient,
                 tested_at: datetime, note: str = "", remarks: str = "", patient_is_new: bool = False,
                 save: Callable[[Edited], str], confirm: Callable[[str], bool], now: Callable[[], datetime] = datetime.now,
                 latest: datetime | None = None) -> None:
        super().__init__(parent)
        self.manual = result is None or result.manual
        self.result = result if result is not None else HearingResult(manual=True)
        self._new = result is None
        self._save, self._confirm, self._now = save, confirm, now
        self._patient = patient
        self._note, self._remarks, self._tested_at = note, remarks, tested_at
        self._no_wheel = _NoWheel(self)
        self.outcome: Edited | None = None
        self.setWindowTitle("청력검사 직접 입력" if self._new else "검사 값 고치기")
        self.resize(1040, 860)
        layout = QVBoxLayout(self)

        self.patient_check = QCheckBox()
        self.patient_note = QLabel()
        self.patient_note.setWordWrap(True)
        self._patient_is_new = patient_is_new
        if not self._new:
            self.patient_check.setVisible(False)
            self.patient_note.setText(f"{who(patient)} 환자 결과지의 검사 값을 고칩니다." if not patient.is_empty
                                      else "환자 정보 없는 결과지의 검사 값을 고칩니다.")
            self.patient_note.setStyleSheet("font-size: 15px; font-weight: 700;")
        elif patient.is_empty:
            self.patient_check.setVisible(False)
            self.patient_note.setText("환자를 고르지 않았습니다. 환자 정보 없이 결과지를 만듭니다. "
                                      "만든 뒤 환자 정보를 넣고 [이 환자 정보로 저장]을 누르세요.")
            self.patient_note.setStyleSheet("color: #b3261e;")
        else:
            self.patient_check.setText(f"{who(patient)} 환자의 검사입니다")
            self.patient_check.setStyleSheet("font-size: 15px; font-weight: 700;")
            self.patient_check.setChecked(patient_is_new)
            self.patient_check.toggled.connect(self._show_patient_note)
        layout.addWidget(self.patient_check)
        layout.addWidget(self.patient_note)
        self._show_patient_note()

        cells = self.result.cells()
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=1)
        extra = tuple(frequency for frequency in EXTRA_FREQUENCIES
                      if any(cell[0] == TONE and cell[3] == frequency for cell in cells))
        self.tone_grids = {ear: ToneGrid(TONE, ear, TONE_ROWS, USUAL_FREQUENCIES + extra, True) for ear, _, _ in EARS}
        self.tabs.addTab(self._ear_page(self.tone_grids, HOW_TO), "순음 · 음장")
        self.speech_grids = {(ear, kind): SpeechGrid(ear, kind) for kind in SPEECH_KINDS for ear, _, _ in EARS}
        self.tabs.addTab(self._speech_page(), "어음")
        self.high_frequency_grids: dict[str, ToneGrid] = {}
        if self.result.high_frequency or any(part.type == HIGH_FREQUENCY for part in self.result.parts):
            self.high_frequency_grids = {ear: ToneGrid(HIGH_FREQUENCY, ear, HIGH_FREQUENCY_ROWS, HF_FREQUENCIES, False)
                                         for ear, _, _ in EARS}
            self.tabs.addTab(self._ear_page(self.high_frequency_grids, HOW_TO.split("\n")[0]), "고주파")
        self.tabs.addTab(self._tinnitus_page(), "이명 •" if self.result.tinnitus else "이명")
        self.tabs.addTab(self._details_page(latest or now()), "검사일시 · 메모")
        for grid in self._grids():
            grid.fill(cells)
        for index, kind in enumerate(SPEECH_KINDS):
            if any(self.speech_grids[(ear, kind)].has_values for ear, _, _ in EARS):
                self.speech_tabs.setTabText(index, f"{KIND_NAMES[kind]} •")

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #8c1d18; background: #fdecea; padding: 6px 10px; border-radius: 6px;")
        self.problem_label.setVisible(False)
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("저장", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ----- pages --------------------------------------------------------------------------

    def _ear_page(self, grids: dict, how_to: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_note(how_to))
        for ear, name, color in EARS:
            heading = QLabel(name)
            heading.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {color};")
            layout.addWidget(heading)
            layout.addWidget(grids[ear])
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _speech_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_note(SPEECH_HOW_TO))
        self.speech_tabs = QTabWidget()
        for kind in SPEECH_KINDS:
            inner = QWidget()
            sides = QHBoxLayout(inner)
            for ear, name, color in EARS:
                grid = self.speech_grids[(ear, kind)]
                side = QGridLayout()
                heading = QLabel(name)
                heading.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {color};")
                side.addWidget(heading, 0, 0)
                srt_row = QHBoxLayout()
                srt_row.addWidget(QLabel("SRT"))
                srt_row.addWidget(grid.srt_edit)
                srt_row.addWidget(QLabel("dB HL"))
                srt_row.addStretch(1)
                side.addLayout(srt_row, 1, 0)
                side.addWidget(grid, 2, 0)
                sides.addLayout(side)
            self.speech_tabs.addTab(inner, KIND_NAMES[kind])
        layout.addWidget(self.speech_tabs)
        layout.addStretch(1)
        return page

    def _tinnitus_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_note(TINNITUS_HOW_TO))
        grid = QGridLayout()
        for column, (_, name, color) in enumerate(EARS, start=1):
            heading = QLabel(name)
            heading.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {color};")
            grid.addWidget(heading, 0, column)
        self.tinnitus_fields: dict[tuple[str, str], QWidget] = {}
        for row, item in enumerate(TINNITUS_ITEMS, start=1):
            grid.addWidget(QLabel(TINNITUS_NAMES[item]), row, 0)
            for column, (ear, _, _) in enumerate(EARS, start=1):
                if item == "inhibition":
                    field = QComboBox()
                    field.setEditable(True)
                    field.addItems(list(INHIBITION_CHOICES))
                    field.lineEdit().setMaxLength(TINNITUS_TEXT)
                    field.installEventFilter(self._no_wheel)
                else:
                    field = QLineEdit()
                    field.setMaxLength(TINNITUS_TEXT)
                field.setMinimumWidth(220)
                self.tinnitus_fields[(ear, item)] = field
                self.set_tinnitus(ear, item, self.result.tinnitus.text(ear, item))
                grid.addWidget(field, row, column)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)
        layout.addStretch(1)
        self._tinnitus = self.tinnitus()
        return page

    def set_tinnitus(self, ear: str, item: str, text: str) -> None:
        """Puts a tinnitus value in its box, as the operator would type it."""
        field = self.tinnitus_fields[(ear, item)]
        if isinstance(field, QComboBox):
            field.setEditText(text)
        else:
            field.setText(text)

    def tinnitus(self) -> Tinnitus:
        """The tinnitus values in their boxes."""
        return Tinnitus.of({key: field.currentText() if isinstance(field, QComboBox) else field.text()
                            for key, field in self.tinnitus_fields.items()})

    def _details_page(self, latest: datetime) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.tested_edit = QDateTimeEdit()
        self.tested_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.tested_edit.setCalendarPopup(True)
        self.tested_edit.setDateTimeRange(QDateTime(min(self._tested_at, EARLIEST_TEST)),
                                          QDateTime(max(latest, self._tested_at).replace(hour=23, minute=59, second=0)))
        self.tested_edit.setDateTime(QDateTime(self._tested_at))
        self.tested_edit.installEventFilter(self._no_wheel)
        form.addRow("검사일시", self.tested_edit)
        self.note_edit = QPlainTextEdit(self._note)
        self.note_edit.setFixedHeight(96)
        self.note_edit.textChanged.connect(self._show_note_hint)
        form.addRow("메모", self.note_edit)
        self.note_hint = QLabel()
        self.note_hint.setStyleSheet("color: #b3261e;")
        form.addRow("", self.note_hint)
        self.remarks_edit = QPlainTextEdit(self._remarks)
        self.remarks_edit.setFixedHeight(96)
        self.remarks_edit.setPlaceholderText("결과지에 인쇄하지 않습니다. 직원끼리 알아 둘 것을 적습니다.")
        form.addRow("특이사항\n(결과지에 안 나옴)", self.remarks_edit)
        form.addRow("", _note("메모는 결과지 맨 아래 메모 칸에 인쇄됩니다. 특이사항은 저장만 하고 인쇄하지 않습니다."))
        self._show_note_hint()
        return page

    def _grids(self) -> list[Grid]:
        return list(self.tone_grids.values()) + list(self.speech_grids.values()) + list(self.high_frequency_grids.values())

    # ----- what was typed -----------------------------------------------------------------

    def tone_grid(self, ear: str) -> ToneGrid:
        return self.tone_grids[ear]

    def speech_grid(self, ear: str, kind: str) -> SpeechGrid:
        return self.speech_grids[(ear, kind)]

    def changed(self) -> bool:
        return (any(grid.changed() for grid in self._grids()) or self._note_text() != self._note
                or self.tinnitus() != self._tinnitus
                or self.remarks_edit.toPlainText().strip() != self._remarks or self._tested() != self._tested_at)

    def collect(self) -> tuple[dict, list[Problem]]:
        changes: dict = {}
        problems: list[Problem] = []
        for grid in self._grids():
            more, wrong = grid.collect()
            changes.update(more)
            problems += wrong
        return changes, problems

    def accept(self) -> None:
        changes, problems = self.collect()
        if problems:
            self._show_problems(problems)
            return
        result = self.result.with_cells(changes, self._now()).with_tinnitus(self.tinnitus())
        if result.is_empty:
            self.problem_label.setText("결과지에 넣을 값이 없습니다. 값을 하나 이상 넣으세요.")
            self.problem_label.setVisible(True)
            self.tabs.setCurrentIndex(0)
            return
        outcome = Edited(result, self._tested(), self._note_text(), self.remarks_edit.toPlainText().strip(),
                         self._new and not self._patient.is_empty and self.patient_check.isChecked())
        failed = self._save(outcome)
        if failed:
            self.problem_label.setText(f"⚠ {failed}\n결과지를 다른 프로그램에서 열어 두었다면 닫고 다시 [저장]을 누르세요.")
            self.problem_label.setVisible(True)
            return
        self.outcome = outcome
        super().accept()

    def reject(self) -> None:
        if self.changed() and not self._confirm(NOT_SAVED):
            return
        super().reject()

    def _show_problems(self, problems: list[Problem]) -> None:
        shown = [problem.text for problem in problems[:6]]
        if len(problems) > 6:
            shown.append(f"… 그 밖에 {len(problems) - 6}칸")
        self.problem_label.setText("저장하지 않았습니다. 빨간 칸을 고쳐 주세요.\n" + "\n".join(shown))
        self.problem_label.setVisible(True)
        first = problems[0]
        widget = first.widget
        if isinstance(widget, SpeechGrid) or widget in [grid.srt_edit for grid in self.speech_grids.values()]:
            self.tabs.setCurrentIndex(1)
            grid = next(grid for grid in self.speech_grids.values() if widget in (grid, grid.srt_edit))
            self.speech_tabs.setCurrentIndex(SPEECH_KINDS.index(grid.kind))
        elif widget in self.high_frequency_grids.values():
            self.tabs.setCurrentIndex(2)
        else:
            self.tabs.setCurrentIndex(0)
        if isinstance(widget, QTableWidget) and first.row >= 0:
            widget.setCurrentCell(first.row, first.column)
        widget.setFocus()

    def _tested(self) -> datetime:
        return self.tested_edit.dateTime().toPython().replace(second=0, microsecond=0)

    def _note_text(self) -> str:
        return self.note_edit.toPlainText().strip()

    def _show_note_hint(self) -> None:
        self.note_hint.setText("메모가 길어 결과지에는 앞부분만 인쇄됩니다." if wrap_note(self._note_text())[1] else "")

    def _show_patient_note(self) -> None:
        if not self._new or self._patient.is_empty:
            return
        if self.patient_check.isChecked():
            text, color = "이 환자의 결과로 저장합니다.", "#666666"
        elif self._patient_is_new:
            text, color = "체크하지 않으면 환자 정보 없이 저장합니다.", "#b3261e"
        else:
            text, color = ("화면의 환자 정보는 앞 검사에 쓴 것입니다. 같은 환자의 검사면 위에 체크하세요. "
                           "체크하지 않으면 환자 정보 없이 저장합니다."), "#b3261e"
        self.patient_note.setText(text)
        self.patient_note.setStyleSheet(f"color: {color};")


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #666666;")
    return label
