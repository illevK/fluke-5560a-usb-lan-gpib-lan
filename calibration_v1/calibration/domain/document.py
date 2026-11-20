"""Editable measurement documents, independent of widgets and hardware."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4
import math

from .types import SequenceStep
from .clipboard import SmartClipboardSequenceParser as Parser, pasted_channel_hint
from .commands import MODEL_PROFILES, OutputRangeError
from .units import SIUnits
from ..services.storage import write_json, read_json

@dataclass
class RunOptions:
    dwell: float = 3.5
    capture: bool = True
    auto_oper: bool = True
    coil: bool = False
    turns: float = 50.0
    compensation: str = 'NONE'
    dual_current: bool = False
    dual_dc: float = .01
    dual_ac: float = .033
    current_group: str = 'all'
    current_basis: str = 'posts'
    output_group: str = 'all'
    dmm_terminal: str = 'auto'
    default_channel: int = 1
    zero_resistance: bool = True
    wait_for_entry: bool = False

    def record_mode(self, dut_connected):
        if not self.auto_oper:
            return 'setup'
        if self.capture and dut_connected:
            return 'dut'
        return 'entry' if self.wait_for_entry else 'timed'

    def validate(self):
        if not math.isfinite(self.dwell) or not .2 <= self.dwell <= 86400:
            raise ValueError('유지시간은 0.2~86400초로 입력하세요.')
        if not math.isfinite(self.turns) or self.turns <= 0:
            raise ValueError('턴코일 배율은 0보다 커야 합니다.')
        if self.compensation not in ('NONE', 'WIRE2', 'WIRE4'):
            raise ValueError('저항 보상 설정을 확인하세요.')
        if self.current_group not in ('all', 'low', 'high') or self.current_basis not in ('posts', 'job'):
            raise ValueError('전류 그룹을 확인하세요.')
        if self.output_group not in ('all', 'single', 'dual') or self.dmm_terminal not in ('auto', '3', '10', '10_all'):
            raise ValueError('단자 그룹을 확인하세요.')
        if self.default_channel < 1:
            raise ValueError('입력 채널을 확인하세요.')
        if self.dual_current:
            MODEL_PROFILES['5560A'].validate_dual_current_voltages(self.dual_dc, self.dual_ac)

    def parser_settings(self, model):
        return dict(model=model, use_coil=self.coil, coil_turns=self.turns,
                    default_zcomp=self.compensation, current_post='AUTO')

@dataclass
class Row:
    step: SequenceStep
    uid: str = field(default_factory=lambda: uuid4().hex)
    order: int = 0
    simulated: bool = False

    def result(self):
        result = SIUnits.format_measurement(self.step.measured_raw, self.step.nominal_raw)
        return ('[미리보기] ' + result) if self.simulated and result else result

class Document:
    def __init__(self):
        self.rows: list[Row] = []
        self.options = RunOptions()
        self.model = '5560A'
        self.dirty = False
        self.path: Path | None = None

    @staticmethod
    def error_step(number, fields, error, kind=None):
        out_of_range = isinstance(error, OutputRangeError)
        return SequenceStep(number, *fields, 'OUT_OF_RANGE' if out_of_range else 'ERROR', '', str(error),
                            '범위 초과' if out_of_range else '입력 확인', declared_kind=kind)

    def build(self, number, fields, kind=None):
        try:
            return Parser.build_sheet_step(step_no=number, vi_cond_raw=fields[0], freq_cond_raw=fields[1],
                pf_cond_raw=fields[2], nominal_raw=fields[3], declared_kind=kind,
                **self.options.parser_settings(self.model))
        except (ValueError, OverflowError) as error:
            return self.error_step(number, fields, error, kind)

    def parse(self, text):
        records = []
        heading = None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                fields, heading, _, kind = Parser.parse_labeled_clipboard_line(line, heading)
            except (ValueError, OverflowError) as error:
                records.append((('-', '-', '-', line.strip()), None, None, error))
                continue
            if fields is not None:
                records.append((fields, kind, pasted_channel_hint(line), None))
        resolved = Parser.resolve_unsigned_pf_triplets([record[0] for record in records])
        rows = []
        for number, (fields, (_, kind, hint, error)) in enumerate(zip(resolved, records), 1):
            step = self.error_step(number, fields, error, kind) if error else self.build(number, fields, kind)
            step.channel_hint = hint
            rows.append(Row(step))
        return rows

    def insert(self, rows, index=None):
        index = len(self.rows) if index is None else index
        start_order = max((row.order for row in self.rows), default=-1) + 1
        for offset, row in enumerate(rows):
            row.order = start_order + offset
        self.rows[index:index] = rows
        self.renumber()

    def renumber(self):
        for number, row in enumerate(self.rows, 1):
            row.step.step_no = number
        self.dirty = True

    def rebuild(self, row):
        previous = row.step
        current = self.build(previous.step_no,
            (previous.vi_cond_raw, previous.freq_cond_raw, previous.pf_cond_raw, previous.nominal_raw), previous.declared_kind)
        for name in ('measured_raw', 'dut_channel', 'dut_channel_suffix', 'channel_hint', 'breakpoint'):
            setattr(current, name, getattr(previous, name))
        row.step = current
        self.dirty = True

    def rebuild_all(self):
        self.options.validate()
        for row in self.rows:
            self.rebuild(row)

    def save(self, path):
        value = {'format': 'calibration-automation', 'version': 1, 'model': self.model,
                 'options': asdict(self.options), 'rows': [asdict(row) for row in self.rows]}
        write_json(Path(path), value)
        self.path = Path(path)
        self.dirty = False

    @classmethod
    def load(cls, path):
        value = read_json(Path(path))
        if not isinstance(value, dict) or value.get('format') != 'calibration-automation' or value.get('version') != 1:
            raise ValueError('교정자동화 v1에서 저장한 측정표를 선택하세요.')
        document = cls()
        document.model = value.get('model', '5560A')
        if document.model not in MODEL_PROFILES:
            raise ValueError('저장된 기준기 모델을 확인하세요.')
        document.options = RunOptions(**value.get('options', {}))
        document.options.validate()
        seen = set()
        for data in value['rows']:
            step = SequenceStep(**data['step'])
            if step.channel_hint is not None:
                step.channel_hint = tuple(step.channel_hint)
            row = Row(step, data['uid'], int(data['order']), bool(data.get('simulated')))
            if row.uid in seen:
                raise ValueError('측정표에 중복 행 식별자가 있습니다.')
            seen.add(row.uid)
            document.rows.append(row)
            document.rebuild(row)
        document.renumber()
        document.path, document.dirty = Path(path), False
        return document
