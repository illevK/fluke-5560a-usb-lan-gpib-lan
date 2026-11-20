from __future__ import annotations
from dataclasses import asdict, replace
from pathlib import Path
import csv
import os
import queue

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QAction, QKeySequence, QCursor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QStackedWidget, QProgressBar, QPlainTextEdit, QFileDialog,
    QMessageBox, QMenu, QInputDialog, QCheckBox, QSpinBox, QSizePolicy)

from .table import MeasurementModel, MeasurementTable, MEASURED
from .dialogs import ConnectionDialog, OptionsDialog, ChannelDialog
from .worker import Job
from .theme import app_icon
from .. import __version__
from ..domain.document import Document, Row, RunOptions
from ..domain.planning import make_plan, includes, compile_point
from ..domain.types import SequenceStep
from ..domain.units import SIUnits
from ..services.connections import DeviceHub, CandidateChoice
from ..services.engine import MeasurementEngine, EngineEvent, State
from ..services.storage import data_dir, write_json

def label(text, name=''):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget

def button(text, callback, name=''):
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(lambda checked=False: callback())
    return widget

def card():
    frame = QFrame()
    frame.setObjectName('Card')
    return frame

class MainWindow(QMainWindow):
    engine_event = Signal(object)
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'교정자동화 · v{__version__}')
        self.setWindowIcon(app_icon())
        self.resize(1330, 870)
        self.setMinimumSize(1020, 690)
        self.document = Document()
        self.hub = DeviceHub()
        try:
            self.document.options = RunOptions(**self.hub.settings.get('run_options', {}))
            self.document.options.validate()
        except (TypeError, ValueError):
            self.document.options = RunOptions()
        self.motion = self.hub.settings.get('motion', True)
        self.job = None
        self._measurement_job = False
        self._record_mode = None
        self.engine = None
        self._pending_success = None
        self._closing = False
        self._close_allowed = False
        self._after_stop = None
        self._next_uid = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(450)
        self._save_timer.timeout.connect(self.autosave)
        self.build_ui()
        self.engine_event.connect(self.on_engine_event)
        self._logs_timer = QTimer(self)
        self._logs_timer.setInterval(150)
        self._logs_timer.timeout.connect(self.drain_logs)
        self._logs_timer.start()
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1500)
        self._live_timer.timeout.connect(self.read_dut)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(30000)
        self._health_timer.timeout.connect(self.health_check)
        self._health_timer.start()
        self.refresh_connections()

    @property
    def busy(self):
        return self.job is not None

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(38, 38))
        header.addWidget(logo)
        header.addSpacing(5)
        header.addWidget(label('교정자동화', 'Title'))
        header.addWidget(label(f'v{__version__}', 'Badge'))
        header.addSpacing(14)
        header.addWidget(label('연결부터 기록까지, 하나의 측정표', 'Subtitle'))
        header.addStretch()
        self.open_button = button('열기', self.open_document)
        header.addWidget(self.open_button)
        header.addWidget(button('저장', self.save_document))
        header.addWidget(button('설정', self.settings_menu))
        root.addLayout(header)

        devices = QHBoxLayout()
        self.device_widgets = {}
        for role, title in (('calibrator', '기준기'), ('dut', 'DUT')):
            frame = card()
            row = QHBoxLayout(frame)
            row.setContentsMargins(16, 13, 16, 13)
            titles = QVBoxLayout()
            top = QHBoxLayout()
            top.addWidget(label(title, 'Subtitle'))
            name = label('연결 대기', 'SectionTitle')
            top.addWidget(name)
            top.addStretch()
            titles.addLayout(top)
            endpoint = label('AUTO · 연결을 누르면 장비와 포트를 찾습니다.', 'Subtitle')
            endpoint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            endpoint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            titles.addWidget(endpoint)
            row.addLayout(titles, 1)
            config = button('설정', lambda role=role: self.connection_settings(role))
            connect = button('연결', lambda role=role: self.connect_device(role), 'Primary')
            row.addWidget(config)
            row.addWidget(connect)
            self.device_widgets[role] = name, endpoint, connect, config
            devices.addWidget(frame, 1)
        root.addLayout(devices)

        navigation = QHBoxLayout()
        self.nav_buttons = []
        for index, title in enumerate(('측정표', 'DUT 연결')):
            tab = button(title, lambda index=index: self.show_page(index), 'Tab')
            tab.setCheckable(True)
            tab.setChecked(index == 0)
            navigation.addWidget(tab)
            self.nav_buttons.append(tab)
        navigation.addStretch()
        self.summary = label('', 'Subtitle')
        navigation.addWidget(self.summary)
        root.addLayout(navigation)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        sheet = card()
        sheet_layout = QVBoxLayout(sheet)
        sheet_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(16, 14, 16, 10)
        self.count_label = label('작업표  ·  0행', 'SectionTitle')
        toolbar.addWidget(self.count_label)
        toolbar.addStretch()
        self.paste_button = button('붙여넣기 +', self.append_clipboard)
        self.clear_button = button('내용 지우기', self.clear_sheet)
        self.clear_button.setToolTip('현재 측정표의 모든 행과 측정값을 비웁니다.')
        self.channel_button = button('채널 배정', self.assign_channels)
        self.options_button = button('측정 설정', self.measurement_settings)
        self.more_button = button('더보기 ···', self.sheet_menu)
        for widget in (self.paste_button, self.clear_button, self.channel_button, self.options_button, self.more_button):
            toolbar.addWidget(widget)
        sheet_layout.addLayout(toolbar)
        self.model = MeasurementModel(self.document)
        self.model.changed.connect(self.document_changed)
        self.model.issue.connect(self.notice)
        self.table = MeasurementTable(self.model)
        self.table.paste_requested.connect(self.paste_text)
        self.table.entry_submitted.connect(self.submit_entry)
        self.table.notice.connect(self.notice)
        self.table.delete_requested.connect(self.delete_rows)
        self.table.clicked.connect(self.selection_changed)
        self.table.verticalHeader().sectionClicked.connect(lambda index: self.selection_changed(self.model.index(index, 0)))
        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.addStretch()
        message = label('측정할 항목을 붙여넣으세요', 'Title')
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(message)
        help_label = label('엑셀의 마지막 열에 명목값을 두고 Ctrl+V\n여러 항목을 한 번에 넣거나, 아래에 이어 붙일 수 있습니다.', 'Subtitle')
        help_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(help_label)
        paste = button('클립보드에서 붙여넣기', self.append_clipboard, 'Primary')
        paste.setMaximumWidth(220)
        empty_layout.addWidget(paste, 0, Qt.AlignmentFlag.AlignHCenter)
        self.restore_button = button('이전 작업표 복구', self.restore_autosave)
        self.restore_button.setVisible((data_dir() / 'autosave.json').exists())
        empty_layout.addWidget(self.restore_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch()
        self.sheet_stack = QStackedWidget()
        self.sheet_stack.addWidget(self.empty)
        self.sheet_stack.addWidget(self.table)
        sheet_layout.addWidget(self.sheet_stack, 1)
        self.pages.addWidget(sheet)

        dut_page = card()
        dut_layout = QVBoxLayout(dut_page)
        dut_layout.setContentsMargins(24, 22, 24, 22)
        dut_layout.addWidget(label('DUT 연결 및 현재 값', 'Title'))
        self.dut_description = label('DUT를 연결하면 모델과 적용한 기본 설정이 표시됩니다.', 'Subtitle')
        self.dut_description.setWordWrap(True)
        dut_layout.addWidget(self.dut_description)
        self.dut_identity = label('')
        self.dut_identity.setWordWrap(True)
        self.dut_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dut_layout.addWidget(self.dut_identity)
        line = QHBoxLayout()
        line.addWidget(label('조회 채널'))
        self.live_channel = QSpinBox()
        self.live_channel.setRange(1, 99)
        self.live_channel.setValue(1)
        line.addWidget(self.live_channel)
        self.read_button = button('값 읽기', self.read_dut)
        line.addWidget(self.read_button)
        self.live = QCheckBox('연속 조회')
        self.live.toggled.connect(lambda checked: self._live_timer.start() if checked else self._live_timer.stop())
        line.addWidget(self.live)
        line.addStretch()
        dut_layout.addLayout(line)
        self.metrics = {}
        tiles = QHBoxLayout()
        for key, title in (('voltage', '전압'), ('current', '전류'), ('power', '유효전력'), ('pf', '역률')):
            tile = card()
            layout = QVBoxLayout(tile)
            layout.setContentsMargins(20, 22, 20, 22)
            layout.addWidget(label(title, 'Subtitle'))
            value = label('—', 'Title')
            layout.addWidget(value)
            self.metrics[key] = value
            tiles.addWidget(tile)
        dut_layout.addLayout(tiles)
        self.sample_detail = QPlainTextEdit()
        self.sample_detail.setReadOnly(True)
        self.sample_detail.setMaximumHeight(170)
        dut_layout.addWidget(self.sample_detail)
        dut_layout.addWidget(label('측정표를 실행하면 해당 행에 맞춰 DUT를 설정하고, 안정화 후 새 값을 자동으로 기록합니다.', 'Subtitle'))
        dut_layout.addStretch()
        self.pages.addWidget(dut_page)

        status = card()
        status_layout = QVBoxLayout(status)
        status_layout.setContentsMargins(16, 12, 16, 12)
        line = QHBoxLayout()
        self.output_badge = label('출력 상태 미확인', 'Badge')
        self.status_title = label('준비', 'SectionTitle')
        self.progress_label = label('0 / 0', 'Subtitle')
        line.addWidget(self.output_badge)
        line.addSpacing(6)
        line.addWidget(self.status_title)
        line.addStretch()
        line.addWidget(self.progress_label)
        status_layout.addLayout(line)
        self.status_message = label('장비를 연결하고 측정표를 붙여넣으면 시작할 수 있습니다.', 'Subtitle')
        self.status_message.setWordWrap(True)
        self.status_message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_layout.addWidget(self.status_message)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        status_layout.addWidget(self.progress)
        self.progress_animation = QPropertyAnimation(self.progress, b'value', self)
        self.progress_animation.setDuration(160)
        self.progress_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        root.addWidget(status)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(800)
        self.log_view.setMaximumHeight(0)
        root.addWidget(self.log_view)
        self.log_animation = QPropertyAnimation(self.log_view, b'maximumHeight', self)
        self.log_animation.setDuration(170)
        self.log_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        footer = QHBoxLayout()
        self.log_button = button('통신 기록 펼치기', self.toggle_log)
        footer.addWidget(self.log_button)
        self.footer_note = label('자동 저장 · 명목값 단위로 표시', 'Subtitle')
        footer.addWidget(self.footer_note)
        footer.addStretch()
        self.step_button = button('한 행 실행  F8', lambda: self.start(single=True))
        self.hold_button = button('진행 보류 · 출력 유지', self.hold)
        self.stop_button = button('정지 · STBY', self.stop, 'Stop')
        self.start_button = button('시작', self.start, 'Primary')
        self.start_button.setMinimumWidth(126)
        for widget in (self.step_button, self.hold_button, self.stop_button, self.start_button):
            footer.addWidget(widget)
        root.addLayout(footer)
        for sequence, callback in [('Ctrl+V', self.paste_from_window), ('Ctrl+S', self.save_document),
                                    ('Ctrl+O', self.open_document), ('F8', lambda: self.start(single=True)),
                                    ('F9', self.toggle_breakpoint), ('Ctrl+Shift+C', lambda: self.table.copy_results(to_end=True))]:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(lambda checked=False, callback=callback: callback())
            self.addAction(action)
        self.table.setColumnHidden(MEASURED, not self.hub.settings.get('show_results', True))
        self.set_busy(False)

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        for i, tab in enumerate(self.nav_buttons):
            tab.setChecked(i == index)

    def notice(self, message):
        full = str(message)
        short = ' '.join(full.splitlines())
        if len(short) > 240:
            short = short[:220] + '… (통신 기록에서 전체 내용 확인)'
            if '출력 상태 미확인' in full:
                short = '출력 상태 미확인 · 본체에서 STBY를 확인하세요. ' + short
        self.status_message.setToolTip(full)
        self.status_message.setText(short)

    def selection_changed(self, index):
        if not self.busy:
            self._next_uid = None
            self.start_button.setText(f'{index.row() + 1}행부터 시작')

    def set_busy(self, busy):
        self.model.locked = busy
        self.clear_button.setEnabled(not busy and bool(self.document.rows))
        for widget in (self.open_button, self.paste_button, self.channel_button, self.options_button, self.more_button, self.step_button):
            widget.setEnabled(not busy)
        for name, endpoint, connect, config in self.device_widgets.values():
            connect.setEnabled(not busy and not self.hub.demo)
            config.setEnabled(not busy and not self.hub.demo)
        self.read_button.setEnabled(not busy and self.hub.session.connected)
        self.live.setEnabled(not busy and self.hub.session.connected)
        self.hold_button.setEnabled(busy and self._measurement_job)
        self.start_button.setEnabled(not busy or self._measurement_job and self.engine.state == State.HOLDING)
        self.stop_button.setEnabled(True)

    def run_job(self, function, on_success=lambda value: None, *, measurement=False, background_read=False, on_error=None):
        if self.busy:
            self.notice('현재 작업의 응답을 기다리고 있습니다.')
            return False
        self._live_timer.stop()
        self._measurement_job = measurement
        if background_read:
            # An idle instrument poll can run while an operator finishes a result.
            mode = self.document.options.record_mode(bool(self.hub.dut and self.hub.dut.connected))
            self.model.timed_entry = mode == 'timed'
        self.job = Job(function, self)
        self.set_busy(True)
        self.job.result.connect(on_success)
        self.job.error.connect(on_error or self.job_error)
        self.job.finished.connect(self.job_finished)
        self.job.start()
        return True

    def job_error(self, error):
        self._closing = False
        if isinstance(error, CandidateChoice):
            choices = error.candidates
            items = [f'{item.profile.model} · {item.settings.address} · {item.identity}' for item in choices]
            selected, accepted = QInputDialog.getItem(self, 'DUT 선택', '연결할 장비', items, 0, False)
            if accepted:
                candidate = choices[items.index(selected)]
                self._pending_success = lambda: self.connect_device('dut', candidate)
            return
        self.status_title.setText('확인 필요')
        self.notice(str(error))
        self.log_view.appendPlainText(str(error))
        self.refresh_connections()

    def job_finished(self):
        job, self.job = self.job, None
        self.model.timed_entry = False
        if self._measurement_job:
            self.table.end_entry()
            # Keep the result column available for finishing a free-entry worksheet.
            if self._record_mode != 'timed':
                self.table.setColumnHidden(MEASURED, not self.hub.settings.get('show_results', True))
            self._record_mode = None
        self._measurement_job = False
        job.deleteLater()
        self.set_busy(False)
        self.refresh_connections()
        if self._pending_success:
            callback, self._pending_success = self._pending_success, None
            QTimer.singleShot(0, callback)
        elif self._after_stop:
            callback, self._after_stop = self._after_stop, None
            QTimer.singleShot(0, callback)
        elif self.live.isChecked() and self.hub.session.connected and not self._closing:
            self._live_timer.start()

    def calibrator_connect_error(self, error):
        offer_setup = (not self.hub.connection_cancel.is_set() and not self._closing and not self._after_stop
                       and not self.hub.calibrator.connected and not self.hub.fluke.manager.recovery_target)
        self.job_error(error)
        if offer_setup:
            self._pending_success = lambda: self.connection_settings('calibrator', failure=str(error))

    def connection_settings(self, role, *, failure=''):
        if self.busy or self._closing:
            return
        config = self.hub.saved(role)
        if role == 'calibrator' and not self.hub.settings.get('calibrator_connection') and not config.address:
            config.mask = '255.255.0.0'
        source = role == 'calibrator'
        dialog = ConnectionDialog(config, role, self, failure=failure,
            presets=self.hub.calibrator_history.choices() if source else (),
            allow_connect=source and not self.hub.calibrator.connected and not self.hub.demo)
        self._health_timer.stop()
        self._live_timer.stop()
        try:
            if dialog.exec():
                self.hub.settings.set(role + '_connection', asdict(dialog.value()))
                self.refresh_connections()
                if dialog.connect_requested:
                    self.connect_device(role)
                else:
                    self.notice('연결 설정을 저장했습니다. 연결을 누르면 적용합니다.')
        finally:
            dialog.deleteLater()
            self._health_timer.start()
            if not self.busy and self.live.isChecked() and self.hub.session.connected:
                self._live_timer.start()

    def connect_device(self, role, candidate=None):
        if self.busy or self.hub.demo:
            return
        connected = self.hub.calibrator.connected if role == 'calibrator' else self.hub.session.connected
        if connected:
            self.notice('STBY 확인 후 연결을 해제합니다.')
            self.run_job(lambda: self.hub.disconnect(role), lambda result: self.on_disconnected())
            return
        config = self.hub.saved(role)
        self.notice(('기준기' if role == 'calibrator' else 'DUT') + '를 찾는 중입니다. 정지를 누르면 검색을 취소합니다.')
        action = lambda: self.hub.connect_calibrator(config) if role == 'calibrator' else self.hub.connect_dut(config, candidate)
        self.run_job(action, lambda result: self.connected(role),
                     on_error=self.calibrator_connect_error if role == 'calibrator' else None)

    def on_disconnected(self):
        self.show_output(False)
        self.notice('STBY 확인 후 연결을 해제했습니다.')

    def connected(self, role):
        if role == 'calibrator':
            self.document.model = self.hub.calibrator.model
            self.document.rebuild_all()
            self.model.refresh()
            self.show_output(self.hub.last_output)
        self.status_title.setText('연결 완료')
        self.notice('DUT 설정과 측정표를 확인하고 시작하세요.' if role == 'calibrator' else self.hub.defaults)
        self.refresh_connections()

    def refresh_connections(self):
        cal = self.hub.calibrator
        self.model.simulated_entry = self.hub.demo
        name, endpoint, connect, _ = self.device_widgets['calibrator']
        name.setText(('미리보기 · ' if self.hub.demo else '') + cal.model if cal.connected else '연결 대기')
        if self.hub.demo:
            endpoint.setText('가상 장비 · 실제 출력 없음')
        elif cal.connected:
            endpoint.setText(self.hub.fluke.manager.info.resource_name)
        else:
            config = self.hub.saved('calibrator')
            target = [config.model] if config.model != 'AUTO' else []
            target.append(config.mode)
            if config.address:
                target.append(config.address)
            endpoint.setText(' · '.join(target) + ' · 연결 대기')
        connect.setText('해제' if cal.connected else '연결')
        name, endpoint, connect, _ = self.device_widgets['dut']
        dut = self.hub.dut
        name.setText(('미리보기 · ' if self.hub.demo else '') + dut.model if dut and dut.connected else '연결 대기')
        settings = self.hub.session.settings
        endpoint.setText('가상 측정 · 결과에 미리보기 표시' if self.hub.demo else f'{settings.mode} · {settings.address}' if settings else 'AUTO · USB / LAN / RS232 / GPIB')
        connect.setText('해제' if self.hub.session.connected else '연결')
        self.dut_description.setText(self.hub.defaults or '연결 시 지원 모델의 빠른 갱신·RMS·전압 동기·자동 범위를 적용합니다.')
        self.dut_identity.setText(self.hub.session.identity)
        model = dut.model if dut else ''
        if model != self.model.dut_model:
            self.model.dut_model = model
            self.model._previews.clear()
            if self.model.rowCount():
                self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rowCount() - 1, 8))
        self.refresh_summary()

    def show_output(self, output):
        text, color, bg = ('OPER · 출력 중', '#a44317', '#fff0df') if output is True else ('STBY · 출력 정지', '#21634a', '#e5f1e9') if output is False else ('출력 상태 미확인', '#816338', '#f6eee0')
        if self.hub.demo:
            text = '미리보기 · ' + text
        self.output_badge.setText(text)
        self.output_badge.setStyleSheet(f'color:{color};background:{bg};border-radius:10px;padding:4px 9px;')

    def measurement_settings(self):
        if self.busy:
            return
        dialog = OptionsDialog(self.document.options, self.document.model, self)
        if dialog.exec():
            self.document.options = dialog.value()
            self.hub.settings.set('run_options', asdict(self.document.options))
            self.document.rebuild_all()
            self.model.refresh()
            self.notice('측정 설정을 적용했습니다. 기존 측정값은 유지됩니다.')

    def refresh_summary(self):
        options = self.document.options
        mode = options.record_mode(bool(self.hub.dut and self.hub.dut.connected))
        labels = {'dut': 'DUT 자동 기록', 'entry': '입력 대기 · Enter로 진행',
                  'timed': '자동 진행 · 자유 입력', 'setup': '출력 설정만'}
        self.summary.setText(f'{self.document.model}  ·  {options.dwell:g}초  ·  {labels[mode]}')
        if options.current_group != 'all':
            self.summary.setText(self.summary.text() + '  ·  ' + ('작은 전류 그룹' if options.current_group == 'low' else '큰 전류 그룹'))
        self.count_label.setText(f'작업표  ·  {len(self.document.rows):,}행')
        self.clear_button.setEnabled(not self.busy and bool(self.document.rows))
        self.sheet_stack.setCurrentIndex(1 if self.document.rows else 0)
        if self.hub.demo:
            self.footer_note.setText('미리보기 모드 · 실제 출력과 측정 없음')
        else:
            self.footer_note.setText('자동 저장 · 명목값 단위로 표시')

    def document_changed(self):
        self.refresh_summary()
        self._save_timer.start()

    def autosave(self):
        if not self.document.rows:
            return False
        path, dirty = self.document.path, self.document.dirty
        try:
            self.document.save(data_dir() / 'autosave.json')
        except OSError as error:
            self.notice(f'자동 저장 실패: {error}')
            return False
        finally:
            self.document.path, self.document.dirty = path, dirty
        self.restore_button.setVisible(True)
        return True

    def append_clipboard(self):
        self.paste_text(QApplication.clipboard().text(), -1, -1)

    def paste_from_window(self):
        focused = QApplication.focusWidget()
        if focused and focused.inherits('QLineEdit'):
            focused.paste()
            return
        index = self.table.currentIndex()
        self.paste_text(QApplication.clipboard().text(), index.row(), index.column())

    def paste_text(self, text, row=-1, column=-1):
        if not text.strip():
            return
        if self.busy and not (self.model.timed_entry and row >= 0 and column == MEASURED and '\t' not in text):
            return
        if row >= 0 and column in (2, 3, 4, 5, MEASURED) and '\t' not in text and self.document.rows:
            from ..domain.clipboard import clipboard_measurement_kind
            if column == MEASURED or not any(clipboard_measurement_kind(line) for line in text.splitlines()):
                for offset, value in enumerate(text.splitlines()):
                    target = row + offset
                    if target >= len(self.document.rows):
                        break
                    self.model.setData(self.model.index(target, column), value)
                self.notice('선택한 열에 붙여넣었습니다.')
                return
        try:
            incoming = self.document.parse(text)
            if not incoming:
                raise ValueError('측정값을 찾지 못했습니다. 마지막 열에 명목값과 단위를 넣어 주세요.')
            self.document.insert(incoming, None if row < 0 else row)
            self.model.refresh()
            index = len(self.document.rows) - len(incoming) if row < 0 else row
            self.table.setCurrentIndex(self.model.index(index, 5))
            self.table.scrollTo(self.model.index(index, 5))
            invalid = sum(r.step.func_type in ('ERROR', 'OUT_OF_RANGE') for r in incoming)
            self.notice(f'{len(incoming)}행 추가' + (f' · 입력·범위 확인 필요 {invalid}행도 보존했습니다.' if invalid else ' · 오른쪽 명목값 기준으로 인식했습니다.'))
        except Exception as error:
            self.job_error(error)

    def selected_rows(self):
        return sorted({index.row() for index in self.table.selectedIndexes()})

    def assign_channels(self):
        if not self.document.rows or self.busy:
            return
        channels = tuple(self.hub.dut.channels) if self.hub.dut else (1, 2, 3, 4)
        dialog = ChannelDialog(self.document.rows, channels, self.selected_rows(), self)
        if dialog.exec():
            for row in self.document.rows:
                if row.uid in dialog.assignments:
                    key = dialog.assignments[row.uid]
                    row.step.dut_channel, row.step.dut_channel_suffix = key or (None, '')
            self.model.refresh()
            self.notice('채널을 배정했습니다. 시작하면 선택한 행의 채널만 끝까지 실행합니다.')

    def sheet_menu(self):
        menu = QMenu(self)
        menu.addAction('클립보드 내용을 아래에 이어 붙이기', self.append_clipboard)
        menu.addAction('빈 행 추가', self.add_row)
        menu.addAction('선택 행 삭제', self.delete_rows)
        menu.addAction('중단점 켜기 / 끄기  F9', self.toggle_breakpoint)
        menu.addSeparator()
        menu.addAction('현재 전류 그룹을 위로 정렬', self.sort_group)
        menu.addAction('채널 순서로 정렬', self.sort_channels)
        menu.addAction('원래 순서로 복원', self.restore_order)
        menu.addSeparator()
        menu.addAction('측정값 전체 복사', lambda: self.table.copy_results(all_rows=True))
        menu.addAction('측정표 내보내기 (CSV)', self.export_csv)
        menu.addAction('새 측정표', self.new_document)
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def add_row(self):
        step = SequenceStep(len(self.document.rows) + 1, '-', 'DC', '-', '', 'ERROR', '', '명목값을 입력하세요.')
        self.document.insert([Row(step)])
        self.model.refresh()
        self.table.setCurrentIndex(self.model.index(len(self.document.rows) - 1, 5))
        self.table.edit(self.table.currentIndex())

    def delete_rows(self):
        if self.busy:
            return
        selected = set(self.selected_rows())
        self.document.rows = [row for index, row in enumerate(self.document.rows) if index not in selected]
        self.document.renumber()
        self.model.refresh()

    def clear_sheet(self):
        if self.busy or not self.document.rows:
            return
        self.clear_button.setFocus()  # Commit an open cell to the recovery copy.
        saved = self.autosave()
        if not saved:
            choice = QMessageBox.question(self, '내용 지우기',
                '현재 표의 복구본을 저장하지 못했습니다. 그래도 모든 행과 측정값을 지울까요?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes or self.busy:
                return
        count = self.model.rowCount()
        self.model.clear_rows()
        self.engine = None
        self._next_uid = None
        self.progress_animation.stop()
        self.progress.setValue(0)
        self.progress_label.setText('0 / 0')
        self.start_button.setText('시작')
        self.status_title.setText('측정 대기')
        self.restore_button.setVisible(saved)
        self.notice(f'측정표 {count:,}행을 비웠습니다.')

    def toggle_breakpoint(self):
        if self.busy:
            return
        for index in self.selected_rows():
            step = self.document.rows[index].step
            step.breakpoint = not step.breakpoint
            self.model.row_changed(index)
        self.document_changed()

    def sort_group(self):
        model = self.hub.dut.model if self.hub.dut else ''
        original = sorted(self.document.rows, key=lambda row: row.order)
        self.document.rows = sorted(original, key=lambda row: not includes(row, self.document.options, self.document.model, model))
        self.document.renumber()
        self.model.refresh()

    def sort_channels(self):
        self.document.rows.sort(key=lambda row: (row.step.dut_channel or 0, row.step.dut_channel_suffix, row.order))
        self.document.renumber()
        self.model.refresh()

    def restore_order(self):
        self.document.rows.sort(key=lambda row: row.order)
        self.document.renumber()
        self.model.refresh()

    def start(self, single=False):
        if self.engine and self.busy and self.engine.state == State.HOLDING:
            self.engine.resume()
            self.start_button.setEnabled(False)
            return
        if self.busy:
            return
        try:
            if not self.hub.calibrator.connected:
                raise ValueError('기준기를 먼저 연결하세요.')
            if self.document.model != self.hub.calibrator.model:
                self.document.model = self.hub.calibrator.model
                self.document.rebuild_all()
                self.model.refresh()
            self.table.setFocus()  # Commit an active editor before snapshotting the plan.
            selected = self.table.currentIndex().row()
            if self._next_uid:
                selected = next((i for i, row in enumerate(self.document.rows) if row.uid == self._next_uid), selected)
            selected = max(0, selected)
            options = replace(self.document.options)
            plan = make_plan(self.document.rows, selected, options, self.document.model,
                             self.hub.dut.model if self.hub.dut else '', single=single)
            self.engine = MeasurementEngine(self.hub.calibrator, self.hub.dut, self.engine_event.emit)
            self._record_mode = options.record_mode(bool(self.hub.dut and self.hub.dut.connected))
            if self._record_mode in ('entry', 'timed'):
                self.show_page(0)
                self.table.setColumnHidden(MEASURED, False)
                self.model.timed_entry = self._record_mode == 'timed'
                self.model.simulated_entry = self.hub.calibrator.simulated
                if self.model.timed_entry and plan:
                    first = next(i for i, row in enumerate(self.document.rows) if row.uid == plan[0].uid)
                    self.table.setCurrentIndex(self.model.index(first, MEASURED))
            self._next_uid = None
            self.progress.setValue(0)
            self.start_button.setText('실행 중')
            self.run_job(lambda: self.engine.run(plan, options), measurement=True)
        except Exception as error:
            self.job_error(error)

    def hold(self):
        if self._measurement_job and self.busy and self.engine.state != State.MANUAL:
            self.engine.request_hold()
            self.notice('현재 응답을 받은 뒤 출력을 유지하고 진행을 보류합니다.')

    def stop(self):
        if self.busy:
            if self._measurement_job:
                self.engine.request_stop()
                self.table.end_entry()
            else:
                self.hub.cancel()
                if self._after_stop is None:
                    self._after_stop = self.stop
            self.notice('현재 대기를 취소하고 STBY를 확인합니다.')
        elif self.hub.calibrator.connected or self.hub.fluke.manager.recovery_target:
            self.run_job(self.hub.calibrator.standby, lambda value: self.stopped())
        else:
            self.notice('연결된 기준기가 없습니다. 출력 상태는 본체에서 확인하세요.')

    def stopped(self):
        self.show_output(False)
        self.status_title.setText('정지')
        self.notice('STBY 확인 완료')

    def submit_entry(self, run_id, row_id, text):
        if self._measurement_job and self.engine.submit_entry(run_id, row_id, text):
            self.table.end_entry()

    def on_engine_event(self, event: EngineEvent):
        if self.engine is None or event.run_id != self.engine.run_id:
            return
        self.status_title.setText(event.state.value)
        if event.message:
            self.notice(event.message)
        self.show_output(event.output)
        self.progress_label.setText(f'{event.completed} / {event.total}')
        value = round(1000 * event.completed / max(1, event.total))
        self.progress_animation.stop()
        if self.motion:
            self.progress_animation.setStartValue(self.progress.value())
            self.progress_animation.setEndValue(value)
            self.progress_animation.start()
        else:
            self.progress.setValue(value)
        index = next((i for i, row in enumerate(self.document.rows) if row.uid == event.row_id), None)
        if self.model.entry and (event.kind in ('result', 'finished', 'emergency') or event.state != State.MANUAL):
            self.table.end_entry()
        if index is not None:
            row = self.document.rows[index]
            old_active = next((i for i, item in enumerate(self.document.rows) if item.uid == self.model.active_uid), None)
            self.model.active_uid = row.uid
            if old_active is not None and old_active != index:
                self.model.row_changed(old_active)
            if event.kind == 'result':
                row.step.status = event.message
                if event.measured:
                    row.step.measured_raw, row.simulated = event.measured, event.simulated
                self.document_changed()
            elif event.kind == 'state' and event.state != State.COMPLETE:
                row.step.status = event.state.value
                if self.model.timed_entry and event.state == State.SETTLING:
                    self.status_title.setText(f'{index + 1}행 · {row.step.nominal_raw} 출력 중')
            self.model.row_changed(index)
            if (event.kind == 'state' and event.state == State.MANUAL and self._measurement_job
                    and not self.engine.stop_requested.is_set() and not self.model.entry):
                self.table.begin_entry(event.run_id, index)
            elif (event.kind == 'state' and event.state in (State.PREPARING, State.WIRING, State.ERROR)
                    and not self.model.timed_entry):
                self.table.setCurrentIndex(self.model.index(index, 5))
                self.table.scrollTo(self.model.index(index, 5))
        if event.kind in ('state', 'log', 'result'):
            self.hub.log(f'[{event.state.value}] {event.message} {event.measured}')
        if event.kind == 'state' and self._measurement_job:
            self.hold_button.setEnabled(event.state in (State.PREPARING, State.ZEROING, State.SETTLING, State.READING))
        if event.state == State.HOLDING:
            self.start_button.setText('계속 진행')
            self.start_button.setEnabled(True)
        if event.kind == 'finished':
            self._next_uid = event.row_id if event.state != State.COMPLETE else None
            self.start_button.setText('선택 행부터 다시 시작' if event.state == State.COMPLETE else '이 행부터 재개')
            self.hold_button.setEnabled(False)
            self.autosave()

    def read_dut(self):
        if self.busy or not self.hub.session.connected:
            return
        channel = self.live_channel.value()
        self.run_job(lambda: self.hub.live_read(channel), self.show_sample, background_read=True)

    def health_check(self):
        if self.busy or self._closing or self.hub.demo:
            return
        if self.hub.calibrator.connected or self.hub.session.connected:
            self.run_job(self.hub.health_check, self.show_output, background_read=True)

    def show_sample(self, sample):
        units = {'voltage': 'V', 'current': 'A', 'power': 'W', 'pf': ''}
        for key, widget in self.metrics.items():
            value = sample.values.get(key)
            widget.setText('—' if value is None else f'{value:.8g} {units[key]}'.strip())
        self.sample_detail.setPlainText(f'CH{sample.channel} · {sample.received_at}\n{sample.note}\n\n' +
            '\n'.join(f'{key}: {raw}' for key, raw in (sample.raw_values or {}).items()))

    def drain_logs(self):
        for _ in range(80):
            try:
                message = self.hub.fluke.manager.events.get_nowait()
            except queue.Empty:
                break
            self.log_view.appendPlainText(str(message))

    def toggle_log(self):
        opened = self.log_view.maximumHeight() > 0
        target = 0 if opened else 145
        self.log_animation.stop()
        self.log_button.setText('통신 기록 펼치기' if opened else '통신 기록 접기')
        if self.motion:
            self.log_animation.setStartValue(self.log_view.maximumHeight())
            self.log_animation.setEndValue(target)
            self.log_animation.start()
        else:
            self.log_view.setMaximumHeight(target)

    def settings_menu(self):
        menu = QMenu(self)
        results = menu.addAction('측정값 입력 열 표시')
        results.setCheckable(True)
        results.setChecked(not self.table.isColumnHidden(MEASURED))
        results.setEnabled(self._record_mode not in ('entry', 'timed'))
        results.toggled.connect(self.toggle_results)
        motion = menu.addAction('화면 전환 효과')
        motion.setCheckable(True)
        motion.setChecked(self.motion)
        motion.toggled.connect(self.toggle_motion)
        menu.addAction('로그 저장 폴더 선택', self.choose_log_folder).setEnabled(not self.busy)
        menu.addAction('로그 폴더 열기', lambda: os.startfile(str(Path(self.hub.fluke.manager.log_path).parent)))
        menu.addSeparator()
        menu.addAction('같은 장비 연결 복구 · STBY', lambda: self.run_job(self.hub.recover, self.notice)).setEnabled(not self.busy)
        menu.addAction('기준기 명령 / 조회 (고급)', self.manual_command).setEnabled(not self.busy)
        demo = menu.addAction('장비 없이 미리보기')
        demo.setCheckable(True)
        demo.setChecked(self.hub.demo)
        demo.setEnabled(not self.busy)
        demo.toggled.connect(self.toggle_demo)
        menu.exec(QCursor.pos())

    def manual_command(self):
        command, accepted = QInputDialog.getText(self, '기준기 명령 / 조회', '명령을 입력하세요. 출력 설정 변경 전에는 STBY를 확인합니다.', text='*IDN?')
        if accepted and command.strip():
            def done(response):
                self.notice(response)
                self.show_output(self.hub.last_output)
            self.run_job(lambda: self.hub.manual_command(command), done)

    def toggle_results(self, show):
        self.table.setColumnHidden(MEASURED, not show)
        self.hub.settings.set('show_results', show)

    def toggle_motion(self, enabled):
        self.motion = enabled
        self.hub.settings.set('motion', enabled)

    def choose_log_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '로그를 저장할 폴더', str(Path(self.hub.fluke.manager.log_path).parent))
        if folder:
            try:
                self.hub.fluke.manager.set_log_folder(folder)
                self.notice('로그 저장 폴더: ' + folder)
            except Exception as error:
                self.job_error(error)

    def toggle_demo(self, enabled):
        try:
            if enabled:
                choices = ['기준기만 · 측정값 직접 입력', '34465A · 단자 변경 및 저항 널',
                           '34461A · 단자 변경 및 저항 널', 'PW3390 · 다채널 전압·전류·전력·역률']
                choice, accepted = QInputDialog.getItem(self, '미리보기 DUT', '흐름을 확인할 DUT 유형', choices, 0, False)
                if not accepted:
                    return
                dut_model = None if choice == choices[0] else choice.split(' · ')[0]
            else:
                dut_model = '34465A'
            self.hub.set_demo(enabled, self.document.model, dut_model)
            self.show_output(False if enabled else None)
            self.refresh_connections()
            self.set_busy(False)
            self.notice('미리보기는 실제 장비에 명령을 보내지 않습니다.' if enabled else '실제 장비 연결 모드로 돌아왔습니다.')
        except Exception as error:
            self.job_error(error)

    def new_document(self):
        if self.busy:
            return
        if self.document.rows and QMessageBox.question(self, '새 측정표', '현재 표는 자동 복구 파일에 보관됩니다. 빈 측정표를 열까요?') != QMessageBox.StandardButton.Yes:
            return
        self.autosave()
        self.document = Document()
        self.document.model = self.hub.calibrator.model
        self.model.refresh(self.document)
        self._next_uid = None

    def save_document(self):
        path = str(self.document.path or (Path.home() / 'Documents' / '측정표.calibration.json'))
        path, _ = QFileDialog.getSaveFileName(self, '측정표 저장', path, '교정자동화 측정표 (*.calibration.json)')
        if path:
            try:
                self.document.save(path)
                self.notice('측정표 저장 완료: ' + path)
            except Exception as error:
                self.job_error(error)

    def load_document(self, path):
        document = Document.load(path)
        if self.hub.calibrator.connected and not self.hub.demo:
            document.model = self.hub.calibrator.model
            document.rebuild_all()
        self.document = document
        self._next_uid = None
        self.model.refresh(document)
        self.notice('측정표를 불러왔습니다. 측정 결과와 채널 배정도 복원했습니다.')

    def open_document(self):
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, '측정표 열기', '', '교정자동화 측정표 (*.json)')
        if path:
            try:
                self.load_document(path)
            except Exception as error:
                self.job_error(error)

    def restore_autosave(self):
        try:
            self.load_document(data_dir() / 'autosave.json')
            self.document.path = None
        except Exception as error:
            self.job_error(error)

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, '측정 결과 내보내기', '측정결과.csv', 'CSV (*.csv)')
        if path:
            from .table import HEADERS
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(HEADERS)
                for row in range(self.model.rowCount()):
                    writer.writerow([self.model.index(row, col).data() for col in range(self.model.columnCount())])
            self.notice('명목값 단위로 결과를 내보냈습니다.')

    def closeEvent(self, event):
        if self._close_allowed:
            event.accept()
            return
        if self._closing:
            event.ignore()
            return
        self.autosave()
        connected = self.hub.calibrator.connected or self.hub.session.connected
        if not self.busy and not connected:
            event.accept()
            return
        event.ignore()
        self._closing = True
        self.live.setChecked(False)
        def finish():
            def done(value):
                self._close_allowed = True
                self._pending_success = self.close
            def disconnect():
                self.hub.disconnect('all')
            self.run_job(disconnect, done)
        if self.busy:
            self._after_stop = finish
            self.stop()
        else:
            finish()
