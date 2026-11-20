from __future__ import annotations
from dataclasses import dataclass, replace
from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QDialogButtonBox,
    QGroupBox, QPushButton, QPlainTextEdit, QMessageBox, QSizePolicy, QGridLayout, QToolButton)
from ..services.connections import ConnectionSettings
from ..services.drivers import REGISTRY
from ..domain.commands import MODEL_PROFILES, serial_baudrates
from ..domain.connection_guide import address_modes, connection_guide, supported_modes
from ..domain.units import SIUnits
from ..domain.planning import suggest_channels
from ..domain.clipboard import channel_name

def combo(items, value=None):
    box = QComboBox()
    for item in items:
        if isinstance(item, tuple):
            box.addItem(item[0], item[1])
        else:
            box.addItem(str(item), item)
    if value is not None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)
    return box

def spin(value, low, high, decimals=0):
    box = QDoubleSpinBox() if decimals else QSpinBox()
    if decimals:
        box.setDecimals(decimals)
    box.setRange(low, high)
    box.setValue(value)
    return box

def check(text, value):
    box = QCheckBox(text)
    box.setChecked(value)
    return box

class ConnectionDialog(QDialog):
    def __init__(self, config, role, parent=None, *, presets=(), failure='', allow_connect=False):
        super().__init__(parent)
        self.setWindowTitle(('기준기' if role == 'calibrator' else 'DUT') + ' 연결 설정')
        self.setMinimumWidth(490)
        self.config, self.role = config, role
        calibrator = role == 'calibrator'
        self.connect_requested = False
        self.presets = presets
        self.failure = failure
        layout = QVBoxLayout(self)
        if failure:
            line = QHBoxLayout()
            message = QLabel('연결 실패 · ' + str(failure).splitlines()[0][:100])
            message.setTextFormat(Qt.TextFormat.PlainText)
            message.setWordWrap(True)
            line.addWidget(message, 1)
            details = QPushButton('시도 기록')
            details.clicked.connect(self.show_failure)
            line.addWidget(details)
            layout.addLayout(line)
        if calibrator:
            self.setMinimumWidth(760)
            columns = QHBoxLayout()
            columns.setSpacing(20)
            layout.addLayout(columns)
            left = QVBoxLayout()
            columns.addLayout(left, 1)
        else:
            intro = QLabel('기본은 AUTO입니다. LAN으로 처음 연결할 때는 본체 IP를 입력하세요.')
            intro.setWordWrap(True)
            layout.addWidget(intro)
            left = layout
        form = self.form = QFormLayout()
        form.setVerticalSpacing(8 if calibrator else 12)
        left.addLayout(form)
        self.mode = combo(supported_modes(config.model) if calibrator else ['AUTO', 'LAN', 'USB', 'RS232', 'GPIB'], config.mode)
        self.model = combo(['AUTO', *MODEL_PROFILES] if role == 'calibrator' else REGISTRY.connection_choices(), config.model)
        self.address = QLineEdit(config.address)
        self.address.setPlaceholderText('LAN IP / COM11 / GPIB 주소 / USB 리소스')
        self.port = spin(config.port, 1, 65535)
        self.mask = QLineEdit(config.mask)
        self.direct = check('PC와 랜선 직결 · 유선 경로 자동 맞춤', config.direct)
        self.protocol = combo([('자동 인식', 'AUTO'), ('WT / VXI-11', 'VXI11'), ('WT5000 TCP', 'WT5000_TCP'),
            ('N4L PPA', 'PPA_LAN'), ('Hioki PW', 'HIOKI_LAN'), ('Keysight 34461A / 34465A TCP', 'DMM_TCP')], config.lan_protocol)
        if calibrator:
            form.addRow('장비 모델', self.model)
        form.addRow('연결 방식', self.mode)
        if not calibrator:
            form.addRow('장비 모델', self.model)
        form.addRow('주소', self.address)
        form.addRow('LAN 포트', self.port)
        form.addRow('서브넷 마스크', self.mask)
        form.addRow('', self.direct)
        if role == 'dut':
            form.addRow('LAN 명령 방식', self.protocol)
        serial = QGroupBox('통신 상세 설정')
        advanced = self.advanced = QFormLayout(serial)
        rates = serial_baudrates(config.model if calibrator else 'AUTO')
        self.baudrate = combo(rates, config.baudrate if config.baudrate in rates else 9600)
        self.data_bits = combo([7, 8], config.data_bits)
        self.parity = combo(['N', 'E', 'O'], config.parity)
        self.stop_bits = combo([1, 2], config.stop_bits)
        self.flow = combo(['NONE', 'XON/XOFF', 'RTS/CTS'] if calibrator else ['NONE', 'XON/XOFF', 'RTS/CTS', 'DTR/DSR'], config.flow)
        self.eol = combo(['CRLF', 'LF', 'CR'], config.eol)
        self.timeout = spin(config.timeout, .2 if calibrator else 1, 60, 1)
        self.operation_timeout = spin(config.operation_timeout, 1, 300)
        for title, widget in [('통신 속도', self.baudrate), ('데이터 비트', self.data_bits), ('패리티', self.parity),
            ('정지 비트', self.stop_bits), ('흐름 제어', self.flow), ('줄 끝 문자', self.eol),
            ('응답 제한 (초)', self.timeout), ('출력 완료 제한 (초)', self.operation_timeout)]:
            advanced.addRow(title, widget)
        left.addWidget(serial)
        if calibrator:
            left.addStretch()
            right = QVBoxLayout()
            columns.addLayout(right, 1)
            guidance = QGroupBox('연결 안내')
            guide = QVBoxLayout(guidance)
            self.panel_guide = self.guide_label(guide, '본체에서 확인')
            self.pc_guide = self.guide_label(guide, 'PC에서 설정')
            right.addWidget(guidance)
            history = QGroupBox('지난 연결 설정')
            history_layout = QVBoxLayout(history)
            self.preset_choice = QComboBox()
            self.preset_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            history_layout.addWidget(self.preset_choice)
            self.preset_detail = self.guide_label(history_layout)
            self.load_button = QPushButton('이 설정 불러오기')
            self.load_button.clicked.connect(self.load_preset)
            history_layout.addWidget(self.load_button)
            right.addWidget(history)
            right.addStretch()
            self._view = (config.model, config.mode)
            self._addresses = {}
            self.preset_choice.currentIndexChanged.connect(self.show_preset)
            self.mode.currentIndexChanged.connect(self.update_calibrator)
            self.update_calibrator()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('저장')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('취소')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        if calibrator and allow_connect:
            self.connect_button = self.buttons.addButton('다시 연결' if failure else '저장 후 연결',
                                                        QDialogButtonBox.ButtonRole.ActionRole)
            self.connect_button.setObjectName('Primary')
            if failure:
                self.connect_button.setDefault(True)
            else:
                self.buttons.button(QDialogButtonBox.StandardButton.Save).setDefault(True)
            self.connect_button.clicked.connect(self.request_connect)
        layout.addWidget(self.buttons)
        self.protocol.currentIndexChanged.connect(self.protocol_changed)
        self.model.currentIndexChanged.connect(self.model_changed)

    @staticmethod
    def guide_label(layout, title=''):
        if title:
            heading = QLabel(title)
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            layout.addWidget(heading)
        value = QLabel()
        value.setTextFormat(Qt.TextFormat.PlainText)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value.setWordWrap(True)
        value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(value)
        return value

    def show_failure(self):
        box = QDialog(self)
        box.setWindowTitle('연결 시도 기록')
        box.resize(650, 380)
        layout = QVBoxLayout(box)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(f'시도한 설정  ·  {self.config.model} / {self.config.mode}\n\n{self.failure}')
        layout.addWidget(details)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(box.reject)
        layout.addWidget(close)
        box.exec()

    def request_connect(self):
        self.connect_requested = True
        self.accept()

    def update_calibrator(self):
        model, mode = self.model.currentData(), self.mode.currentData()
        previous_model, previous_mode = self._view
        address = self.address.text().strip()
        if model != previous_model:
            previous_speed = self.baudrate.currentData()
            speeds = serial_baudrates(model)
            self.baudrate.clear()
            for speed in speeds:
                self.baudrate.addItem(str(speed), speed)
            self.baudrate.setCurrentIndex(self.baudrate.findData(previous_speed if previous_speed in speeds else 9600))
            with QSignalBlocker(self.mode):
                self.mode.clear()
                for item in supported_modes(model):
                    self.mode.addItem(item, item)
                self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
                mode = self.mode.currentData()
        profile = MODEL_PROFILES.get(model)
        uses_com = mode == 'RS232' or mode == 'USB' and bool(profile and profile.usb_virtual_com)
        serial_fields = uses_com or mode == 'USB' and model == 'AUTO'
        lan = mode in ('LAN', 'AUTO') and 'LAN' in supported_modes(model)
        if (model, mode) != self._view:
            self._addresses[self._view] = address
            options = address_modes(address, model)
            compatible = bool(options) if mode == 'AUTO' else mode in options
            if (previous_model != model and previous_model != 'AUTO') or not compatible:
                address = ''
            self.address.setText(self._addresses.get((model, mode), address))
            self._view = model, mode
        auto_inputs = ['COM 번호']
        if 'LAN' in supported_modes(model):
            auto_inputs.insert(0, 'LAN IP')
        if 'GPIB' in supported_modes(model):
            auto_inputs.append('GPIB 주소')
        if model in ('AUTO', '5560A'):
            auto_inputs.append('USB VISA 주소')
        hints = {'AUTO': ' / '.join(auto_inputs), 'LAN': '본체에 표시된 IP',
                 'GPIB': '본체 주소 0~30 또는 GPIB VISA 주소',
                 'USB': 'USB VISA 주소 (선택)' if model != 'AUTO' else 'USB VISA 주소 또는 가상 COM (선택)'}
        self.address.setPlaceholderText('COM11 (비우면 검색)' if uses_com else hints[mode])
        address_label = {'AUTO': '주소 (선택)', 'LAN': '장비 IP', 'GPIB': 'GPIB 주소', 'USB': 'USB 주소'}
        self.form.labelForField(self.address).setText('COM 포트' if uses_com else address_label[mode])
        for field in (self.port, self.mask, self.direct):
            self.form.setRowVisible(field, lan)
        for field in (self.baudrate, self.data_bits, self.parity, self.stop_bits, self.flow):
            self.advanced.setRowVisible(field, serial_fields)
        self.advanced.setRowVisible(self.eol, serial_fields or mode in ('LAN', 'AUTO'))
        panel, pc = connection_guide(model, mode)
        self.panel_guide.setText(panel)
        self.pc_guide.setText(pc)
        self.shown_presets = [r for r in self.presets if r.matches(model, mode)]
        with QSignalBlocker(self.preset_choice):
            self.preset_choice.clear()
            self.preset_choice.addItems([r.title() for r in self.shown_presets])
        self.show_preset()

    def show_preset(self):
        index = self.preset_choice.currentIndex()
        exists = 0 <= index < len(self.shown_presets)
        self.preset_choice.setVisible(exists)
        self.load_button.setVisible(exists)
        self.preset_detail.setText(self.shown_presets[index].details() if exists else '이 조건의 연결 기록이 없습니다.')

    def load_preset(self):
        preset = self.shown_presets[self.preset_choice.currentIndex()]
        values = preset.apply(self.value())
        self.model.setCurrentIndex(self.model.findData(values.model))
        self.mode.setCurrentIndex(self.mode.findData(values.mode))
        self.address.setText(values.address)
        self.port.setValue(values.port)
        self.mask.setText(values.mask)
        self.direct.setChecked(values.direct)
        self.timeout.setValue(values.timeout)
        self.operation_timeout.setValue(values.operation_timeout)
        for name in ('baudrate', 'data_bits', 'parity', 'stop_bits', 'flow', 'eol'):
            box = getattr(self, name)
            box.setCurrentIndex(box.findData(getattr(values, name)))
        self.preset_choice.setCurrentIndex(self.shown_presets.index(preset))

    def protocol_changed(self):
        ports = {'HIOKI_LAN': 3390, 'WT5000_TCP': 10002, 'PPA_LAN': 10001, 'DMM_TCP': 5025}
        if self.protocol.currentData() in ports:
            self.port.setValue(ports[self.protocol.currentData()])

    def model_changed(self):
        if self.role != 'dut':
            self.flow.setCurrentIndex(self.flow.findData('XON/XOFF' if self.model.currentData() == '5730A' else 'NONE'))
            self.update_calibrator()
            return
        # A newly selected family should not inherit another device's socket or framing.
        self.protocol.setCurrentIndex(self.protocol.findData('AUTO'))
        defaults = {'34401A': (9600, 7, 'E', 2, 'DTR/DSR', 'LF'),
                    'PPA': (19200, 8, 'N', 1, 'RTS/CTS', 'CR'),
                    'HIOKI': (38400, 8, 'N', 1, 'NONE', 'CRLF')}
        values = defaults.get(self.model.currentData(), (9600, 8, 'N', 1, 'NONE', 'LF'))
        for widget, value in zip((self.baudrate, self.data_bits, self.parity, self.stop_bits, self.flow, self.eol), values):
            widget.setCurrentIndex(widget.findData(value))

    def value(self):
        return ConnectionSettings(mode=self.mode.currentData(), model=self.model.currentData(), address=self.address.text().strip(),
            port=self.port.value(), mask=self.mask.text().strip(), direct=self.direct.isChecked(),
            baudrate=self.baudrate.currentData(), data_bits=self.data_bits.currentData(), parity=self.parity.currentData(),
            stop_bits=self.stop_bits.currentData(), flow=self.flow.currentData(), eol=self.eol.currentData(),
            timeout=self.timeout.value(), operation_timeout=self.operation_timeout.value(), lan_protocol=self.protocol.currentData())

class OptionsDialog(QDialog):
    def __init__(self, options, model, parent=None):
        super().__init__(parent)
        self.setWindowTitle('측정 설정')
        self.options = options
        self.resize(760, 560)
        outer = QVBoxLayout(self)
        columns = QHBoxLayout()
        outer.addLayout(columns)
        left, right = QVBoxLayout(), QVBoxLayout()
        columns.addLayout(left)
        columns.addLayout(right)
        basic = QGroupBox('실행')
        form = QFormLayout(basic)
        self.dwell = spin(options.dwell, .2, 86400, 2)
        self.capture = check('DUT 연결 시 측정값 자동 기록', options.capture)
        self.manual_progress = combo([('자동 진행 · 자유 입력', False), ('입력 대기 · Enter로 진행', True)],
                                     options.wait_for_entry)
        self.manual_progress.setToolTip('DUT 없이 측정하거나 측정값 자동 기록을 껐을 때 적용합니다.')
        self.auto_oper = check('설정 후 기준기 출력 (OPER)', options.auto_oper)
        self.channel = spin(options.default_channel, 1, 99)
        form.addRow('유지시간 (초)', self.dwell)
        form.addRow(self.capture)
        form.addRow('직접 입력 진행 방식', self.manual_progress)
        form.addRow(self.auto_oper)
        form.addRow('미배정 행의 DUT 채널', self.channel)
        left.addWidget(basic)
        groups = QGroupBox('단자와 실행 그룹')
        form = QFormLayout(groups)
        self.current = combo([('전체 · 단자 변경 시 정지', 'all'), ('작은 전류 + 전압·저항', 'low'), ('큰 전류·전력만', 'high')], options.current_group)
        self.basis = combo([('장비의 실제 단자', 'posts'), ('작업 기준: 3 A 이상', 'job')], options.current_basis)
        self.output = combo([('전체 출력', 'all'), ('싱글 출력', 'single'), ('듀얼 출력', 'dual')], options.output_group)
        self.terminal = combo([('자동 · 3 A 미만 / 3 A 이상 분리', 'auto'), ('3 A 단자 · 3 A 미만', '3'),
                               ('10 A 단자 · 3 A 이상', '10'), ('10 A 단자 · 모든 전류', '10_all')], options.dmm_terminal)
        for title, widget in [('실행 그룹', self.current), ('전류 구분', self.basis), ('5560A 출력 그룹', self.output), ('34461A / 34465A 입력', self.terminal)]:
            form.addRow(title, widget)
        note = QLabel(MODEL_PROFILES[model].terminal_note + '\n34461A·34465A는 3 A부터 10 A 단자\n리드는 STBY에서 직접 옮깁니다.')
        note.setObjectName('Subtitle')
        form.addRow(note)
        left.addWidget(groups)
        coil = QGroupBox('턴코일')
        form = QFormLayout(coil)
        self.coil = check('턴코일 사용', options.coil)
        self.turns = spin(options.turns, .001, 100000, 3)
        form.addRow(self.coil)
        form.addRow('배율 / 회전수', self.turns)
        form.addRow(QLabel('지원 모델·주파수에서 LCOMP를 자동 적용합니다.'))
        right.addWidget(coil)
        dual = QGroupBox('5560A 전류 듀얼 출력')
        form = QFormLayout(dual)
        self.dual = check('작은 전압을 함께 출력', options.dual_current)
        self.dc = QLineEdit(f'{options.dual_dc:g} V')
        self.ac = QLineEdit(f'{options.dual_ac:g} V')
        form.addRow(self.dual)
        form.addRow('DC 전압', self.dc)
        form.addRow('AC 전압', self.ac)
        form.addRow(QLabel('실제 듀얼 출력 지원 범위를 확인한 후 실행합니다.'))
        dual.setEnabled(model == '5560A')
        right.addWidget(dual)
        resistance = QGroupBox('저항')
        form = QFormLayout(resistance)
        self.comp = combo([('2선식 · 외부 보상 OFF', 'NONE'), ('2선 보상', 'WIRE2'), ('4선 보상', 'WIRE4')], options.compensation)
        self.zero = check('34461A·34465A에서 0 Ω 널값 설정 후 측정', options.zero_resistance)
        form.addRow('기준기 보상', self.comp)
        form.addRow(self.zero)
        note = QLabel('널 측정 시 보상 OFF · 2선식으로 고정합니다.\n0~10 MΩ 구간에서 불필요한 보상 제한을 피합니다.')
        note.setObjectName('Subtitle')
        form.addRow(note)
        right.addWidget(resistance)
        left.addStretch()
        right.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('적용')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('취소')
        buttons.accepted.connect(self.validated_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def value(self):
        return RunOptions(dwell=self.dwell.value(), capture=self.capture.isChecked(), auto_oper=self.auto_oper.isChecked(),
            wait_for_entry=self.manual_progress.currentData(),
            default_channel=self.channel.value(), coil=self.coil.isChecked(), turns=self.turns.value(), compensation=self.comp.currentData(),
            dual_current=self.dual.isChecked(), dual_dc=SIUnits.input_value(self.dc.text(), 'V'), dual_ac=SIUnits.input_value(self.ac.text(), 'V'),
            current_group=self.current.currentData(), current_basis=self.basis.currentData(), output_group=self.output.currentData(),
            dmm_terminal=self.terminal.currentData(), zero_resistance=self.zero.isChecked())

    def validated_accept(self):
        try:
            self.value().validate()
        except ValueError as error:
            QMessageBox.information(self, '설정 확인', str(error))
            return
        self.accept()

class ChannelDialog(QDialog):
    def __init__(self, rows, channels, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle('채널 배정')
        self.resize(650, 510)
        self.rows, self.assignments = rows, {}
        self.channels = channels
        layout = QVBoxLayout(self)
        intro = QLabel('반복되는 주파수·명목값을 항목별로 찾습니다. 미리보기를 확인한 후 적용하세요.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.mode = combo(['자동 감지', '선택 범위에 한 채널 지정', '배정 해제'])
        self.start = spin(selected[0] + 1 if selected else 1, 1, max(1, len(rows)))
        self.end = spin(selected[-1] + 1 if len(selected) > 1 else len(rows), 1, max(1, len(rows)))
        self.channel = QLineEdit('1')
        self.extra = combo([('추가 구간 보류', None)] + [(f'CH{c}@에서 재측정', c) for c in channels])
        for title, widget in [('방법', self.mode), ('시작 행', self.start), ('마지막 행', self.end),
                               ('직접 지정 (예: 3@)', self.channel), ('채널 수를 넘는 구간', self.extra)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        review = QPushButton('배정 미리보기')
        review.clicked.connect(self.review)
        layout.addWidget(review)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText('배정 적용')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('취소')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self.mode, self.extra):
            widget.currentIndexChanged.connect(self.invalidate)
        for widget in (self.start, self.end):
            widget.valueChanged.connect(self.invalidate)
        self.channel.textChanged.connect(self.invalidate)

    def invalidate(self):
        self.assignments = {}
        self.apply_button.setEnabled(False)

    def review(self):
        import re
        start, end = self.start.value() - 1, self.end.value()
        self.invalidate()
        if start >= end:
            self.preview.setPlainText('행 범위를 확인하세요.')
            return
        if self.mode.currentIndex() == 0:
            self.assignments, descriptions = suggest_channels(self.rows, self.channels, start, end, self.extra.currentData())
        else:
            key = None
            if self.mode.currentIndex() == 1:
                match = re.fullmatch(r'(?:CH)?\s*([1-9]\d?)(@\d*)?', self.channel.text(), re.I)
                if not match or int(match[1]) not in self.channels:
                    self.preview.setPlainText('사용 가능한 채널: ' + ', '.join(map(str, self.channels)))
                    return
                key = int(match[1]), match[2] or ''
            self.assignments = {row.uid: key for row in self.rows[start:end]}
            descriptions = [f'{start + 1}~{end}행 · {channel_name(key) if key else "배정 해제"}']
        self.preview.setPlainText('\n'.join(descriptions) or '반복 조건을 찾지 못했습니다. 직접 지정을 사용하세요.')
        self.apply_button.setEnabled(bool(self.assignments))
