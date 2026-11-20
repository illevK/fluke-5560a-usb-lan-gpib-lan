"""Protocols and parsing migrated from v6.10.38. Maintained as normal Python source."""
from __future__ import annotations
import os
import math
import re
import sys
import time
import socket
import tempfile
import ipaddress
import json
import threading
import queue
import subprocess
import datetime
import select
import struct
import base64
from contextlib import ExitStack
from pathlib import Path
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException
from typing import Optional, List, Tuple, Dict
from calibration.domain.commands import CalibratorCommandBuilder, Fluke5560ACommandBuilder, OutputRangeError
from calibration.domain.types import CHANNEL_MEASUREMENT_NAMES, MEASUREMENT_NAMES, SequenceStep
from calibration.domain.units import SIUnits


def clipboard_measurement_kind(label):
    key = re.sub(r"[\s._:\-]+", "", label.upper()).replace("직류", "DC").replace("교류", "AC")
    if key in CHANNEL_MEASUREMENT_NAMES:
        return key
    if key in ("POWERFACTOR", "역률"):
        return "PF"
    for mode, spellings in (("DC", ("DC", "DIRECTCURRENT")), ("AC", ("AC", "ALTERNATINGCURRENT"))):
        for suffix, names in (("V", ("VOLT", "VOLTAGE", "V", "전압")),
                              ("I", ("CURR", "CURRENT", "AMP", "A", "전류")),
                              ("P", ("POW", "POWER", "W", "전력"))):
            if any(key in (form + name, name + form) for form in spellings for name in names):
                return mode + suffix
    return None


def pasted_channel_hint(line):
    for cell in line.split("\t")[:-1]:
        label = re.fullmatch(r"\s*(CH\s*[-–]?\s*)?([1-9][0-9]?)\s*([*@]?)\s*", cell, re.I)
        if label and (label[1] or label[3] == "@"):
            return int(label[2]), "@" if label[3] else ""
    return None


def channel_name(key):
    return f"CH{key[0]}{key[1]}" if key is not None else ""


def _channel_frequency(step):
    parsed = SmartClipboardSequenceParser.parse_value_and_unit(step.freq_cond_raw)
    if parsed:
        base, scale = SIUnits.split(parsed[1])
        if base == "HZ":
            return parsed[0] * scale
    if step.freq_cond_raw.strip().upper() in ("", "-", "DC", "0"):
        return 0.0
    return step.freq_cond_raw.strip()


def _channel_measurement_kind(step):
    if step.func_type in CHANNEL_MEASUREMENT_NAMES:
        return step.func_type
    if step.func_type != "OUT_OF_RANGE":
        return None
    target = SmartClipboardSequenceParser.parse_value_and_unit(step.nominal_raw)
    if target is None:
        return "PF" if SmartClipboardSequenceParser.VI_SLASH_PATTERN.fullmatch(step.vi_cond_raw.strip()) else None
    base, _ = SIUnits.split(target[1])
    quantity = {"V": "V", "A": "I", "W": "P"}.get(base)
    frequency = _channel_frequency(step)
    if quantity and isinstance(frequency, (float, int)):
        return ("AC" if frequency > 0 else "DC") + quantity
    return None


def repeated_channel_blocks(steps, *, allow_tail=False):
    """Return row indices for exact repeated blocks of measurement conditions."""
    rows = [index for index, step in enumerate(steps) if step.func_type != "BLANK"]
    keys = [
        (_channel_measurement_kind(step), _channel_frequency(step),
         *(re.sub(r"\s+", "", value) for value in (step.vi_cond_raw, step.pf_cond_raw, step.nominal_raw)))
        for step in (steps[index] for index in rows)
    ]
    for size in range(2, len(keys) // 2 + 1):
        full, tail = divmod(len(keys), size)
        if tail and not allow_tail:
            continue
        pattern = keys[:size]
        matched = full * size
        if len(set(pattern)) > 1 and keys[:matched] == pattern * full:
            return [rows[start:start + size] for start in range(0, matched, size)]
    return []


def detect_channel_sections(steps):
    """Find a repeated sequence for each measurement type, then per frequency."""
    by_kind = {kind: [] for kind in CHANNEL_MEASUREMENT_NAMES}
    for index, step in enumerate(steps):
        kind = _channel_measurement_kind(step)
        if kind:
            by_kind[kind].append(index)

    def find_blocks(indices, allow_tail=False):
        positions = repeated_channel_blocks([steps[i] for i in indices], allow_tail=allow_tail)
        return [[indices[i] for i in block] for block in positions]

    sections = []
    for kind, indices in by_kind.items():
        name = CHANNEL_MEASUREMENT_NAMES[kind]
        if any(steps[index].channel_hint for index in indices):
            explicit = []
            for index in indices:
                if steps[index].channel_hint:
                    explicit.append([])
                if explicit:
                    explicit[-1].append(index)
            sections.append((name, explicit))
            continue
        blocks = find_blocks(indices)
        if blocks:
            sections.append((name, blocks))
            continue
        frequencies = {}
        for index in indices:
            frequencies.setdefault(_channel_frequency(steps[index]), []).append(index)
        if len(frequencies) < 2:
            if kind in ("DCI", "ACI") and (blocks := find_blocks(indices, allow_tail=True)):
                sections.append((name, blocks))
            continue
        for frequency, subset in frequencies.items():
            blocks = find_blocks(subset, allow_tail=kind in ("DCI", "ACI"))
            if blocks:
                label = f"{frequency:g} Hz" if isinstance(frequency, (float, int)) and frequency else frequency or "DC"
                sections.append((f"{name} · {label}", blocks))
    return sections


def channel_row_ranges(indices):
    ranges = []
    for index in indices:
        row = index + 1
        if ranges and ranges[-1][1] + 1 == row:
            ranges[-1][1] = row
        else:
            ranges.append([row, row])
    return ", ".join(str(start) if start == end else f"{start}~{end}" for start, end in ranges)


class SmartClipboardSequenceParser:
    """
    엑셀이나 성적서에서 복사한 텍스트(탭 또는 띄어쓰기 구분)를 행/열 단위로 자동 분류합니다.
    """

    VI_SLASH_PATTERN = SIUnits.VI
    TOKEN_FINDER = SIUnits.TOKEN
    FREQ_UNITS = set(SIUnits.units("HZ"))
    POWER_UNITS = set(SIUnits.units("W"))
    VOLTAGE_UNITS = set(SIUnits.units("V"))
    CURRENT_UNITS = set(SIUnits.units("A"))
    RESISTANCE_UNITS = set(SIUnits.units("OHM"))
    OUTPUT_UNITS = POWER_UNITS | VOLTAGE_UNITS | CURRENT_UNITS | RESISTANCE_UNITS

    @classmethod
    def parse_value_and_unit(cls, text):
        match = SIUnits.VALUE.fullmatch(SIUnits.clean(text))
        if match:
            try:
                return SIUnits.number(match[1]), SIUnits.normalize(match[2])
            except ValueError:
                pass
        return None

    @classmethod
    def parse_dc_power_row(cls, line):
        """A complete V/A pair alone represents a DC power point."""
        text = SIUnits.clean(line).strip()
        match = cls.VI_SLASH_PATTERN.fullmatch(text)
        if match is None:
            if text.count("/") == 1:
                left, right = (part.strip() for part in text.split("/"))
                if cls.TOKEN_FINDER.fullmatch(left) and cls.TOKEN_FINDER.fullmatch(right):
                    raise ValueError("전력 조건은 전압 / 전류 순서로 입력하세요. 예: 120 V / 1 A")
            return None
        volts = SIUnits.to_base(SIUnits.number(match[1]), match[2], "V")
        amps = SIUnits.to_base(SIUnits.number(match[3]), match[4], "A")
        if volts == 0:
            raise ValueError("DC 전력 조건의 전압은 0이 아닌 값으로 입력하세요.")
        watts = SIUnits.number(volts * amps)
        voltage = f"{match[1]} {match[2]}"
        current = f"{match[3]} {match[4]}"
        return (f"{voltage} / {current}", "DC", "1", f"{watts:.12g} W")

    @staticmethod
    def parse_power_factor(text):
        value_text = SIUnits.clean(text).strip()
        number = SIUnits.NUMBER
        # Accept the old signed display as well as 0.5 Lead / Lag 0.5 / 1lead.
        legacy = re.fullmatch(rf"({number})\s*\(\s*({number})\s+(LEAD|LAG)\s*\)", value_text, re.I)
        if legacy:
            if not math.isclose(abs(SIUnits.number(legacy[1])), abs(SIUnits.number(legacy[2]))):
                raise ValueError(f"역률 표시값이 서로 다릅니다: {text}")
            value_text = f"{legacy[2]} {legacy[3]}"
        match = re.fullmatch(rf"(?:(LEAD|LAG)\s*({number})|({number})\s*(LEAD|LAG)?)", value_text, re.I)
        if not match:
            raise ValueError(f"역률은 0.5 Lead, 0.5 Lag, 1처럼 입력하세요: {text}")
        signed = SIUnits.number(match[2] if match[1] else match[3])
        explicit = match[1] or match[4]
        value, direction, _ = Fluke5560ACommandBuilder.decode_signed_pf(signed)
        if explicit:
            direction = explicit.upper()
        if value == 1:
            direction = "LEAD"
        return value, direction, f"{value:g} {direction}"

    @classmethod
    def resolve_unsigned_pf_triplets(cls, rows):
        """Apply the spreadsheet convention only to a complete unsigned PF trio."""
        signatures = []
        for voltage_current, frequency, pf, nominal in rows:
            vi = cls.VI_SLASH_PATTERN.fullmatch(voltage_current.strip())
            freq = cls.parse_value_and_unit(frequency)
            text = SIUnits.clean(nominal).strip()
            if (vi is None or freq is None or SIUnits.split(freq[1])[0] != "HZ"
                    or pf != nominal or not re.fullmatch(SIUnits.NUMBER, text) or text.startswith(("+", "-"))):
                signatures.append(None)
                continue
            try:
                value = SIUnits.number(text)
                conditions = (SIUnits.to_base(vi[1], vi[2], "V"),
                              SIUnits.to_base(vi[3], vi[4], "A"),
                              SIUnits.to_base(*freq, "HZ"))
            except ValueError:
                signatures.append(None)
                continue
            signatures.append((value, conditions) if conditions[2] > 0 else None)

        resolved = list(rows)
        for index, (first, second, third) in enumerate(zip(signatures, signatures[1:], signatures[2:])):
            if first and first[0] == 1 and second == third == (0.5, first[1]):
                for offset, label in enumerate(("1 LEAD", "0.5 LEAD", "0.5 LAG")):
                    row = rows[index + offset]
                    resolved[index + offset] = (*row[:2], label, label)
        return resolved

    @classmethod
    def parse_tabular_row(cls, line):
        *conditions, nominal = (cell.strip() for cell in line.split("\t"))
        pair = None
        quantities = {}
        settings = []
        for cell in conditions:
            if not cell:
                continue
            vi = cls.VI_SLASH_PATTERN.fullmatch(cell)
            if vi:
                pair = vi
                continue
            quantity = cls.parse_value_and_unit(cell)
            if quantity:
                quantities[SIUnits.split(quantity[1])[0]] = cell
            else:
                settings.append(cell)

        quantity = cls.parse_value_and_unit(nominal)
        unit = SIUnits.split(quantity[1])[0] if quantity else None
        voltage, current = quantities.get("V"), quantities.get("A")
        frequency = quantities.get("HZ")
        freq_condition = frequency or next(
            (cell for cell in settings if cell.upper() in {"DC", "WIRE2", "WIRE4", "2W", "4W"}), "DC"
        )
        if pair:
            if unit == "W" and voltage is not None:
                paired_voltage = SIUnits.to_base(SIUnits.number(pair[1]), pair[2], "V")
                if not math.isclose(SIUnits.to_base(*cls.parse_value_and_unit(voltage), "V"), paired_voltage):
                    raise ValueError("전력 조건의 전압과 별도 전압 열이 서로 다릅니다.")
            voltage = f"{pair[1]} {pair[2]}" if unit == "W" or not voltage else voltage
            current = f"{pair[3]} {pair[4]}" if unit == "W" or not current else current
        vi_condition = f"{voltage} / {current}" if voltage and current else voltage or "-"
        if unit == "W":
            # Search from the measurements toward the left so reference columns
            # preceding the test conditions do not supply a phase setting.
            pf = "1"
            for cell in reversed(settings):
                try:
                    _, _, pf = cls.parse_power_factor(cell)
                except ValueError:
                    continue
                break
            return vi_condition, freq_condition, pf, nominal
        if unit in {"V", "A", "OHM"}:
            if pair:
                raise ValueError("전압 / 전류 조건의 마지막 열에는 전력(W 계열) 또는 역률을 입력하세요.")
            return "-", freq_condition, "-", nominal
        if voltage and current:
            if not frequency:
                raise ValueError("역률 출력에는 AC 주파수가 필요합니다. 예: 60 Hz")
            directions = set(re.findall(r"(?<![A-Za-z])(LEAD|LAG)(?![A-Za-z])",
                                        " ".join([*settings, nominal]).upper()))
            if len(directions) > 1:
                raise ValueError("한 역률 행에 Lead와 Lag를 동시에 지정할 수 없습니다.")
            if nominal and directions and not re.search(r"LEAD|LAG", nominal, re.I):
                nominal += " " + next(iter(directions))
            cls.parse_power_factor(nominal)
            return vi_condition, frequency, nominal, nominal
        if quantities:
            raise ValueError("마지막 열에 단위가 있는 명목값을 입력하세요.")
        return None

    @classmethod
    def parse_power_factor_row(cls, line):
        """Read separate Hz/V/A columns, ignoring reference numbers without units."""
        quantities = {}
        end = 0
        for match in cls.TOKEN_FINDER.finditer(line):
            try:
                base, _ = SIUnits.split(match[2])
            except ValueError:
                continue
            if base not in ("HZ", "V", "A") or base in quantities:
                return None
            quantities[base] = f"{match[1]} {match[2]}"
            end = match.end()
        if set(quantities) != {"HZ", "V", "A"}:
            return None
        tail = line[end:].strip()
        if not tail:
            return None
        # Direction may be in the first column or next to the final PF value.
        directions = set(re.findall(r"(?<![A-Za-z])(LEAD|LAG)(?![A-Za-z])", line.upper()))
        if len(directions) > 1:
            raise ValueError("한 역률 행에 Lead와 Lag를 동시에 지정할 수 없습니다.")
        if directions and not re.search(r"LEAD|LAG", tail, re.I):
            tail += " " + next(iter(directions))
        cls.parse_power_factor(tail)
        return (f"{quantities['V']} / {quantities['A']}", quantities['HZ'], tail, tail)

    @classmethod
    def build_power_factor_step(cls, step_no, vi_cond_raw, freq_cond_raw, nominal_raw,
                                use_coil, coil_turns, breakpoint, model):
        vi = cls.VI_SLASH_PATTERN.fullmatch(vi_cond_raw.strip())
        if not vi:
            raise ValueError("역률 출력에는 전압/전류 조건이 필요합니다. 예: 120 V / 10 A")
        volts = SIUnits.to_base(SIUnits.number(vi[1]), vi[2], "V")
        amps = SIUnits.to_base(SIUnits.number(vi[3]), vi[4], "A")
        frequency = cls.parse_value_and_unit(freq_cond_raw)
        if frequency is None:
            raise ValueError("역률 출력에는 AC 주파수가 필요합니다. 예: 60 Hz")
        hz = SIUnits.to_base(*frequency, "HZ")
        value, direction, display = cls.parse_power_factor(nominal_raw)
        result = CalibratorCommandBuilder(model).build_power_cmd(
            mode="ACP", calc_method="VI", voltage_v=volts, current_or_power_val=amps,
            power_unit="A", freq_hz=hz, phase_mode="DPF", dpf_val=value,
            dpf_lead_lag=direction, use_coil=use_coil, coil_turns=coil_turns)
        actual = result["actual_current_a"]
        desc = f"{volts:g} V, {actual:g} A, {hz:g} Hz, PF {display}"
        if use_coil and coil_turns != 1:
            desc += f" (코일 {coil_turns:g}턴 → {result['effective_current_a']:g} A)"
        return SequenceStep(step_no, vi_cond_raw.strip(), freq_cond_raw.strip(), display,
                            display, "PF", result["cmd"], desc, breakpoint=breakpoint)


    @classmethod
    def parse_labeled_clipboard_line(cls, raw_line, heading=None):
        text = SIUnits.clean(raw_line)
        label = clipboard_measurement_kind(text)
        if label:
            return None, label, True, None
        inline = None
        if "\t" in text:
            cells = text.split("\t")
            labeled = [(i, kind) for i, cell in enumerate(cells[:-1])
                       if (kind := clipboard_measurement_kind(cell))]
            if labeled:
                inline = labeled[0][1]
                indexes = {i for i, _ in labeled}
                text = "\t".join(cell for i, cell in enumerate(cells) if i not in indexes)
        else:
            start_value = re.search(r"(?<!\w)[+-]?(?:\d|\.\d)", text)
            if start_value:
                inline = clipboard_measurement_kind(text[:start_value.start()])
                if inline:
                    text = text[start_value.start():]

        kind = inline or heading
        fields = cls.parse_line_to_fields(text)
        if fields is None and inline:
            fields = ("-", "DC", "-", text.strip().split("\t")[-1])
        if fields is None or kind is None:
            return fields, heading, False, None
        vi, frequency, pf, nominal = fields
        has_explicit_frequency = any(
            (quantity := cls.parse_value_and_unit(token[0])) and quantity[1] in cls.FREQ_UNITS
            for token in cls.TOKEN_FINDER.finditer(text)
        )
        if not has_explicit_frequency:
            frequency = "DC" if kind.startswith("DC") else "AC"
        return (vi, frequency, pf, nominal), heading, False, kind

    @classmethod
    def _clipboard_type_issue(cls, kind, vi, frequency, nominal):
        quantity = cls.parse_value_and_unit(nominal)
        unit = SIUnits.split(quantity[1])[0] if quantity else None
        expected = {"V": "V", "I": "A", "P": "W"}.get(kind[-1])
        name = MEASUREMENT_NAMES[kind]
        if unit != expected or kind == "PF" and not cls.VI_SLASH_PATTERN.fullmatch(vi.strip()):
            return f"{name} 표기와 명목값({nominal})이 맞지 않습니다."
        reading = cls.parse_value_and_unit(frequency)
        hz = SIUnits.to_base(*reading, "HZ") if reading and reading[1] in cls.FREQ_UNITS else None
        if kind.startswith("DC"):
            if hz != 0 and frequency.strip().upper() not in ("", "-", "DC", "0"):
                return f"{name} 행의 주파수를 DC 또는 0 Hz로 입력하세요."
        elif hz is None or hz <= 0:
            return f"{name} 행에 0 Hz보다 큰 주파수를 입력하세요."
        return ""

    @classmethod
    def _is_header_line(cls, line: str) -> bool:
        low = line.strip().lower()
        header_keywords = ("spec", "range", "조건", "명목", "측정", "기준", "설정", "역률", "주파수")
        if any(k in low for k in header_keywords):
            for m in cls.TOKEN_FINDER.finditer(line):
                try:
                    u = Fluke5560ACommandBuilder.normalize_unit(m.group(2))
                    if u in cls.OUTPUT_UNITS or u in cls.FREQ_UNITS:
                        return False
                except ValueError:
                    continue
            return True
        return False

    @classmethod
    def build_sheet_step(cls, *, step_no, vi_cond_raw, freq_cond_raw, pf_cond_raw, nominal_raw,
                         breakpoint=False, declared_kind=None, **settings):
        if declared_kind and (issue := cls._clipboard_type_issue(declared_kind, vi_cond_raw, freq_cond_raw, nominal_raw)):
            return SequenceStep(step_no, vi_cond_raw, freq_cond_raw, pf_cond_raw, nominal_raw,
                                "ERROR", "", issue, "⚠ 측정 항목 확인", breakpoint, declared_kind=declared_kind)
        try:
            step = cls.build_Single_step_from_fields(step_no, vi_cond_raw, freq_cond_raw, pf_cond_raw,
                                                     nominal_raw, breakpoint=breakpoint, **settings)
        except OutputRangeError as exc:
            step = SequenceStep(step_no, vi_cond_raw, freq_cond_raw, pf_cond_raw, nominal_raw,
                                "OUT_OF_RANGE", "", str(exc).splitlines()[0], "⚠ 출력 범위 초과", breakpoint)
        step.declared_kind = declared_kind
        return step

    @classmethod
    def build_Single_step_from_fields(
        cls,
        step_no: int,
        vi_cond_raw: str,
        freq_cond_raw: str,
        pf_cond_raw: str,
        nominal_raw: str,
        use_coil: bool = False,
        coil_turns: float = 1.0,
        default_zcomp: str = "WIRE4",
        current_post: str = "AUTO",
        breakpoint: bool = False,
        model: str = "5560A",
    ) -> SequenceStep:
        vi_cond_raw = SIUnits.clean(vi_cond_raw)
        freq_cond_raw = SIUnits.clean(freq_cond_raw)
        if not nominal_raw.strip() and freq_cond_raw.strip().upper() in ("", "DC", "0", "0 HZ"):
            dc = cls.parse_dc_power_row(vi_cond_raw)
            if dc:
                nominal_raw, pf_cond_raw = dc[3], "1"
        parsed_nom = cls.parse_value_and_unit(nominal_raw)
        if parsed_nom is None and cls.VI_SLASH_PATTERN.fullmatch(vi_cond_raw.strip()):
            return cls.build_power_factor_step(step_no, vi_cond_raw, freq_cond_raw, nominal_raw,
                                               use_coil, coil_turns, breakpoint, model)
        if parsed_nom is None:
            raise ValueError(f"#{step_no}행의 명목값('{nominal_raw}')에서 수치와 단위를 인식할 수 없습니다. (예: 100 mV, 5.5 A, 60 W)")
        nom_val, nom_unit = parsed_nom

        freq_hz = 0.0
        has_freq = False
        freq_display = freq_cond_raw.strip() if freq_cond_raw.strip() and freq_cond_raw.strip() != "-" else "DC"
        for m in cls.TOKEN_FINDER.finditer(freq_cond_raw):
            f_val = SIUnits.number(m.group(1))
            try:
                f_unit = Fluke5560ACommandBuilder.normalize_unit(m.group(2))
                if f_unit in cls.FREQ_UNITS:
                    freq_hz = SIUnits.to_base(f_val, f_unit, "HZ")
                    if freq_hz < 0:
                        raise ValueError("주파수는 0 이상이어야 합니다.")
                    has_freq = freq_hz > 0
                    freq_display = f"{m.group(1)} {m.group(2)}"
            except ValueError as exc:
                raise ValueError(f"주파수 입력 오류: {freq_cond_raw}") from exc

        if nom_unit in cls.POWER_UNITS:
            m_vi = cls.VI_SLASH_PATTERN.search(vi_cond_raw)
            if m_vi:
                v_val = SIUnits.number(m_vi.group(1))
                v_unit = Fluke5560ACommandBuilder.normalize_unit(m_vi.group(2))
                i_val = SIUnits.number(m_vi.group(3))
                i_unit = Fluke5560ACommandBuilder.normalize_unit(m_vi.group(4))
                voltage_v = SIUnits.to_base(v_val, v_unit, "V")
                cond_current_a = SIUnits.to_base(i_val, i_unit, "A")
                vi_display = f"{m_vi.group(1)} {m_vi.group(2)} / {m_vi.group(3)} {m_vi.group(4)}"
            else:
                voltage_v = 220.0
                for m_v in cls.TOKEN_FINDER.finditer(vi_cond_raw):
                    u_v = Fluke5560ACommandBuilder.normalize_unit(m_v.group(2))
                    if u_v in cls.VOLTAGE_UNITS:
                        voltage_v = SIUnits.number(m_v.group(1)) * Fluke5560ACommandBuilder.unit_multiplier(u_v)
                        break
                cond_current_a = None
                vi_display = f"{voltage_v:g} V"

            dpf_val, dpf_lead_lag, pf_display = cls.parse_power_factor(pf_cond_raw if pf_cond_raw.strip() not in ("", "-") else "1")

            target_power_w = nom_val * Fluke5560ACommandBuilder.unit_multiplier(nom_unit)
            if voltage_v == 0:
                raise ValueError("전력 조건의 전압은 0이 아닌 값으로 입력하세요.")
            factor = dpf_val if has_freq else 1.0
            if cond_current_a is None:
                target_current_a = target_power_w / (voltage_v * factor)
            else:
                expected_power = voltage_v * cond_current_a * factor
                if not math.isclose(expected_power, target_power_w, rel_tol=0.02):
                    detail = f"조건 계산값 {expected_power:.9g} W / 명목값 {nominal_raw.strip()}"
                    return SequenceStep(step_no, vi_display, freq_display, pf_display, nominal_raw.strip(),
                                        "ERROR", "", detail, "⚠ 전력 불일치", breakpoint)
                target_current_a = cond_current_a

            mode = "ACP" if has_freq else "DCP"
            res = CalibratorCommandBuilder(model).build_power_cmd(
                mode=mode,
                calc_method="VI",
                voltage_v=voltage_v,
                current_or_power_val=target_current_a,
                power_unit="A",
                freq_hz=freq_hz if has_freq else 0.0,
                phase_mode="DPF",
                dpf_val=dpf_val,
                dpf_lead_lag=dpf_lead_lag,
                phase_deg=0.0,
                use_coil=use_coil,
                coil_turns=coil_turns,
            )
            actual_i = float(res["actual_current_a"])
            eff_i = float(res["effective_current_a"])
            if use_coil and coil_turns != 1.0:
                desc = f"{voltage_v:g}V, 실제 {actual_i:.6g}A (코일 {coil_turns:g}x➔{eff_i:.6g}A), {dpf_val:g} {dpf_lead_lag}"
            else:
                desc = f"{voltage_v:g}V, {actual_i:.6g}A, {freq_display}, {dpf_val:g} {dpf_lead_lag}"

            return SequenceStep(
                step_no=step_no,
                vi_cond_raw=vi_display,
                freq_cond_raw=freq_display,
                pf_cond_raw=pf_display,
                nominal_raw=nominal_raw.strip(),
                func_type=mode,
                scpi_cmd=str(res["cmd"]),
                actual_out_desc=desc,
                breakpoint=breakpoint,
            )

        if nom_unit in cls.VOLTAGE_UNITS:
            mode = "ACV" if has_freq else "DCV"
            scpi_cmd, _ = CalibratorCommandBuilder(model).build_voltage_cmd(
                mode=mode, value=nom_val, unit=nom_unit, freq_hz=freq_hz if has_freq else 0.0
            )
            desc = f"{nom_val:g} {nom_unit} ({freq_display})"
            return SequenceStep(
                step_no=step_no,
                vi_cond_raw="-",
                freq_cond_raw=freq_display,
                pf_cond_raw="-",
                nominal_raw=nominal_raw.strip(),
                func_type=mode,
                scpi_cmd=scpi_cmd,
                actual_out_desc=desc,
                breakpoint=breakpoint,
            )

        if nom_unit in cls.CURRENT_UNITS:
            mode = "ACI" if has_freq else "DCI"
            scpi_cmd, actual_a, effective_a = CalibratorCommandBuilder(model).build_current_cmd(
                mode=mode,
                input_value=nom_val,
                unit=nom_unit,
                freq_hz=freq_hz if has_freq else 0.0,
                use_coil=use_coil,
                coil_turns=coil_turns,
                current_post=current_post,
            )
            if use_coil and coil_turns != 1.0:
                desc = f"실제 {actual_a:.6g} A [{freq_display}] (코일 {coil_turns:g}x➔{effective_a:.6g} A)"
            else:
                desc = f"실제 {actual_a:.6g} A 직결 [{freq_display}]"
            if model == "5730A":
                desc += " · NORMAL (OUTPUT HI/LO)"
            return SequenceStep(
                step_no=step_no,
                vi_cond_raw="-",
                freq_cond_raw=freq_display,
                pf_cond_raw="-",
                nominal_raw=nominal_raw.strip(),
                func_type=mode,
                scpi_cmd=scpi_cmd,
                actual_out_desc=desc,
                breakpoint=breakpoint,
            )

        if nom_unit in cls.RESISTANCE_UNITS:
            zcomp_mode = default_zcomp
            c_up = freq_cond_raw.upper()
            if "4W" in c_up or "WIRE4" in c_up:
                zcomp_mode = "WIRE4"
            elif "2W" in c_up or "WIRE2" in c_up:
                zcomp_mode = "WIRE2"
            elif "NONE" in c_up:
                zcomp_mode = "NONE"
            scpi_cmd, _ = CalibratorCommandBuilder(model).build_resistance_cmd(
                value=nom_val, unit=nom_unit, zcomp=zcomp_mode
            )
            desc = f"{nom_val:g} {nom_unit} (ZCOMP: {zcomp_mode})"
            if model == "5730A":
                wiring = "4선식" if "EXTSENSE ON" in scpi_cmd else "2선식"
                comp = "ON" if "RCOMP ON" in scpi_cmd else "OFF"
                desc = f"{nom_val:g} {nom_unit} · {wiring} · 보상 {comp}"
            return SequenceStep(
                step_no=step_no,
                vi_cond_raw="-",
                freq_cond_raw=zcomp_mode,
                pf_cond_raw="-",
                nominal_raw=nominal_raw.strip(),
                func_type="OHM",
                scpi_cmd=scpi_cmd,
                actual_out_desc=desc,
                breakpoint=breakpoint,
            )

        raise ValueError(f"#{step_no}행의 단위('{nom_unit}')는 지원되지 않습니다.")

    @classmethod
    def parse_line_to_fields(cls, raw_line: str) -> Optional[Tuple[str, str, str, str]]:
        line = SIUnits.clean(raw_line).strip()
        if not line or cls._is_header_line(line):
            return None

        if "\t" in raw_line:
            return cls.parse_tabular_row(SIUnits.clean(raw_line))
        dc_row = cls.parse_dc_power_row(line)
        if dc_row is not None:
            return dc_row
        pf_row = cls.parse_power_factor_row(line)
        if pf_row is not None:
            return pf_row
        m_vi = cls.VI_SLASH_PATTERN.search(line)
        if m_vi:
            vi_cond_str = f"{m_vi.group(1)} {m_vi.group(2)} / {m_vi.group(3)} {m_vi.group(4)}"
            rest = line[m_vi.end():]

            freq_str = "60 Hz"
            power_nom_str = ""
            spans_to_mask: List[Tuple[int, int]] = []

            for m_tok in cls.TOKEN_FINDER.finditer(rest):
                try:
                    u_tok = Fluke5560ACommandBuilder.normalize_unit(m_tok.group(2))
                except ValueError:
                    continue
                spans_to_mask.append(m_tok.span())
                tok_text = f"{m_tok.group(1)} {m_tok.group(2)}"
                if u_tok in cls.FREQ_UNITS:
                    freq_str = tok_text
                elif u_tok in cls.POWER_UNITS:
                    power_nom_str = tok_text
                elif u_tok in cls.VOLTAGE_UNITS:
                    pass

            masked_chars = list(rest)
            for s_idx, e_idx in spans_to_mask:
                for k in range(s_idx, e_idx):
                    masked_chars[k] = " "
            rem_str = "".join(masked_chars)
            pf_match = re.search(r"([+-]?\d+(?:\.\d+)?)", rem_str)
            pf_str = pf_match.group(1) if pf_match else "1"
            direction = re.search(r"\b(LEAD|LAG)\b", rem_str, re.I)
            if direction:
                pf_str += " " + direction[1]

            if power_nom_str:
                return (vi_cond_str, freq_str, pf_str, power_nom_str)
            if pf_match and any(
                    SIUnits.split(token[2])[0] == "HZ" for token in cls.TOKEN_FINDER.finditer(rest)):
                cls.parse_power_factor(pf_str)
                return vi_cond_str, freq_str, pf_str, pf_str
            dc = cls.parse_dc_power_row(vi_cond_str)
            return vi_cond_str, freq_str, "1", dc[3]

        freq_tokens: List[str] = []
        output_tokens: List[Tuple[str, float, str]] = []
        wire_cond: str = ""

        up_line = line.upper()
        if "WIRE4" in up_line or re.search(r"\b4W\b", up_line):
            wire_cond = "4W"
        elif "WIRE2" in up_line or re.search(r"\b2W\b", up_line):
            wire_cond = "2W"

        for m in cls.TOKEN_FINDER.finditer(line):
            num_str, unit_str = m.group(1), m.group(2)
            raw_token = f"{num_str} {unit_str}"
            try:
                norm_unit = Fluke5560ACommandBuilder.normalize_unit(unit_str)
            except ValueError:
                continue

            if norm_unit in cls.FREQ_UNITS:
                freq_tokens.append(raw_token)
            elif norm_unit in cls.OUTPUT_UNITS:
                output_tokens.append((raw_token, SIUnits.number(num_str), norm_unit))

        if not output_tokens:
            return None

        nominal_raw, _, _ = output_tokens[-1]
        freq_cond = freq_tokens[-1] if freq_tokens else (wire_cond if wire_cond else "DC")
        return ("-", freq_cond, "-", nominal_raw)

    @classmethod
    def build_sequence_steps(
        cls,
        raw_text: str,
        use_coil: bool = False,
        coil_turns: float = 1.0,
        default_zcomp: str = "WIRE4",
        current_post: str = "AUTO",
        model: str = "5560A",
    ) -> List[SequenceStep]:
        steps: List[SequenceStep] = []
        step_counter = 0

        rows, kinds = [], []
        heading = None
        for line in raw_text.splitlines():
            fields, heading, _, kind = cls.parse_labeled_clipboard_line(line, heading)
            if fields is not None:
                rows.append(fields)
                kinds.append(kind)
        for (vi_c, fq_c, pf_c, nom_c), kind in zip(cls.resolve_unsigned_pf_triplets(rows), kinds):
            if kind:
                step_counter += 1
                steps.append(cls.build_sheet_step(step_no=step_counter, vi_cond_raw=vi_c, freq_cond_raw=fq_c,
                    pf_cond_raw=pf_c, nominal_raw=nom_c, declared_kind=kind, use_coil=use_coil,
                    coil_turns=coil_turns, default_zcomp=default_zcomp, current_post=current_post, model=model))
                continue
            step_counter += 1
            steps.append(
                cls.build_Single_step_from_fields(
                    model=model,
                    step_no=step_counter,
                    vi_cond_raw=vi_c,
                    freq_cond_raw=fq_c,
                    pf_cond_raw=pf_c,
                    nominal_raw=nom_c,
                    use_coil=use_coil,
                    coil_turns=coil_turns,
                    default_zcomp=default_zcomp,
                    current_post=current_post,
                )
            )
        return steps
