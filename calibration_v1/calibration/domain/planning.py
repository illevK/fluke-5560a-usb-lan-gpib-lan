"""Compile immutable run points and wiring boundaries without any device I/O."""
from __future__ import annotations
from dataclasses import dataclass, replace
import re
from .commands import MODEL_PROFILES
from .groups import SequenceCurrentFilter
from .types import SequenceStep, DMM_MODELS, TRUEVOLT_MODELS
from .document import RunOptions, Row
from .clipboard import detect_channel_sections, channel_name, channel_row_ranges

QUANTITIES = {'DCV': ('dc_voltage', 'V'), 'ACV': ('voltage', 'V'),
              'DCI': ('dc_current', 'A'), 'ACI': ('current', 'A'),
              'DCP': ('power', 'W'), 'ACP': ('power', 'W'), 'PF': ('pf', ''),
              'OHM': ('resistance', 'OHM'), 'FREQ': ('frequency', 'Hz')}

@dataclass(frozen=True)
class Point:
    uid: str
    step: SequenceStep
    command: str
    channel: int
    wiring: tuple
    dual: bool = False
    terminal: int | None = None
    error: str = ''

def compile_point(row: Row, options: RunOptions, model: str, dut_model='') -> Point:
    step = replace(row.step)
    command, dual, terminal, error = step.scpi_cmd, False, None, ''
    channel = step.dut_channel or options.default_channel
    group = step.channel_key or (channel, '')
    source_wiring = None
    dmm_wiring = None
    try:
        if not command or step.func_type not in QUANTITIES:
            raise ValueError(step.actual_out_desc or '이 행의 입력을 확인하세요.')
        if options.dual_current and model == '5560A' and step.func_type in ('DCI', 'ACI'):
            dual_command = MODEL_PROFILES[model].dual_current_command(command, dc_voltage=options.dual_dc, ac_voltage=options.dual_ac)
            if dual_command:
                command, dual = dual_command, True
            elif options.output_group != 'single':
                raise ValueError('이 AC 전류는 듀얼 출력 범위를 벗어납니다. 싱글 출력 그룹으로 결선을 바꿔 실행하세요.')
        values = MODEL_PROFILES[model].output_values(command.partition(';')[0])
        if 'A' in values:
            high = MODEL_PROFILES[model].high_current(values['A'])
            source_wiring = ('high' if high else 'low',
                             'dual' if 'V' in values else 'single') if model == '5560A' else ('high' if high else 'low', '')
        if dut_model in DMM_MODELS:
            dmm_wiring = 'current' if step.func_type in ('DCI', 'ACI') else 'resistance' if step.func_type == 'OHM' else 'voltage'
            maximum = 750 if step.func_type == 'ACV' else 1000
            if step.func_type in ('ACV', 'DCV') and abs(values.get('V', 0)) > maximum:
                raise ValueError(f'{dut_model} 입력 한계 {maximum} V를 초과합니다.')
            if dut_model == '34401A' and step.func_type in ('DCI', 'ACI') and abs(values['A']) > 3:
                raise ValueError('34401A의 전류 측정 한계 3 A를 초과합니다.')
        if dut_model in TRUEVOLT_MODELS and step.func_type in ('DCI', 'ACI'):
            amps = abs(values['A'])
            if amps > 10:
                raise ValueError(f'{dut_model}의 전류 측정 한계 10 A를 초과합니다.')
            terminal = 10 if amps >= 3 or options.dmm_terminal == '10_all' else 3
            dmm_wiring = f'current_{terminal}'
        if dut_model in TRUEVOLT_MODELS and step.func_type == 'OHM' and options.capture and options.zero_resistance:
            command = MODEL_PROFILES[model].prepare_generated(command.partition(';')[0] + '; ZCOMP NONE')
        MODEL_PROFILES[model].validate_command(command)
    except (ValueError, KeyError) as exc:
        error = str(exc)
    return Point(row.uid, step, command, channel, (group, source_wiring, dmm_wiring), dual, terminal, error)

def includes(row: Row, options: RunOptions, model, dut_model=''):
    step = row.step
    if step.func_type == 'BLANK':
        return False
    if step.func_type in ('ERROR', 'OUT_OF_RANGE'):
        return True
    amps = SequenceCurrentFilter.actual_current(step)
    if options.current_group != 'all':
        basis = SequenceCurrentFilter.POSTS if options.current_basis == 'posts' else SequenceCurrentFilter.JOB
        if not SequenceCurrentFilter.includes(step, options.current_group == 'high', basis, model):
            return False
    if dut_model in TRUEVOLT_MODELS and options.dmm_terminal != 'auto':
        if amps is None and options.dmm_terminal != '3':
            return False
        if step.func_type in ('DCI', 'ACI') and amps is not None:
            if options.dmm_terminal == '3' and amps >= 3 or options.dmm_terminal == '10' and amps < 3:
                return False
    if model == '5560A' and options.output_group != 'all':
        dual = step.func_type in ('DCP', 'ACP', 'PF')
        if options.dual_current and step.func_type in ('DCI', 'ACI'):
            dual = MODEL_PROFILES[model].dual_current_command(step.scpi_cmd,
                    dc_voltage=options.dual_dc, ac_voltage=options.dual_ac) is not None
        if dual != (options.output_group == 'dual'):
            return False
    return True

def make_plan(rows, start, options, model, dut_model='', single=False):
    options.validate()
    if not 0 <= start < len(rows):
        raise ValueError('시작할 측정 행을 선택하세요.')
    key = rows[start].step.channel_key
    if not includes(rows[start], options, model, dut_model):
        raise ValueError('선택한 행은 현재 그룹에 포함되지 않습니다. 측정 설정의 그룹을 바꾸세요.')
    selected = [row for row in rows[start:] if row.step.channel_key == key and includes(row, options, model, dut_model)]
    if single:
        selected = selected[:1]
    return tuple(compile_point(row, options, model, dut_model) for row in selected)

def wiring_change(previous, current):
    if previous is None:
        return ''
    for before, after, label in zip(previous, current, ('DUT 채널', '기준기 전류 단자 / 출력 결선', 'DUT 입력 단자')):
        if before is not None and after is not None and before != after:
            return label + ' 변경이 필요합니다. STBY에서 결선을 확인하고 시작하세요.'
    return ''

def advance_wiring(previous, current):
    return tuple(now if now is not None else old for old, now in zip(previous or (None, None, None), current))

def suggest_channels(rows, channels, start=0, end=None, extra_channel=None):
    end = len(rows) if end is None else end
    selection = [row.step for row in rows[start:end]]
    sections = detect_channel_sections(selection)
    assignments, descriptions = {}, []
    templates = {}
    for _, blocks in sections:
        hints = tuple(selection[block[0]].channel_hint for block in blocks)
        if all(hints):
            templates.setdefault(len(blocks), set()).add(hints)
    for label, blocks in sections:
        hints = tuple(selection[block[0]].channel_hint for block in blocks)
        known = templates.get(len(blocks), set())
        if not any(hints) and len(known) == 1:
            hints = next(iter(known))
        extra_count = 0
        for position, block in enumerate(blocks):
            hint = hints[position]
            if hint and hint[1] or not hint and position >= len(channels):
                extra_count += 1
                key = (extra_channel or (hint[0] if hint else 0), '@' if extra_count == 1 else f'@{extra_count}')
            else:
                key = hint or (channels[position], '')
            indices = [index + start for index in block]
            if key[0] not in channels:
                descriptions.append(f'{label} · {channel_row_ranges(indices)}행: 추가 채널 선택 필요')
                continue
            assignments.update((rows[index].uid, key) for index in indices)
            descriptions.append(f'{label} · {channel_name(key)} · {channel_row_ranges(indices)}행')
    return assignments, descriptions
