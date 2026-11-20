from __future__ import annotations
import re
from html import escape
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QMimeData, Signal, QEvent, QTimer
from PySide6.QtGui import QColor, QKeySequence, QFont
from PySide6.QtWidgets import QTableView, QHeaderView, QAbstractItemView, QApplication, QMenu, QStyledItemDelegate, QLineEdit
from ..domain.types import MEASUREMENT_NAMES, TRUEVOLT_MODELS
from ..domain.document import Row
from ..domain.units import SIUnits
from ..domain.planning import compile_point, includes

FIELDS = ('dut_channel', 'func_type', 'vi_cond_raw', 'freq_cond_raw', 'pf_cond_raw', 'nominal_raw', 'measured_raw', 'actual_out_desc', 'status')
HEADERS = ('채널', '항목', '전력 조건', '주파수 / 보상', '역률 조건', '명목값', '측정값 · 복사', '출력 / 안내', '진행 상태')
MEASURED = 6

class MeasurementModel(QAbstractTableModel):
    changed = Signal()
    issue = Signal(str)
    def __init__(self, document):
        super().__init__()
        self.document = document
        self.locked = False
        self.active_uid = ''
        self.dut_model = ''
        self.entry = None
        self.entry_draft = ''
        self.timed_entry = False
        self.simulated_entry = False
        self._previews = {}

    def preview(self, row):
        if row.uid not in self._previews:
            self._previews[row.uid] = compile_point(row, self.document.options, self.document.model, self.dut_model)
        return self._previews[row.uid]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.document.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(FIELDS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.document.rows):
            return None
        row = self.document.rows[index.row()]
        step, col = row.step, index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == 0:
                return f'{step.dut_channel}{step.dut_channel_suffix}' if step.dut_channel else '—'
            if col == 1:
                return MEASUREMENT_NAMES.get(step.func_type, '입력 확인')
            if col == MEASURED:
                return row.result() if role == Qt.ItemDataRole.DisplayRole else step.measured_raw
            if col == 7:
                point = self.preview(row)
                if point.error:
                    return point.error
                details = [step.actual_out_desc]
                if point.terminal:
                    details.append(f'DUT {point.terminal} A 단자')
                if point.dual:
                    details.append('전류 듀얼 출력')
                if (step.func_type == 'OHM' and self.dut_model in TRUEVOLT_MODELS
                        and self.document.options.capture and self.document.options.zero_resistance):
                    details.append('2선식 · 0 Ω 널')
                return ' · '.join(details)
            if col == 8:
                return ('● ' if step.breakpoint else '') + step.status
            return getattr(step, FIELDS[col])
        if role == Qt.ItemDataRole.ToolTipRole:
            point = self.preview(row)
            return f'{step.actual_out_desc}\n{point.command}\n{point.error or step.status}'
        if role == Qt.ItemDataRole.ForegroundRole:
            if not includes(row, self.document.options, self.document.model, self.dut_model):
                return QColor('#adb8b1')
            if step.func_type in ('ERROR', 'OUT_OF_RANGE') or step.status.startswith('중단'):
                return QColor('#b34343')
            if col == MEASURED:
                return QColor('#176a50')
            if col in (0, 3, 4, 7, 8):
                return QColor('#6e8177')
        if role == Qt.ItemDataRole.BackgroundRole:
            if row.uid == self.active_uid:
                return QColor('#eaf4ee')
            if col == MEASURED:
                return QColor('#f4f9f6')
        if role == Qt.ItemDataRole.FontRole and col in (5, MEASURED):
            font = QFont('Malgun Gothic', 10)
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if col in (5, MEASURED) else Qt.AlignmentFlag.AlignLeft))
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section] if orientation == Qt.Orientation.Horizontal else str(section + 1)
        if role == Qt.ItemDataRole.ToolTipRole and orientation == Qt.Orientation.Horizontal and section == MEASURED:
            return '클릭하면 측정값 전체를 복사합니다. 빈 행도 유지합니다.'

    def awaiting_entry(self, index):
        return (self.entry is not None and index.isValid() and index.column() == MEASURED
                and self.document.rows[index.row()].uid == self.entry[1])

    def flags(self, index):
        flags = super().flags(index)
        if (not self.locked and index.column() in (0, 2, 3, 4, 5, MEASURED)
                or index.column() == MEASURED and (self.timed_entry or self.awaiting_entry(index))):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        if self.locked and not (self.timed_entry and index.column() == MEASURED):
            return False
        row = self.document.rows[index.row()]
        value = str(value).strip()
        if index.column() == 0:
            if value in ('', '—', '-'):
                row.step.dut_channel, row.step.dut_channel_suffix = None, ''
            else:
                match = re.fullmatch(r'(?:CH)?\s*([1-9]\d?)(@\d*)?', value, re.I)
                if not match:
                    self.issue.emit('채널은 1, 2, 3@처럼 입력하세요.')
                    return False
                row.step.dut_channel, row.step.dut_channel_suffix = int(match[1]), match[2] or ''
        elif index.column() in (2, 3, 4, 5):
            setattr(row.step, FIELDS[index.column()], value)
            self.document.rebuild(row)
        elif index.column() == MEASURED:
            row.step.measured_raw = SIUnits.with_nominal_unit(value, row.step.nominal_raw)
            row.simulated = self.simulated_entry
        else:
            return False
        self.document.dirty = True
        self._previews.pop(row.uid, None)
        self.row_changed(index.row())
        self.changed.emit()
        return True

    def refresh(self, document=None):
        self.beginResetModel()
        self._previews.clear()
        if document is not None:
            self.document = document
        self.endResetModel()
        self.changed.emit()

    def clear_rows(self):
        self.beginResetModel()
        self.document.rows.clear()
        self.document.dirty = True
        self.active_uid = ''
        self._previews.clear()
        self.endResetModel()
        self.changed.emit()

    def row_changed(self, row):
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(FIELDS) - 1))

class EntryDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        model = self.view.model()
        if model.awaiting_entry(index):
            editor.setProperty('entry_run', model.entry[0])
            editor.setProperty('entry_uid', model.entry[1])
            editor.setPlaceholderText('측정값 입력 후 Enter')
        return editor

    def setEditorData(self, editor, index):
        # Status updates can repaint the edited row even after the last output.
        if index.column() == MEASURED and editor.property('entry_initialized'):
            return
        if editor.property('entry_run'):
            editor.setText(self.view.model().entry_draft)
        else:
            super().setEditorData(editor, index)
        if index.column() == MEASURED:
            editor.setProperty('entry_initialized', True)

    def setModelData(self, editor, model, index):
        if editor.property('entry_run'):
            # Moving focus keeps a draft. Only Enter and a confirmed output can record it.
            if model.entry == (editor.property('entry_run'), editor.property('entry_uid')):
                model.entry_draft = editor.text()
            return
        super().setModelData(editor, model, index)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if editor.property('entry_run'):
                if editor.text().strip():
                    self.view.entry_submitted.emit(editor.property('entry_run'), editor.property('entry_uid'), editor.text())
                else:
                    self.view.notice.emit('측정값을 입력하고 Enter를 누르세요. 출력 유지 중입니다.')
                return True
            current = self.view.currentIndex()
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
            next_index = self.view.model().index(current.row() + 1, current.column())
            if next_index.flags() & Qt.ItemFlag.ItemIsEditable:
                self.view.setCurrentIndex(next_index)
                QTimer.singleShot(0, lambda: self.view.edit(next_index))
            return True
        return super().eventFilter(editor, event)

class MeasurementTable(QTableView):
    paste_requested = Signal(str, int, int)
    entry_submitted = Signal(str, str, str)
    notice = Signal(str)
    delete_requested = Signal()
    def __init__(self, model):
        super().__init__()
        self.setModel(model)
        self.setItemDelegate(EntryDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.setShowGrid(False)
        self.verticalHeader().setDefaultSectionSize(38)
        self.verticalHeader().setMinimumWidth(38)
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().sectionClicked.connect(lambda c: self.copy_results(all_rows=True) if c == MEASURED else None)
        for column, width in enumerate((62, 90, 145, 115, 90, 125, 155, 220, 135)):
            self.setColumnWidth(column, width)
        self.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context_menu)

    def begin_entry(self, run_id, row):
        model = self.model()
        model.entry = (run_id, model.document.rows[row].uid)
        model.entry_draft = ''
        index = model.index(row, MEASURED)
        self.setCurrentIndex(index)
        self.scrollTo(index)
        self.setFocus()
        self.edit(index)

    def end_entry(self):
        model = self.model()
        if model.entry:
            run_id, uid = model.entry
            model.entry = None
            model.entry_draft = ''
            for editor in self.findChildren(QLineEdit):
                if editor.property('entry_run') == run_id and editor.property('entry_uid') == uid and editor.isVisible():
                    self.itemDelegate().closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.RevertModelCache)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.model().awaiting_entry(self.currentIndex()):
            self.edit(self.currentIndex())
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            index = self.currentIndex()
            self.paste_requested.emit(QApplication.clipboard().text(), index.row(), index.column())
            return
        if event.key() == Qt.Key.Key_Delete and (not self.model().locked or self.model().timed_entry):
            for index in self.selectedIndexes():
                if index.column() == MEASURED:
                    self.model().setData(index, '')
            return
        super().keyPressEvent(event)

    def copy_selection(self):
        self.commit_measurement_edit()
        selected = self.selectedIndexes()
        if not selected:
            return
        rows, cols = sorted({i.row() for i in selected}), sorted({i.column() for i in selected})
        cells = {(i.row(), i.column()): str(i.data() or '') for i in selected}
        self.copy_cells([[cells.get((r, c), '') for c in cols] for r in rows])
        self.notice.emit(f'{len(rows)}행 복사 완료')

    def commit_measurement_edit(self):
        index = self.currentIndex()
        if index.column() == MEASURED:
            editor = self.indexWidget(index)
            if isinstance(editor, QLineEdit) and not editor.property('entry_run'):
                self.commitData(editor)

    @staticmethod
    def copy_cells(grid):
        # Excel uses table dimensions to paste the last cell even when it is empty.
        mime = QMimeData()
        mime.setText('\n'.join('\t'.join(row) for row in grid))
        rows = ['<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in row) + '</tr>' for row in grid]
        mime.setHtml('<table>' + ''.join(rows) + '</table>')
        QApplication.clipboard().setMimeData(mime)

    def copy_results(self, all_rows=False, to_end=False):
        self.commit_measurement_edit()
        rows = self.model().document.rows
        indices = list(range(len(rows))) if all_rows else sorted({i.row() for i in self.selectedIndexes()})
        if to_end:
            indices = list(range(max(0, self.currentIndex().row()), len(rows)))
        if not indices:
            return
        self.copy_cells([[rows[i].result()] for i in indices])
        self.notice.emit(f'측정값 {len(indices)}행 복사 완료 · 명목값 단위 적용')

    def context_menu(self, position):
        menu = QMenu(self)
        menu.addAction('선택한 셀 복사', self.copy_selection)
        menu.addAction('선택한 행의 측정값 복사', self.copy_results)
        menu.addAction('이 행부터 마지막 측정값까지 복사', lambda: self.copy_results(to_end=True))
        menu.addAction('측정값 전체 복사', lambda: self.copy_results(all_rows=True))
        menu.addSeparator()
        action = menu.addAction('선택 행 삭제', self.delete_requested.emit)
        action.setEnabled(not self.model().locked)
        menu.exec(self.viewport().mapToGlobal(position))
