#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fluke 5560A / 5522A / 5520A / 5080A Controller v6.10.4
LAN: verified physical adapter, source-IP binding, Telnet stream framing,
strict *IDN? validation, read-only diagnosis, background connection tasks.
USB/GPIB: PyVISA with backend diagnostics and correct interface handling.
RS232: pyserial, including PC USB-to-RS232 adapters, with configurable line settings.
No automatic PC IP/DHCP changes; no open-port identification fallback.
Reference: Fluke 5560A Remote Programmers Manual, Rev. 3 (3/25),
https://media.fluke.com/b2b30a71-26d0-4067-be01-b308017892a8_original%20file.pdf
"""

from __future__ import annotations

import os
import math
import re
from decimal import Decimal, DecimalException
import sys
import time
import socket
import ipaddress
import json
import threading
import queue
import argparse
import subprocess
import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

# Tkinter 선택적 임포트 (GUI 환경에서는 데스크톱 GUI 실행, 미설치 환경에서는 대화형 CLI 실행)
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    HAS_TKINTER = True
except ImportError:
    tk = None
    ttk = None
    messagebox = None
    HAS_TKINTER = False

# PyVISA 선택적 임포트 (설치되어 있지 않아도 LAN 소켓 및 시뮬레이션 모드는 정상 동작)
PYVISA_IMPORT_ERROR = ""
try:
    import pyvisa
    HAS_PYVISA = True
except ImportError as exc:
    pyvisa = None
    HAS_PYVISA = False
    PYVISA_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def dependency_import_message(package, display_name, import_error):
    """Target the interpreter running this app, independent of PATH/py defaults."""
    if getattr(sys, "frozen", False):
        return (f"프로그램에 포함된 {display_name}를 불러오지 못했습니다.\n"
                f"원인: {import_error or '모듈을 사용할 수 없음'}\n"
                "최신 Fluke 교정기.exe 파일로 교체한 뒤 다시 실행하세요.")
    executable = sys.executable
    if os.path.basename(executable).lower() == "pythonw.exe":
        console_python = os.path.join(os.path.dirname(executable), "python.exe")
        if os.path.isfile(console_python):
            executable = console_python
    return (
        f"현재 프로그램의 Python에서 {display_name}를 불러올 수 없습니다.\n"
        f"실행 Python: {sys.executable}\n"
        f"원인: {import_error or '모듈을 사용할 수 없음'}\n\n"
        "Python이 여러 개이면 CMD에서 설치한 환경과 다를 수 있습니다.\n"
        "CMD(명령 프롬프트)에서 아래 명령을 실행하세요:\n"
        f'"{executable}" -m pip install {package}\n\n'
        "설치를 마쳤다면 이 프로그램을 완전히 종료한 뒤 다시 실행하세요."
    )


_VISA_POOL_LOCK = threading.RLock()
_VISA_POOL_MANAGER = None
_VISA_POOL_USERS = 0


class SharedVisaManager:
    """Own one lease on PyVISA's cached, process-wide resource manager.

    Closing a raw ResourceManager closes every resource it created, including
    other connections. Each lease owns its resources; only the last lease may
    close the shared manager. Device queries remain on separate VISA sessions.
    """
    def __init__(self):
        global _VISA_POOL_MANAGER, _VISA_POOL_USERS
        self._manager = None
        self._resources = []
        with _VISA_POOL_LOCK:
            if _VISA_POOL_MANAGER is None:
                _VISA_POOL_MANAGER = pyvisa.ResourceManager()
            self._manager = _VISA_POOL_MANAGER
            _VISA_POOL_USERS += 1

    def _require_open(self):
        if self._manager is None:
            raise ConnectionError("VISA 관리자 사용이 끝났습니다.")
        return self._manager

    @property
    def visalib(self):
        with _VISA_POOL_LOCK:
            return self._require_open().visalib

    def list_resources(self, *args, **kwargs):
        with _VISA_POOL_LOCK:
            return self._require_open().list_resources(*args, **kwargs)

    def open_resource(self, *args, **kwargs):
        with _VISA_POOL_LOCK:
            resource = self._require_open().open_resource(*args, **kwargs)
            self._resources.append(resource)
            return resource

    def close_resource(self, resource):
        with _VISA_POOL_LOCK:
            for index, owned in enumerate(self._resources):
                if owned is resource:
                    self._resources.pop(index)
                    try:
                        resource.close()
                    except Exception:
                        pass
                    return

    def close(self):
        global _VISA_POOL_MANAGER, _VISA_POOL_USERS
        with _VISA_POOL_LOCK:
            if self._manager is None:
                return
            for resource in list(self._resources):
                self.close_resource(resource)
            manager, self._manager = self._manager, None
            _VISA_POOL_USERS -= 1
            if _VISA_POOL_USERS == 0:
                _VISA_POOL_MANAGER = None
                manager.close()


# =====================================================================
# 1. 하드웨어 사양 상수 및 기본 샘플 데이터 (Fluke 5560A 기준)
# =====================================================================
MAX_VOLTAGE_V = 1020.0          # 최대 AC/DC 전압 (1020 V)
MAX_CURRENT_A = 30.0            # 최대 연속 AC/DC 전류 (30 A, 무휴지 연속 출력)
MAX_RESISTANCE_OHM = 1.1e9      # 최대 저항 (1100 MΩ)
DEFAULT_LAN_PORT = 3490         # Fluke 5560A 공식 Ethernet Telnet 포트 (CRLF 종료)
HIGH_VOLTAGE_WARN_V = 30.0      # 고전압 주의 기준 (30 V 이상)

# 샘플 1: 사용자 제공 AC 전력 63행 데이터
SAMPLE_POWER_TABLE = """120 V / 500 mA\t\t\t\t\t50 Hz\t1 \t120 V\t60 W
120 V / 1 A   \t\t\t\t\t50 Hz\t1 \t120 V\t120 W
120 V / 5 A   \t\t\t\t\t50 Hz\t1 \t120 V\t600 W
120 V / 20 A   \t\t\t\t\t50 Hz\t1 \t120 V\t2.4 kW
220 V / 1 A   \t\t\t\t\t50 Hz\t1 \t220 V\t220 W
240 V / 5 mA\t\t\t\t\t50 Hz\t1 \t240 V\t1.2 W
240 V / 1 A   \t\t\t\t\t50 Hz\t1 \t240 V\t240 W
240 V / 5 A   \t\t\t\t\t50 Hz\t1 \t240 V\t1.2 kW
240 V / 20 A   \t\t\t\t\t50 Hz\t1 \t240 V\t4.8 kW
120 V / 10 mA\t\t\t\t\t50 Hz\t-0.5 \t120 V\t0.6 W
120 V / 500 mA\t\t\t\t\t50 Hz\t-0.5 \t120 V\t30 W
120 V / 1 A   \t\t\t\t\t50 Hz\t-0.5 \t120 V\t60 W
120 V / 5 A   \t\t\t\t\t50 Hz\t-0.5 \t120 V\t300 W
120 V / 20 A   \t\t\t\t\t50 Hz\t-0.5 \t120 V\t1.2 kW
220 V / 1 A   \t\t\t\t\t50 Hz\t-0.5 \t220 V\t110 W
240 V / 5 mA\t\t\t\t\t50 Hz\t-0.5 \t240 V\t0.6 W
240 V / 1 A   \t\t\t\t\t50 Hz\t-0.5 \t240 V\t120 W
240 V / 5 A   \t\t\t\t\t50 Hz\t-0.5 \t240 V\t0.6 kW
240 V / 20 A   \t\t\t\t\t50 Hz\t-0.5 \t240 V\t2.4 kW
120 V / 10 mA\t\t\t\t\t50 Hz\t0.5 \t120 V\t0.6 W
120 V / 500 mA\t\t\t\t\t50 Hz\t0.5 \t120 V\t30 W
120 V / 1 A   \t\t\t\t\t50 Hz\t0.5 \t120 V\t60 W
120 V / 5 A   \t\t\t\t\t50 Hz\t0.5 \t120 V\t300 W
120 V / 20 A   \t\t\t\t\t50 Hz\t0.5 \t120 V\t1.2 kW
220 V / 1 A   \t\t\t\t\t50 Hz\t0.5 \t220 V\t110 W
240 V / 5 mA\t\t\t\t\t50 Hz\t0.5 \t240 V\t0.6 W
240 V / 1 A   \t\t\t\t\t50 Hz\t0.5 \t240 V\t120 W
240 V / 5 A   \t\t\t\t\t50 Hz\t0.5 \t240 V\t0.6 kW
240 V / 20 A   \t\t\t\t\t50 Hz\t0.5 \t240 V\t2.4 kW
120 V / 10 mA\t\t\t\t\t60 Hz\t1 \t120 V\t1.2 W
120 V / 500 mA\t\t\t\t\t60 Hz\t1 \t120 V\t60 W
120 V / 1 A   \t\t\t\t\t60 Hz\t1 \t120 V\t120 W
120 V / 5 A   \t\t\t\t\t60 Hz\t1 \t120 V\t600 W
120 V / 20 A   \t\t\t\t\t60 Hz\t1 \t120 V\t2.4 kW
240 V / 5 mA\t\t\t\t\t60 Hz\t1 \t240 V\t1.2 W
220 V / 1 A   \t\t\t\t\t60 Hz\t1 \t220 V\t220 W
240 V / 1 A   \t\t\t\t\t60 Hz\t1 \t240 V\t240 W
240 V / 5 A   \t\t\t\t\t60 Hz\t1 \t240 V\t1.2 kW
240 V / 20 A   \t\t\t\t\t60 Hz\t1 \t240 V\t4.8 kW
120 V / 10 mA\t\t\t\t\t60 Hz\t-0.5 \t120 V\t0.6 W
120 V / 500 mA\t\t\t\t\t60 Hz\t-0.5 \t120 V\t30 W
120 V / 1 A   \t\t\t\t\t60 Hz\t-0.5 \t120 V\t60 W
120 V / 5 A   \t\t\t\t\t60 Hz\t-0.5 \t120 V\t300 W
120 V / 20 A   \t\t\t\t\t60 Hz\t-0.5 \t120 V\t1.2 kW
220 V / 1 A   \t\t\t\t\t60 Hz\t-0.5 \t220 V\t110 W
240 V / 5 mA\t\t\t\t\t60 Hz\t-0.5 \t240 V\t0.6 W
240 V / 1 A   \t\t\t\t\t60 Hz\t-0.5 \t240 V\t120 W
240 V / 5 A   \t\t\t\t\t60 Hz\t-0.5 \t240 V\t0.6 kW
240 V / 20 A   \t\t\t\t\t60 Hz\t-0.5 \t240 V\t2.4 kW
120 V / 10 mA\t\t\t\t\t60 Hz\t0.5 \t120 V\t0.6 W
120 V / 500 mA\t\t\t\t\t60 Hz\t0.5 \t120 V\t30 W
120 V / 1 A   \t\t\t\t\t60 Hz\t0.5 \t120 V\t60 W
120 V / 5 A   \t\t\t\t\t60 Hz\t0.5 \t120 V\t300 W
120 V / 20 A   \t\t\t\t\t60 Hz\t0.5 \t120 V\t1.2 kW
220 V / 1 A   \t\t\t\t\t60 Hz\t0.5 \t220 V\t110 W
240 V / 5 mA\t\t\t\t\t60 Hz\t0.5 \t240 V\t0.6 W
240 V / 1 A   \t\t\t\t\t60 Hz\t0.5 \t240 V\t120 W
240 V / 5 A   \t\t\t\t\t60 Hz\t0.5 \t240 V\t0.6 kW
240 V / 20 A   \t\t\t\t\t60 Hz\t0.5 \t240 V\t2.4 kW
115 V / 2.087 mA\t\t\t\t\t60 Hz\t1 \t115 V\t0.24 W
115 V / 4.348 mA\t\t\t\t\t60 Hz\t1 \t115 V\t0.5 W
115 V / 8.696 mA\t\t\t\t\t60 Hz\t1 \t115 V\t1 W
115 V / 43.478 mA\t\t\t\t\t60 Hz\t1 \t115 V\t5 W"""

# 샘플 2: 사용자 제공 DC 전압 표
SAMPLE_DCV_TABLE = """Spec\tRange\t조건1\t명목값
99\t600 mV\t\t100.0 mV
-101\t600 mV\t\t-100.0 mV
0.99\t6 V\t\t1.000 V
-1.01\t6 V\t\t-1.000 V
54.45\t60 V\t\t55.00 V
-55.55\t60 V\t\t-55.00 V
39.6\t60 V\t\t40.00 V
-40.4\t60 V\t\t-40.00 V
29.7\t60 V\t\t30.00 V
-30.3\t60 V\t\t-30.00 V
19.8\t60 V\t\t20.00 V
-20.2\t60 V\t\t-20.00 V
9.9\t60 V\t\t10.00 V
-10.1\t60 V\t\t-10.00 V
594\t600 V\t\t600.0 V
-606\t600 V\t\t-600.0 V"""

# 샘플 3: 사용자 제공 주파수 조건 포함 AC 전압
SAMPLE_ACV_TABLE = """100 Hz\t\t\t100 mV
1 kHz\t\t\t100 mV
100 Hz\t\t\t1 V
1 kHz\t\t\t1 V
100 Hz\t\t\t55 V
1 kHz\t\t\t55 V
1 kHz\t\t\t40 V
1 kHz\t\t\t30 V
1 kHz\t\t\t20 V
1 kHz\t\t\t10 V
100 Hz\t\t\t600 V
1 kHz\t\t\t600 V"""

# 샘플 4: 사용자 제공 주파수 조건 포함 AC 전류
SAMPLE_ACI_TABLE = """100 Hz\t\t\t5.5 A
100 Hz\t\t\t4 A
100 Hz\t\t\t3 A
100 Hz\t\t\t2 A
100 Hz\t\t\t1 A
1 kHz\t\t\t5.5 A
1 kHz\t\t\t4 A
1 kHz\t\t\t3 A
1 kHz\t\t\t2 A
1 kHz\t\t\t1 A
100 Hz\t\t\t10 A
1 kHz\t\t\t10 A"""


@dataclass
class ConnectionInfo:
    """연결된 장비의 인터페이스 및 식별 정보"""
    connected: bool = False
    interface_type: str = "DISCONNECTED"
    resource_name: str = ""
    idn_string: str = ""
    discovered_ip: str = ""
    discovered_port: int = DEFAULT_LAN_PORT


@dataclass
class SequenceStep:
    """엑셀 스타일 시트의 각 행(Row)에 대응하는 시퀀스 스텝 데이터"""
    step_no: int
    vi_cond_raw: str        # A열: 전압/전류 조건 (예: "120 V / 500 mA", 해당 없으면 "-")
    freq_cond_raw: str      # B열: 주파수/보상 조건 (예: "50 Hz", "1 kHz", "DC")
    pf_cond_raw: str        # C열: 역률 표시 (예: "1 (1.0 LEAD)", "-0.5 (0.5 LEAD)", "0.5 (0.5 LAG)", 해당 없으면 "-")
    nominal_raw: str        # D열: 명목값 (예: "60 W", "2.4 kW", "100.0 mV", "5.5 A")
    func_type: str          # 기능: DCV, ACV, DCI, ACI, OHM, DCP, ACP
    scpi_cmd: str           # 전송될 실제 SCPI 명령
    actual_out_desc: str    # 실제 5560A 출력 요약 (턴코일 적용 시 환산 전류 표시)
    status: str = "대기"
    breakpoint: bool = False
    measured_raw: str = ""


# =====================================================================
# 2. SCPI 명령 생성 및 턴코일/전력 연산 엔진 (비즈니스 로직)
# =====================================================================
class SIUnits:
    """User-facing SI notation. Prefix case is significant, especially m versus M."""
    PREFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
                "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    BASES = ("V", "A", "W", "OHM", "HZ")
    NUMBER = r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    UNIT = r"[a-zA-ZμµΩΩ가-힣]+"
    TOKEN = re.compile(r"(?<![\w.,+\-])(" + NUMBER + r")\s*(" + UNIT + r")(?![\w.])")
    VALUE = re.compile(r"\s*(" + NUMBER + r")\s*(" + UNIT + r")\s*")
    VI = re.compile(r"(?<![\w.,+\-])(" + NUMBER + r")\s*(" + UNIT + r")\s*/\s*(" + NUMBER + r")\s*(" + UNIT + r")(?![\w.])")

    @staticmethod
    def clean(text):
        return str(text).replace("−", "-").replace("μ", "u").replace("µ", "u")

    @staticmethod
    def number(text):
        value = float(str(text).replace(",", ""))
        if not math.isfinite(value):
            raise ValueError("유한한 수치를 입력하세요.")
        return value

    @classmethod
    def normalize(cls, unit):
        text = cls.clean(unit).strip().replace("Ω", "OHM").replace("Ω", "OHM")
        aliases = {"ohms": "OHM", "ohm": "OHM", "hz": "HZ", "v": "V", "a": "A", "w": "W",
                   "볼트": "V", "암페어": "A", "와트": "W", "옴": "OHM", "헤르츠": "HZ"}
        names = {"pico": "p", "nano": "n", "micro": "u", "milli": "m", "kilo": "k", "mega": "M", "giga": "G", "tera": "T",
                 "피코": "p", "나노": "n", "마이크로": "u", "밀리": "m", "킬로": "k", "메가": "M", "기가": "G", "테라": "T"}
        for suffix, base in aliases.items():
            if not text.lower().endswith(suffix):
                continue
            prefix = text[:-len(suffix)]
            prefix = names.get(prefix.lower(), {"U": "u", "K": "k"}.get(prefix, prefix))
            if prefix in cls.PREFIXES:
                return prefix + base
        raise ValueError(f"지원하지 않는 단위: {unit}. 접두어 p/n/u(µ)/m/k/M/G/T를 사용하세요. m과 M은 서로 다릅니다.")

    @classmethod
    def split(cls, unit):
        normalized = cls.normalize(unit)
        for base in cls.BASES:
            if normalized.endswith(base):
                return base, cls.PREFIXES[normalized[:-len(base)]]
        raise ValueError(f"지원하지 않는 단위: {unit}")

    @classmethod
    def units(cls, base):
        return tuple(prefix + base for prefix in cls.PREFIXES)

    @classmethod
    def to_base(cls, value, unit, expected):
        base, scale = cls.split(unit)
        if base != expected:
            raise ValueError(f"{expected} 계열의 단위를 입력하세요: {unit}")
        return cls.number(cls.number(value) * scale)

    @classmethod
    def input_value(cls, text, default_unit):
        """Manual input fields accept either a number or a complete quantity."""
        cleaned = cls.clean(text).strip()
        match = cls.VALUE.fullmatch(cleaned)
        if match:
            return cls.to_base(cls.number(match[1]), match[2], default_unit)
        if not re.fullmatch(cls.NUMBER, cleaned):
            raise ValueError(f"수치 또는 단위가 붙은 값을 입력하세요: {text}")
        return cls.number(cleaned)

    @classmethod
    def with_nominal_unit(cls, measured, nominal):
        """Keep the unit of a number typed without a suffix at the time of entry."""
        target = cls.VALUE.fullmatch(nominal.replace("−", "-"))
        if target and re.fullmatch(cls.NUMBER, cls.clean(measured).strip()):
            try:
                cls.normalize(target[2])
            except ValueError:
                return measured
            return f"{measured.strip()} {target[2]}"
        return measured

    @classmethod
    def format_measurement(cls, measured, nominal):
        target = cls.VALUE.fullmatch(nominal.replace("−", "-"))
        if not target or not measured.strip():
            return measured
        source = cls.VALUE.fullmatch(cls.clean(cls.with_nominal_unit(measured, nominal)))
        if not source:
            return measured
        try:
            target_base, target_scale = cls.split(target[2])
            source_base, source_scale = cls.split(source[2])
            if source_base != target_base:
                return measured
            if source_scale == target_scale and "e" not in source[1].lower():
                return f"{source[1]} {target[2]}"
            value = Decimal(source[1].replace(",", ""))
            # All SI prefixes here are powers of ten. Shift the decimal exponent
            # directly to avoid rounding the meter's original digit sequence.
            shift = Decimal(str(source_scale)).adjusted() - Decimal(str(target_scale)).adjusted()
            sign, digits, exponent = value.as_tuple()
            value = Decimal((sign, digits, exponent + shift))
            notation = "f" if -12 <= value.adjusted() <= 15 else "E"
            display = format(value, notation)
            return f"{display} {target[2]}"
        except (ValueError, DecimalException):
            return measured


class Fluke5560ACommandBuilder:
    """Fluke 5560A Remote Programmers Manual 규격에 맞춘 SCPI/원격 명령 생성기"""

    @staticmethod
    def normalize_unit(unit):
        return SIUnits.normalize(unit)

    @classmethod
    def unit_multiplier(cls, unit):
        return SIUnits.split(unit)[1]

    @staticmethod
    def decode_signed_pf(pf_raw_val: float) -> Tuple[float, str, str]:
        """
        사용자 정의 역률 부호 규칙 해석:
        - 1 (또는 1.0)  -> (1.0, "LEAD", "1 (1.0 LEAD)")
        - -0.5 (음수)   -> (0.5, "LEAD", "-0.5 (0.5 LEAD)")
        - 0.5  (양수)   -> (0.5, "LAG",  "0.5 (0.5 LAG)")
        """
        if abs(pf_raw_val) > 1.0 or pf_raw_val == 0.0:
            raise ValueError(f"역률({pf_raw_val})은 0이 아닌 -1.0 ~ +1.0 범위여야 합니다.")
        if abs(abs(pf_raw_val) - 1.0) < 1e-6:
            return 1.0, "LEAD", f"{pf_raw_val:g} (1.0 LEAD)"
        if pf_raw_val < 0:
            dpf = abs(pf_raw_val)
            return dpf, "LEAD", f"{pf_raw_val:g} ({dpf:g} LEAD)"
        dpf = pf_raw_val
        return dpf, "LAG", f"{pf_raw_val:g} ({dpf:g} LAG)"

    @classmethod
    def build_voltage_cmd(
        cls,
        mode: str,
        value: float,
        unit: str = "V",
        freq_hz: float = 0.0,
    ) -> Tuple[str, float]:
        norm_unit = cls.normalize_unit(unit)
        volts = SIUnits.to_base(value, norm_unit, "V")
        if abs(volts) > MAX_VOLTAGE_V:
            raise ValueError(
                f"설정 전압({volts:.4g} V)이 Fluke 5560A 최대 허용 한계(±{MAX_VOLTAGE_V:g} V)를 초과합니다."
            )

        is_ac = mode.upper() == "ACV"
        if is_ac:
            if volts <= 0:
                raise ValueError("AC 전압은 0보다 큰 양수여야 합니다.")
            if freq_hz <= 0:
                raise ValueError("AC 전압 모드에서는 주파수(Hz)가 0보다 커야 합니다.")
            cmd = f"OUT {volts:.12g} V, {freq_hz:.6g} HZ"
        else:
            cmd = f"OUT {volts:.12g} V, 0 HZ"

        return cmd, volts

    @classmethod
    def calculate_turn_coil_current(
        cls,
        target_current_a: float,
        coil_turns: float,
        use_coil: bool = True,
    ) -> Tuple[float, float]:
        if not use_coil:
            coil_turns = 1.0
        if coil_turns <= 0:
            raise ValueError("턴코일 배율(Turn Ratio)은 0보다 큰 값이어야 합니다.")

        actual_output_a = target_current_a / coil_turns
        effective_clamp_a = actual_output_a * coil_turns

        if abs(actual_output_a) > MAX_CURRENT_A:
            max_effective = MAX_CURRENT_A * coil_turns
            raise ValueError(
                f"5560A 실제 출력 전류({actual_output_a:.4f} A)가 장비 연속 최대 출력 한계({MAX_CURRENT_A:g} A)를 초과합니다.\n"
                f"현재 {coil_turns:g}턴 코일 기준 최대 설정 가능 전류는 {max_effective:g} A 입니다."
            )
        return actual_output_a, effective_clamp_a

    @classmethod
    def build_current_cmd(
        cls,
        mode: str,
        input_value: float,
        unit: str = "A",
        freq_hz: float = 0.0,
        use_coil: bool = False,
        coil_turns: float = 1.0,
        current_post: str = "AUTO",
    ) -> Tuple[str, float, float]:
        norm_unit = cls.normalize_unit(unit)
        target_a = SIUnits.to_base(input_value, norm_unit, "A")
        actual_a, effective_a = cls.calculate_turn_coil_current(
            target_current_a=target_a,
            coil_turns=coil_turns,
            use_coil=use_coil,
        )

        is_ac = mode.upper() == "ACI"
        if is_ac:
            if actual_a <= 0:
                raise ValueError("AC 전류는 0보다 큰 양수여야 합니다.")
            if freq_hz <= 0:
                raise ValueError("AC 전류 모드에서는 주파수(Hz)가 0보다 커야 합니다.")
            out_cmd = f"OUT {actual_a:.8g} A, {freq_hz:.6g} HZ"
        else:
            out_cmd = f"OUT {actual_a:.8g} A, 0 HZ"

        cmds = [out_cmd]
        if current_post.upper() in ("AUX", "A20"):
            cmds.append(f"CUR_POST {current_post.upper()}")

        return "; ".join(cmds), actual_a, effective_a

    @classmethod
    def build_resistance_cmd(
        cls,
        value: float,
        unit: str = "OHM",
        zcomp: str = "NONE",
    ) -> Tuple[str, float]:
        norm_unit = cls.normalize_unit(unit)
        ohms = SIUnits.to_base(value, norm_unit, "OHM")
        if ohms < 0 or ohms > MAX_RESISTANCE_OHM:
            raise ValueError(
                f"설정 저항({ohms:.4g} Ω)이 허용 범위(0 Ω ~ {MAX_RESISTANCE_OHM/1e6:g} MΩ)를 벗어났습니다."
            )
        zcomp_clean = zcomp.strip().upper()
        if zcomp_clean not in ("NONE", "WIRE2", "WIRE4"):
            raise ValueError(f"유효하지 않은 ZCOMP 모드입니다: {zcomp}")

        cmd = f"OUT {ohms:.12g} OHM; ZCOMP {zcomp_clean}"
        return cmd, ohms

    @classmethod
    def build_power_cmd(
        cls,
        mode: str,
        calc_method: str,
        voltage_v: float,
        current_or_power_val: float,
        power_unit: str = "W",
        freq_hz: float = 60.0,
        phase_mode: str = "DPF",
        dpf_val: float = 1.0,
        dpf_lead_lag: str = "LEAD",
        phase_deg: float = 0.0,
        use_coil: bool = False,
        coil_turns: float = 1.0,
    ) -> Dict[str, object]:
        is_ac = mode.upper() == "ACP"
        if abs(voltage_v) <= 0 or abs(voltage_v) > MAX_VOLTAGE_V:
            raise ValueError(f"기준 전압은 0보다 크고 {MAX_VOLTAGE_V:g} V 이하여야 합니다.")

        if not is_ac:
            pf = 1.0
            resolved_phase_deg = 0.0
        else:
            if freq_hz <= 0:
                raise ValueError("AC 전력 모드에서는 주파수(Hz)가 0보다 커야 합니다.")
            if phase_mode.upper() == "DPF":
                if not (0.0 < dpf_val <= 1.0):
                    raise ValueError("역률(DPF)은 0.0 초과 1.0 이하 범위여야 합니다.")
                pf = dpf_val
                angle_rad = math.acos(pf)
                deg_mag = math.degrees(angle_rad)
                resolved_phase_deg = deg_mag if dpf_lead_lag.upper() == "LEAD" else -deg_mag
            else:
                if not (-180.0 <= phase_deg <= 180.0):
                    raise ValueError("위상각(PHASE)은 -180.0° ~ +180.0° 범위여야 합니다.")
                resolved_phase_deg = phase_deg
                pf = math.cos(math.radians(resolved_phase_deg))

        if calc_method.upper() == "PW":
            target_power_w = SIUnits.to_base(current_or_power_val, power_unit, "W")
            if abs(pf) < 1e-6:
                raise ValueError("역률(cos θ)이 0일 때는 유효전력(W) 기준으로 전류를 역산할 수 없습니다.")
            target_effective_current_a = target_power_w / (voltage_v * pf)
        else:
            target_effective_current_a = current_or_power_val

        actual_current_a, effective_current_a = cls.calculate_turn_coil_current(
            target_current_a=target_effective_current_a,
            coil_turns=coil_turns,
            use_coil=use_coil,
        )

        apparent_power_va = abs(voltage_v * effective_current_a)
        active_power_w = voltage_v * effective_current_a * pf
        reactive_power_var = (
            apparent_power_va * math.sin(math.radians(abs(resolved_phase_deg)))
            if is_ac else 0.0
        )

        if not is_ac:
            cmd = f"OUT {voltage_v:.8g} V, {actual_current_a:.8g} A, 0 HZ"
        else:
            cmds = [f"OUT {voltage_v:.8g} V, {actual_current_a:.8g} A, {freq_hz:.6g} HZ"]
            if phase_mode.upper() == "DPF":
                lead_lag_token = "LEAD" if dpf_lead_lag.upper() == "LEAD" else "LAG"
                cmds.append(f"DPF {dpf_val:.6g}, {lead_lag_token}")
            else:
                cmds.append(f"PHASE {resolved_phase_deg:+.3f}")
            cmd = "; ".join(cmds)

        return {
            "cmd": cmd,
            "voltage_v": voltage_v,
            "actual_current_a": actual_current_a,
            "effective_current_a": effective_current_a,
            "active_power_w": active_power_w,
            "apparent_power_va": apparent_power_va,
            "reactive_power_var": reactive_power_var,
            "pf": pf,
            "phase_deg": resolved_phase_deg,
        }


# =====================================================================
# 3. 엑셀/성적서 복사·붙여넣기 스마트 파서 (띄어쓰기 / 다중 탭 완벽 호환)
# =====================================================================
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
            _, _, display = cls.parse_power_factor(nominal)
            return vi_condition, frequency, display, display
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
        _, _, display = cls.parse_power_factor(tail)
        return (f"{quantities['V']} / {quantities['A']}", quantities['HZ'], display, display)

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
                _, _, display = cls.parse_power_factor(pf_str)
                return vi_cond_str, freq_str, display, display
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

        for raw_line in raw_text.splitlines():
            parsed_fields = cls.parse_line_to_fields(raw_line)
            if parsed_fields is None:
                continue
            vi_c, fq_c, pf_c, nom_c = parsed_fields
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


# =====================================================================
# 4. 실제 랜카드 조회 및 USB/GPIB/LAN 통신 엔진
# =====================================================================
@dataclass(frozen=True)
class CalibratorProfile:
    model: str
    interfaces: tuple
    max_current: float
    max_resistance: float = 1.1e9

    @property
    def terminal_note(self):
        return ("VI AUX ≤3.1 A / 30A >3.1 A" if self.model == "5560A"
                else "AUX <3 A / 20A ≥3 A (고전류 출력 시간 제한은 본체 설명서 참조)")

    def high_current(self, amps):
        return abs(amps) > 3.1 if self.model == "5560A" else abs(amps) >= 3.0

    def lcomp_allowed(self, amps, hz):
        if self.model == "5560A" or hz <= 0:
            return False
        return hz < (65 if self.model == "5080A" else (440 if abs(amps) < .33 else 1000))

    @staticmethod
    def output_values(command):
        """Parse the V/A/Hz/ohm OUT subset used by this application, in SI units."""
        values = {}
        scales = {"UV": ("V", 1e-6), "MV": ("V", .001), "V": ("V", 1), "KV": ("V", 1000),
                  "UA": ("A", 1e-6), "MA": ("A", .001), "A": ("A", 1),
                  "HZ": ("HZ", 1), "KHZ": ("HZ", 1000), "MHZ": ("HZ", 1e6),
                  "OHM": ("OHM", 1), "KOHM": ("OHM", 1e3),
                  "MOHM": ("OHM", 1e6), "GOHM": ("OHM", 1e9)}
        for arg in command.split(None, 1)[1].split(","):
            m = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)\s*([A-Z]+)\s*", arg, re.I)
            if not m or m[2].upper() not in scales:
                raise ValueError("OUT은 전압(V), 전류(A), 주파수(HZ), 저항(OHM)과 단위를 명시하세요.")
            unit, scale = scales[m[2].upper()]
            value = float(m[1]) * scale
            if not math.isfinite(value) or unit in values:
                raise ValueError("OUT의 수치 또는 중복 단위를 확인하세요.")
            values[unit] = value
        if "OHM" in values:
            if len(values) != 1:
                raise ValueError("저항 OUT에는 다른 출력값을 함께 지정할 수 없습니다.")
        elif not (set(values) in ({"V", "HZ"}, {"A", "HZ"}, {"V", "A", "HZ"}, {"V"}, {"A"}, {"V", "A"}, {"HZ"})):
            raise ValueError("지원하는 OUT 조합은 전압·전류·주파수 또는 저항입니다.")
        return values

    def validate_command(self, command):
        parts = [p.strip().upper() for p in command.split(";") if p.strip()]
        out_parts = [p for p in parts if re.match(r"^OUT(?:\s|$)", p)]
        if len(out_parts) > 1:
            raise ValueError("한 번에 OUT 한 개만 전송하세요.")
        values = self.output_values(out_parts[0]) if out_parts else {}
        if abs(values.get("V", 0)) > 1020:
            raise ValueError(f"{self.model}: 최대 전압 1020 V를 초과합니다.")
        if abs(values.get("A", 0)) > self.max_current:
            raise ValueError(f"{self.model}: 코일 환산 후 실제 전류가 최대 {self.max_current:g} A를 초과합니다.")
        if values.get("HZ", 0) < 0:
            raise ValueError("주파수는 0 이상이어야 합니다.")
        if values.get("HZ", 0) > 0 and any(values.get(u, 1) <= 0 for u in ("V", "A")):
            raise ValueError("AC 전압·전류는 양수여야 합니다. 위상은 PHASE/DPF로 설정하세요.")
        ohms = values.get("OHM")
        if ohms is not None:
            if not 0 <= ohms <= self.max_resistance:
                raise ValueError(f"{self.model}: 저항 범위 0~{self.max_resistance / 1e6:g} MΩ를 벗어났습니다.")
            if self.model == "5080A" and not any(math.isclose(ohms, r, rel_tol=1e-10, abs_tol=1e-12) for r in RESISTORS_5080A):
                raise ValueError("5080A는 고정 저항만 출력합니다: 0, 1/1.9/10/19/100/190 Ω, "
                                 "1/1.9/10/19/100/190 kΩ, 1/1.9/10/19/100/190 MΩ. 자동 반올림하지 않습니다.")
        for part in parts:
            if part.startswith("CUR_POST "):
                post = part.split()[-1]
                if self.model == "5560A":
                    raise ValueError("5560A의 단자는 전류에 따라 자동 선택됩니다. CUR_POST 설정은 지원하지 않습니다.")
                if post not in ("AUX", "A20"):
                    raise ValueError("전류 단자는 AUX 또는 A20이어야 합니다.")
                if post == "AUX" and self.high_current(values.get("A", 0)):
                    raise ValueError(f"{self.model}: 3 A 이상은 A20 단자를 사용해야 합니다.")
            if part.startswith("LCOMP "):
                if self.model == "5560A":
                    raise ValueError("5560A는 단독 턴코일용 LCOMP 명령을 지원하지 않습니다.")
                if part not in ("LCOMP ON", "LCOMP OFF"):
                    raise ValueError("LCOMP는 ON/OFF를 사용하세요.")
                if part == "LCOMP ON" and values and not self.lcomp_allowed(values.get("A", 0), values.get("HZ", 0)):
                    raise ValueError(f"{self.model}: 이 전류·주파수에서는 LCOMP ON을 지원하지 않습니다.")
            if part.startswith("ZCOMP ") and ohms is not None and part != "ZCOMP NONE":
                limit = 1e6 if self.model == "5080A" else (110000 if self.model != "5560A" else None)
                if limit is not None and ohms >= limit:
                    raise ValueError(f"{self.model}: 이 저항값에서는 ZCOMP NONE을 사용하세요.")
        return command

    def prepare_generated(self, command, use_coil=False):
        parts = [p.strip() for p in command.split(";")]
        out = next((p for p in parts if p.upper().startswith("OUT ")), None)
        if out is None:
            return self.validate_command(command)
        values = self.output_values(out)
        if "A" in values and self.model != "5560A":
            if not any(p.upper().startswith("CUR_POST ") for p in parts):
                parts.append("CUR_POST A20" if self.high_current(values["A"]) else "CUR_POST AUX")
            if values.get("HZ", 0) > 0:
                enabled = use_coil and self.lcomp_allowed(values["A"], values["HZ"])
                parts.append("LCOMP ON" if enabled else "LCOMP OFF")
        if "OHM" in values:
            limit = 1e6 if self.model == "5080A" else (110000 if self.model != "5560A" else None)
            if limit is not None and values["OHM"] >= limit:
                parts = ["ZCOMP NONE" if p.upper().startswith("ZCOMP ") else p for p in parts]
        return self.validate_command("; ".join(parts))


RESISTORS_5080A = (0,) + tuple(v * scale for scale in (1, 1000, 1000000) for v in (1, 1.9, 10, 19, 100, 190))
MODEL_PROFILES = {
    "5560A": CalibratorProfile("5560A", ("LAN", "USB", "RS232", "GPIB"), 30),
    "5522A": CalibratorProfile("5522A", ("RS232", "GPIB"), 20.5),
    "5520A": CalibratorProfile("5520A", ("RS232", "GPIB"), 20.5),
    "5080A": CalibratorProfile("5080A", ("LAN", "RS232"), 20.5, 190e6),
}


class CalibratorCommandBuilder:
    """Model-specific facade; preserve original builders for external callers."""
    def __init__(self, model="5560A"):
        self.profile = MODEL_PROFILES[model]

    def _build(self, method, *args, **kwargs):
        # UI/parser pass keyword arguments, so coil intent remains explicit.
        result = getattr(Fluke5560ACommandBuilder, method)(*args, **kwargs)
        coil = kwargs.get("use_coil", False)
        if isinstance(result, dict):
            result = dict(result)
            result["cmd"] = self.profile.prepare_generated(result["cmd"], coil)
            return result
        return (self.profile.prepare_generated(result[0], coil),) + result[1:]

    def build_voltage_cmd(self, **kwargs):
        return self._build("build_voltage_cmd", **kwargs)

    def build_current_cmd(self, **kwargs):
        return self._build("build_current_cmd", **kwargs)

    def build_resistance_cmd(self, **kwargs):
        return self._build("build_resistance_cmd", **kwargs)

    def build_power_cmd(self, **kwargs):
        return self._build("build_power_cmd", **kwargs)


class SequenceCurrentFilter:
    """Classify the current actually encoded in OUT, after coil conversion."""
    JOB = "작업 기준: 3 A 이상"
    POSTS = "선택 모델의 실제 단자 기준"
    CURRENT_FUNCTIONS = {"DCI", "ACI", "DCP", "ACP", "PF"}
    CURRENT_TOKEN = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)\s*(UA|MA|A)\s*$", re.I)

    @classmethod
    def actual_current(cls, step):
        if step.func_type not in cls.CURRENT_FUNCTIONS:
            return None
        values = []
        for part in step.scpi_cmd.split(";"):
            match = re.match(r"^\s*OUT\s+(.+)$", part, re.I)
            if not match:
                continue
            for argument in match[1].split(","):
                current = cls.CURRENT_TOKEN.fullmatch(argument.strip())
                if current:
                    value = float(current[1]) * {"UA": 1e-6, "MA": 1e-3, "A": 1}[current[2].upper()]
                    if not math.isfinite(value):
                        raise ValueError(f"#{step.step_no}행의 출력 전류가 유효하지 않습니다.")
                    values.append(abs(value))
        if len(values) != 1:
            raise ValueError(f"#{step.step_no}행의 실제 출력 전류를 판별할 수 없습니다: {step.scpi_cmd}")
        return values[0]

    @classmethod
    def includes(cls, step, high_only=False, basis=JOB, model="5560A"):
        if basis not in (cls.JOB, cls.POSTS):
            raise ValueError("전류 그룹 기준을 확인하세요.")
        current = cls.actual_current(step)
        if current is None:
            # Voltage/resistance run once in the normal group, never in high-only.
            return not high_only
        high = current >= 3.0 if basis == cls.JOB else MODEL_PROFILES[model].high_current(current)
        return high == high_only

    @classmethod
    def label(cls, high_only, basis, model="5560A"):
        if basis == cls.JOB:
            return "3 A 이상 전류·전력만" if high_only else "3 A 미만 전류·전력 + 전압·저항"
        if model == "5560A":
            return "3.1 A 초과 전류·전력만" if high_only else "3.1 A 이하 전류·전력 + 전압·저항"
        return "3 A 이상 전류·전력만 (20A)" if high_only else "3 A 미만 전류·전력 + 전압·저항 (AUX)"


class AutoNetworkAligner:
    """Read-only network inventory. Never infer a physical adapter from its name."""

    _cache_time = 0.0
    _cache = []
    last_error = ""

    @staticmethod
    def normalize_ipv4(ip_str: str) -> str:
        value = ip_str.strip()
        if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", value):
            raise ValueError("5560A 화면의 IPv4 주소를 입력하세요. 예: 192.168.100.101")
        value = ".".join(str(int(part)) for part in value.split("."))
        addr = ipaddress.IPv4Address(value)
        if addr.is_unspecified or addr.is_multicast or int(addr) == 0xffffffff:
            raise ValueError("0.0.0.0 또는 브로드캐스트/멀티캐스트 주소에는 연결할 수 없습니다.")
        return str(addr)

    @classmethod
    def list_ethernet_interfaces(cls, refresh=False):
        if not refresh and cls._cache and time.monotonic() - cls._cache_time < 5:
            return cls._cache
        cls.last_error = ""
        adapters = []
        if sys.platform == "win32":
            # JSON and explicit UTF-8 avoid localized netsh headings and CP949 issues.
            script = r"""
            $ErrorActionPreference = 'Stop'
            [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
            $allIPs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)
            $rows = @(Get-NetAdapter -Physical -ErrorAction Stop | ForEach-Object {
                $nic = $_
                $ips = @($allIPs | Where-Object { $_.InterfaceIndex -eq $nic.ifIndex -and $_.AddressState -eq 'Preferred' } | ForEach-Object {
                    @{ ip = $_.IPAddress; prefix = [int]$_.PrefixLength }
                })
                @{ idx = [string]$nic.ifIndex; name = $nic.Name; state = [string]$nic.Status;
                   description = $nic.InterfaceDescription; wired = ($nic.NdisPhysicalMedium -eq 14);
                   hardware = [bool]$nic.HardwareInterface; addresses = $ips }
            })
            ConvertTo-Json -InputObject $rows -Depth 5 -Compress
            """
            try:
                proc = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode:
                    raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip())
                rows = json.loads(proc.stdout.decode("utf-8-sig")) or []
                if isinstance(rows, dict):
                    rows = [rows]
                adapters = [row for row in rows if row.get("hardware")]
                adapters.sort(key=lambda row: (row["state"] != "Up", not row["wired"], int(row["idx"])))
            except Exception as exc:
                cls.last_error = f"랜카드 조회 실패: {exc}"
        else:
            cls.last_error = "랜카드 상세 조회는 Windows에서 지원합니다. LAN 원본 IP는 직접 지정할 수 있습니다."
        cls._cache = adapters
        cls._cache_time = time.monotonic()
        return adapters

    @classmethod
    def select_source_ip(cls, adapter_idx: str, target_ip: str) -> str:
        target = ipaddress.IPv4Address(cls.normalize_ipv4(target_ip))
        if not adapter_idx:
            if not target.is_link_local:
                return ""
            # Link-local instrument traffic belongs on the attached cable.
            wired = [adapter for adapter in cls.list_ethernet_interfaces()
                     if adapter.get("wired") and adapter["state"] == "Up" and adapter.get("addresses")]
            for adapter in wired:
                for item in adapter["addresses"]:
                    network = ipaddress.IPv4Interface(f"{item['ip']}/{item['prefix']}").network
                    if target in network and str(target) != item["ip"] and target not in (
                            network.network_address, network.broadcast_address):
                        return item["ip"]
            if len(wired) == 1:
                return wired[0]["addresses"][0]["ip"]
            return ""
        for adapter in cls.list_ethernet_interfaces(refresh=True):
            if adapter["idx"] != str(adapter_idx):
                continue
            if adapter["state"] != "Up":
                raise ConnectionError(f"{adapter['name']}: 케이블 연결 상태가 Up이 아닙니다.")
            addresses = adapter.get("addresses") or []
            if not addresses:
                raise ConnectionError(f"{adapter['name']}: 사용 가능한 IPv4 주소가 없습니다.")
            for item in addresses:
                network = ipaddress.IPv4Interface(f"{item['ip']}/{item['prefix']}").network
                if target in network:
                    if str(target) == item['ip'] or target in (network.network_address, network.broadcast_address):
                        raise ValueError("대상 주소가 PC 자체 주소 또는 해당 서브넷의 네트워크/브로드캐스트 주소입니다.")
                    return item["ip"]
            # Routed networks are legitimate. Report the mismatch, but do not force /24.
            return addresses[0]["ip"]
        raise ConnectionError("선택한 실제 랜카드를 찾을 수 없습니다. 랜카드 새로고침을 눌러주세요.")

    @classmethod
    def link_local_source_ips(cls, source_ip, target_ip, adapter_idx):
        """Offer alternate addresses only on the adapter selected for link-local LAN."""
        if not source_ip or not ipaddress.IPv4Address(target_ip).is_link_local:
            return (source_ip,)
        for adapter in cls.list_ethernet_interfaces():
            if adapter["state"] != "Up":
                continue
            if adapter_idx and adapter["idx"] != str(adapter_idx):
                continue
            if not adapter_idx and not adapter.get("wired"):
                continue
            ips = [item["ip"] for item in adapter.get("addresses", ())]
            if source_ip in ips:
                return (source_ip, *(ip for ip in ips if ip not in (source_ip, target_ip)))
        return (source_ip,)

    @classmethod
    def describe(cls, target_ip="", source_ip=""):
        lines = []
        target = None
        if target_ip:
            target = ipaddress.IPv4Address(cls.normalize_ipv4(target_ip))
        for adapter in cls.list_ethernet_interfaces():
            addresses = adapter.get("addresses") or []
            ips = ", ".join(f"{a['ip']}/{a['prefix']}" for a in addresses) or "사용 가능한 IPv4 없음"
            lines.append(f"{adapter['idx']}: {adapter['name']} [{adapter['state']}] {ips}")
            for item in addresses:
                if item['ip'] == source_ip and target:
                    network = ipaddress.IPv4Interface(f"{item['ip']}/{item['prefix']}").network
                    if target not in network:
                        lines.append("  대상과 PC가 다른 서브넷입니다. 직결이라면 양쪽 IP/마스크를 맞춰야 합니다. 사내망이라면 라우팅도 확인하세요.")
        if cls.last_error:
            lines.append(cls.last_error)
        return "\n".join(lines) or "랜카드 정보 없음"


class InstrumentProtocolError(ConnectionError):
    pass


class OperationCancelled(RuntimeError):
    pass


class InstrumentCommandError(RuntimeError):
    """The instrument replied with an error; this is not a TCP failure."""
    pass


def close_lan_socket(sock):
    """Send FIN, consume final bytes briefly, then release the single-client session."""
    try:
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            if not sock.recv(4096):
                break
    except OSError:
        pass
    finally:
        sock.close()


class TelnetLineTransport:
    """Incremental Telnet parser + CR/LF framing; one transaction at a time."""

    PROMPT = re.compile(r"^(?:(?:5[0-9]{3}A)?\s*[=!?>]>?\s*)", re.I)

    def __init__(self, sock, timeout=3.0, eol="CRLF"):
        self.sock = sock
        self.timeout = timeout
        self.terminator = {"CRLF": b"\r\n", "CR": b"\r", "LF": b"\n"}[eol]
        self.buffer = bytearray()
        self.iac_state = "data"
        self.iac_verb = 0
        self.last_received = ""
        self._recent_writes = []

    def _feed(self, raw):
        for byte in raw:
            state = self.iac_state
            if state == "data":
                if byte == 255:
                    self.iac_state = "iac"
                elif byte != 0:  # Telnet CR-NUL padding
                    self.buffer.append(byte)
            elif state == "iac":
                if byte == 255:
                    self.buffer.append(byte)
                    self.iac_state = "data"
                elif byte in (251, 252, 253, 254):
                    self.iac_verb = byte
                    self.iac_state = "option"
                elif byte == 250:
                    self.iac_state = "sub"
                else:
                    self.iac_state = "data"
            elif state == "option":
                if self.iac_verb == 251:  # WILL -> DONT; WONT/DONT need no reply
                    self.sock.sendall(bytes((255, 254, byte)))
                elif self.iac_verb == 253:  # DO -> WONT
                    self.sock.sendall(bytes((255, 252, byte)))
                self.iac_state = "data"
            elif state == "sub":
                if byte == 255:
                    self.iac_state = "sub_iac"
            elif state == "sub_iac":
                self.iac_state = "data" if byte == 240 else "sub"
        if len(self.buffer) > 65536:
            raise InstrumentProtocolError("응답이 64 KiB를 초과했습니다. 대상 IP와 장비 종류를 확인하세요.")

    @classmethod
    def clean_line(cls, line, command):
        text = line.decode("ascii", errors="replace").strip()
        if re.match(r"^[!?]>", text):
            raise InstrumentProtocolError(f"장비 오류 응답: {text}")
        text = cls.PROMPT.sub("", text).strip()
        return "" if text.upper() == command.upper() else text

    def query(self, command, identity=False, timeout=None, cancel_event=None, progress=None):
        limit = self.timeout if timeout is None else timeout
        # One query, one reply. Never retransmit OPC while a reply is pending.
        self.write(command)
        started = time.monotonic()
        deadline = started + limit
        next_progress = started + 1
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("사용자가 현재 응답 대기를 중단했습니다.")
            while True:
                match = re.search(rb"[\r\n]", self.buffer)
                if match is None:
                    break
                line = bytes(self.buffer[:match.start()])
                del self.buffer[:match.end()]
                text = self.clean_line(line, command)
                if not text or text.upper() in self._recent_writes:
                    continue
                self.last_received = text
                # Initial banners/prompts are not identification responses.
                if identity and len(text.split(",")) < 4:
                    continue
                return text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                partial = bytes(self.buffer[-120:]).decode("ascii", errors="replace")
                raise TimeoutError(f"{command}: {limit:g}초 동안 완전한 응답 없음 (부분 수신: {partial or '없음'}).")
            if progress and time.monotonic() >= next_progress:
                progress(time.monotonic() - started, limit)
                next_progress = time.monotonic() + 1
            self.sock.settimeout(min(0.2, remaining))
            try:
                raw = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not raw:
                raise ConnectionError("장비가 TCP 연결을 종료했습니다.")
            self._feed(raw)

    def write(self, command):
        self.sock.settimeout(self.timeout)
        self.sock.sendall(command.encode("ascii") + self.terminator)
        self._recent_writes.append(command.upper())
        self._recent_writes = self._recent_writes[-16:]


PYSERIAL_IMPORT_ERROR = ""
try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError as exc:
    serial = None
    HAS_PYSERIAL = False
    PYSERIAL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class SerialEndpoint:
    """Adapt pyserial to the cancellable line reader; an empty read is a timeout."""
    def __init__(self, port):
        self.port = port

    def settimeout(self, timeout):
        self.port.timeout = timeout
        self.port.write_timeout = timeout

    def recv(self, size):
        if not self.port.is_open:
            raise ConnectionError("COM 포트가 닫혔습니다. USB–RS-232 연결을 확인하세요.")
        data = self.port.read(min(size, max(1, self.port.in_waiting)))
        if not data:
            raise socket.timeout()
        return data

    def sendall(self, data):
        sent = self.port.write(data)
        if sent != len(data):
            raise TimeoutError("RS-232 명령 일부만 전송되었습니다. 재전송하지 않고 연결을 종료합니다.")


class SerialLineTransport(TelnetLineTransport):
    def _feed(self, raw):
        # RS-232 is not Telnet. Never send IAC negotiation bytes to a serial port.
        self.buffer.extend(raw)
        if len(self.buffer) > 65536:
            raise InstrumentProtocolError("RS-232 응답이 64 KiB를 초과했습니다.")


# DUT acquisition owns its own connection; linked sequences request fresh samples.
# Command references and supported families: outputs/DUT_연결_안내.md.
DUT_METRICS = (
    ("voltage", "전압 · RMS", "V"), ("current", "전류 · RMS", "A"),
    ("power", "유효전력", "W"), ("pf", "역률", ""),
    ("apparent", "피상전력", "VA"), ("reactive", "무효전력", "var"),
    ("frequency", "주파수", "Hz"),
)


@dataclass(frozen=True)
class DUTProfile:
    family: str
    model: str
    channels: int
    wt3000_numeric: bool = False


def identify_dut(identity):
    parts = [part.strip().strip('"').upper() for part in identity.split(',')]
    if len(parts) < 2:
        raise ValueError(f"DUT 식별 응답을 확인할 수 없습니다: {identity[:160]}")
    maker = re.sub(r"[^A-Z0-9]", "", parts[0])
    model = re.sub(r"[\s-]", "", parts[1])
    if maker in ("NEWTONS4TH", "NEWTONS4THLTD", "N4L"):
        number = model.removeprefix("PPA")
        # These families document the same POWER/RMS and POWER/WATTS records.
        if re.fullmatch(r"(?:5\d{2}|(?:15|25|26|35|45|55)\d{2})[A-Z]?", number):
            return DUTProfile("PPA", "PPA" + number, 6 if number.startswith("35") else 3)
    if maker.startswith("YOKOGAWA"):
        # WT3000(E) includes the option suffix in the model field of *IDN?.
        model_code = parts[1].split("-", 1)[0].strip()
        if re.fullmatch(r"76030[1-4]", model_code):
            return DUTProfile("WT", f"WT3000 ({model_code})", int(model_code[-1]), wt3000_numeric=True)
        if re.fullmatch(r"WT300[1-4]E", model_code):
            return DUTProfile("WT", model_code, int(model_code[-2]), wt3000_numeric=True)
        if model == "WT5000":
            return DUTProfile("WT", model, 7)
        if re.fullmatch(r"WT(?:310(?:HC|EH|E)?|332E?|333E?)", model):
            channels = 2 if model.startswith("WT332") else 3 if model.startswith("WT333") else 1
            return DUTProfile("WT", model, channels)
        if re.fullmatch(r"WT180[1-6]E?", model):
            return DUTProfile("WT", model, int(model[5]))
        if model in ("WT1800", "WT1800E"):
            return DUTProfile("WT", model, 6)
        if re.fullmatch(r"76020[1-3]", model):
            return DUTProfile("WT", "WT500 (" + model + ")", int(model[-1]))
        if model == "WT500":
            return DUTProfile("WT", model, 3)
    raise ValueError(f"아직 등록되지 않은 DUT입니다: {identity[:160]}\n"
                     "PPA 5xx/15xx/25xx/26xx/35xx/45xx/55xx, WT300/300E/500/1800/3000/3000E/5000 계열을 지원합니다. "
                     "다른 모델은 해당 통신 명령 확인 후 추가할 수 있습니다.")


def dut_number_fields(reply, count):
    # A bad response must never be silently shifted into another measurement.
    tokens = reply.strip().split(',')
    if len(tokens) != count:
        raise ValueError(f"DUT 응답 항목 수가 다릅니다 (예상 {count}, 수신 {len(tokens)}): {reply[:160]}")
    values = []
    for token in tokens:
        token = token.strip()
        if token.upper() in ("NAN", "INF", "+INF", "-INF", "OVLD", "OVER", "OVERLOAD", "---", "----"):
            values.append((None, token))
            continue
        try:
            value = float(token)
        except ValueError as exc:
            raise ValueError(f"DUT의 숫자 응답을 해석할 수 없습니다: {token[:80]}") from exc
        values.append((value if math.isfinite(value) and abs(value) < 9e36 else None, token))
    return values


def dut_numbers(reply, count):
    return [value for value, _ in dut_number_fields(reply, count)]


@dataclass(frozen=True)
class DUTSample:
    identity: str
    channel: int
    received_at: str
    values: dict
    note: str
    simulated: bool = False
    vi_modes: tuple[str, str] | None = None
    raw_values: dict[str, str] | None = None


@dataclass(frozen=True)
class DUTSettings:
    mode: str = "VISA"
    address: str = ""
    port: int = 10001
    timeout: float = 10.0
    eol: str = "CRLF"
    baudrate: int = 9600
    data_bits: int = 8
    parity: str = "N"
    stop_bits: int = 1
    flow: str = "NONE"

    def validate(self):
        if self.mode not in ("VISA", "WT_LAN", "WT5000_TCP", "PPA_LAN", "SERIAL", "DEMO"):
            raise ValueError("DUT 연결 방식을 선택하세요.")
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
            raise ValueError("DUT 응답 대기는 1~60초로 입력하세요.")
        if self.eol not in ("LF", "CRLF", "CR"):
            raise ValueError("줄 끝 문자를 확인하세요.")
        if self.mode == "DEMO":
            return
        if not self.address.strip() or any(c in self.address for c in '\r\n\x00'):
            raise ValueError("DUT 주소 또는 COM/VISA 리소스를 입력하세요.")
        if self.mode in ("WT_LAN", "WT5000_TCP", "PPA_LAN"):
            AutoNetworkAligner.normalize_ipv4(self.address)
        if self.mode == "PPA_LAN" and not 1 <= self.port <= 65535:
            raise ValueError("PPA 본체의 TCP 포트 번호를 입력하세요 (1~65535).")
        if self.mode == "SERIAL":
            if not re.fullmatch(r"COM[1-9]\d*", self.address, re.I):
                raise ValueError("COM 포트 형식으로 입력하세요. 예: COM5")
            if self.baudrate not in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
                raise ValueError("RS232 통신 속도를 확인하세요.")
            if self.data_bits not in (7, 8) or self.parity not in ("N", "E", "O") or self.stop_bits not in (1, 2) or self.flow not in ("NONE", "RTS/CTS", "XON/XOFF"):
                raise ValueError("RS232 통신 설정을 확인하세요.")
        if self.mode == "VISA" and not re.fullmatch(r"(?:USB|GPIB|TCPIP)\d*::[^\r\n]+::(?:INSTR|SOCKET)", self.address, re.I):
            raise ValueError("USB/GPIB/TCPIP VISA 리소스를 선택하세요. COM 포트는 RS232 방식을 사용하세요.")


class DUTTransport:
    """A bounded ASCII transport. No calibrator commands, reset, or retries."""
    def __init__(self, settings, cancel):
        self.settings, self.cancel = settings, cancel
        self.resource = self.manager = self.sock = self.serial_port = None
        self.buffer = bytearray()
        self.eol = "\n" if settings.mode == "WT5000_TCP" else {"LF": "\n", "CRLF": "\r\n", "CR": "\r"}[settings.eol]

    def _check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError("DUT 조회를 중지했습니다.")

    def open(self):
        s = self.settings
        self._check_cancel()
        try:
            if s.mode in ("VISA", "WT_LAN"):
                if not HAS_PYVISA:
                    raise RuntimeError(dependency_import_message("pyvisa", "PyVISA", PYVISA_IMPORT_ERROR))
                self.manager = SharedVisaManager()
                address = f"TCPIP0::{s.address}::inst0::INSTR" if s.mode == "WT_LAN" else s.address
                self.resource = self.manager.open_resource(address, open_timeout=int(s.timeout * 1000))
                self.resource.timeout = int(s.timeout * 1000)
                self.resource.write_termination = self.eol
                self.resource.read_termination = self.eol[-1]
                self.resource.encoding = "ascii"
            elif s.mode in ("PPA_LAN", "WT5000_TCP"):
                port = 10002 if s.mode == "WT5000_TCP" else s.port
                self.sock = socket.create_connection((s.address, port), timeout=s.timeout)
            elif s.mode == "SERIAL":
                if not HAS_PYSERIAL:
                    raise RuntimeError(dependency_import_message("pyserial", "pyserial", PYSERIAL_IMPORT_ERROR))
                self.serial_port = serial.Serial(s.address, baudrate=s.baudrate, bytesize=s.data_bits,
                    parity=s.parity, stopbits=s.stop_bits, timeout=.2, write_timeout=s.timeout,
                    rtscts=s.flow == "RTS/CTS", xonxoff=s.flow == "XON/XOFF")
            else:
                raise ValueError("지원하지 않는 실제 연결 방식입니다.")
            self._check_cancel()
        except Exception:
            self.close()
            raise

    def write(self, command):
        self._check_cancel()
        if '\n' in command or '\r' in command:
            raise ValueError("DUT 명령은 한 줄이어야 합니다.")
        if self.resource is not None:
            self.resource.write(command)
        else:
            data = (command + self.eol).encode('ascii')
            if self.sock is not None:
                self.sock.settimeout(self.settings.timeout)
                self.sock.sendall(data)
            elif self.serial_port is not None:
                if self.serial_port.write(data) != len(data):
                    raise IOError("DUT 명령 일부만 전송되었습니다.")
            else:
                raise ConnectionError("DUT가 연결되지 않았습니다.")

    def query(self, command):
        self.write(command)
        if self.resource is not None:
            reply = self.resource.read()
            self._check_cancel()
            if len(reply) > 65536:
                raise ValueError("DUT 응답이 너무 큽니다.")
            return reply.strip()
        deadline = time.monotonic() + self.settings.timeout
        while True:
            self._check_cancel()
            for index, byte in enumerate(self.buffer):
                if byte in (10, 13):
                    line = bytes(self.buffer[:index])
                    del self.buffer[:index + 1]
                    if line.strip():
                        return line.decode('ascii').strip()
                    break
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"DUT 응답 시간 초과: {command}")
                if self.sock is not None:
                    self.sock.settimeout(min(.2, remaining))
                    try:
                        data = self.sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        raise ConnectionError("DUT가 연결을 종료했습니다.")
                else:
                    data = self.serial_port.read(min(4096, max(1, self.serial_port.in_waiting)))
                self.buffer.extend(data)
                if len(self.buffer) > 65536:
                    raise ValueError("DUT 응답이 너무 큽니다.")

    def close(self):
        self.resource = None  # Its manager lease closes this session only.
        for name in ("sock", "serial_port", "manager"):
            obj = getattr(self, name)
            setattr(self, name, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.buffer.clear()


class DUTSession:
    def __init__(self, transport_factory=DUTTransport):
        self.transport_factory = transport_factory
        self.cancel = threading.Event()
        self.transport = self.profile = self.sample = None
        self.identity = ""
        self.simulated = False
        self.input_channels = ()

    @property
    def connected(self):
        return self.profile is not None

    @property
    def supports_fresh_capture(self):
        return self.connected and self.profile.family in ("WT", "PPA")

    @staticmethod
    def _wt_reply_value(reply):
        # Preserve whitespace in the payload, even when a response header is present.
        return re.sub(r"^:\S+\s+", "", reply.strip()).upper()

    def _set_wt_default(self, command, value, *accepted):
        self.transport.write(f"{command} {value}")
        raw = self.transport.query(f"{command}?")
        reply = self._wt_reply_value(raw)
        if reply not in (accepted or (value,)):
            raise ValueError(f"{command} {value} 설정 확인 실패: {raw[:100]}")

    def _set_wt_display(self, compact, channel):
        profile = self.profile
        if compact:
            prefix = ":DISPLAY:NORMAL"
        else:
            panel = ":DISPLAY:NUMERIC1" if profile.model == "WT5000" else ":DISPLAY:NUMERIC"
            prefix = panel + ":NORMAL:VAL4"
        functions = ("U", "I", "P", "LAMBDA") if compact or profile.wt3000_numeric else (
            "URMS", "IRMS", "P", "LAMBDA")
        for index, function in enumerate(functions, 1):
            command = f"{prefix}:ITEM{index}"
            item = f"{function},{channel}"
            short = item.replace("LAMBDA,", "LAMB,")
            self._set_wt_default(command, item, item, short, item + ",TOTAL", short + ",TOT",
                                 short + ",TOTAL", item + ",TOT")
        if not compact:
            self._set_wt_default(f"{panel}:NORMAL:FORMAT", "VAL4")
            self._set_wt_default(f"{prefix}:PAGE", "1")
            if profile.model == "WT5000":
                self._set_wt_default(":DISPLAY:CUSTOM:MODE", "OFF", "OFF", "0")
                mode_command = ":DISPLAY:CONFIG"
            else:
                mode_command = ":DISPLAY:MODE"
            self._set_wt_default(mode_command, "NUMERIC", "NUM", "NUME", "NUMERIC")

    def apply_measurement_defaults(self):
        """Apply only after the UI has verified the connected DUT's identity."""
        if self.transport is None:
            return ""
        t, profile = self.transport, self.profile
        if profile.family == "PPA":
            # CONFIG indices are the common voltage/current autorange controls.
            # This preserves the configured minimum ranges and input scaling.
            t.write("SPEED,FAST")
            t.write("FQREF,VOLTAGE")
            t.write("COUPLI,PHASE1,AC+DC")
            for index in (28, 29):
                t.write(f"CONFIG,{index},0")
            ref_index = 40 if profile.model.startswith(("PPA25", "PPA26")) else 38
            checks = (("CONFIG,13?", "3"), ("CONFIG,28?", "0"), ("CONFIG,29?", "0"),
                      (f"CONFIG,{ref_index}?", "0"), ("COUPLI,PHASE1?", "0"))
            for query, expected in checks:
                raw = t.query(query)
                if raw.strip() != expected:
                    raise ValueError(f"PPA 기본 설정 확인 실패: {query} / {raw[:100]}")
            return "FAST · 전압 기준 · V/I 자동 범위"

        self._set_wt_default(":COMMUNICATE:HEADER", "OFF", "0", "OFF")
        compact_wt = profile.model.startswith(("WT310", "WT332", "WT333"))
        channels = tuple(range(1, profile.channels + 1))
        rate_command, rate_ms = ":RATE", 500
        if compact_wt or profile.wt3000_numeric:
            rate_ms = 100 if compact_wt else 250
        if profile.model == "WT5000":
            self._set_wt_default(":UPDATE:MODE", "CONSTANT", "CONS", "CONSTANT")
            self._set_wt_default(":UPDATE:METHOD", "SSPERIOD", "SSP", "SSPERIOD")
            rate_command = ":UPDATE:RATE"
            channels = tuple(ch for ch in channels if self._wt_reply_value(
                t.query(f":SYSTEM:ELEMENT{ch}:MODEL?")).strip('"') != "NONE")
            if not channels:
                raise ValueError("WT5000에 장착된 입력 모듈을 찾을 수 없습니다.")
        t.write(f"{rate_command} {rate_ms}MS")
        raw = t.query(f"{rate_command}?")
        try:
            actual_rate = float(self._wt_reply_value(raw))
        except ValueError:
            actual_rate = None
        if actual_rate is None or not math.isclose(actual_rate, rate_ms / 1000):
            raise ValueError(f"갱신 주기 {rate_ms} ms 설정 확인 실패: {raw[:100]}")
        if compact_wt:
            self._set_wt_default(":INPUT:MODE", "RMS")
            self._set_wt_default(":INPUT:SYNCHRONIZE", "VOLTAGE", "VOLT", "VOLTAGE")
            for quantity in ("VOLTAGE", "CURRENT"):
                self._set_wt_default(f":INPUT:{quantity}:AUTO", "ON", "1", "ON")
        else:
            for channel in channels:
                element = f"ELEMENT{channel}"
                for quantity in ("VOLTAGE", "CURRENT"):
                    if profile.wt3000_numeric:
                        self._set_wt_default(f":INPUT:{quantity}:MODE:{element}", "RMS")
                    self._set_wt_default(f":INPUT:{quantity}:AUTO:{element}", "ON", "1", "ON")
                self._set_wt_default(f":INPUT:SYNCHRONIZE:{element}", f"U{channel}")
        self.input_channels = channels
        defaults = f"{rate_ms} ms · RMS · 전압 동기 · V/I 자동 범위"
        try:
            self._set_wt_display(compact_wt, channels[0])
        except ValueError as exc:
            return f"{defaults} · 본체 표시 확인 실패 ({exc})"
        return f"{defaults} · 본체 전압/전류/전력/역률"

    def connect(self, settings):
        settings.validate()
        self.disconnect()
        if self.cancel.is_set():
            raise InterruptedError("DUT 연결을 중지했습니다.")
        if settings.mode == "DEMO":
            self.profile = DUTProfile("DEMO", "가상 DUT", 3)
            self.identity = "가상 DUT · 실제 측정값 아님"
            self.simulated = True
            return self.identity
        try:
            self.transport = self.transport_factory(settings, self.cancel)
            self.transport.open()
            self.identity = self.transport.query("*IDN?")
            self.profile = identify_dut(self.identity)
            if settings.mode in ("PPA_LAN", "WT_LAN") and self.profile.family != settings.mode.split('_')[0]:
                raise ValueError("선택한 LAN 방식과 DUT 제조사가 다릅니다.")
            if settings.mode == "WT5000_TCP" and self.profile.model != "WT5000":
                raise ValueError("이 TCP 연결은 WT5000용입니다. 본체 Remote Control의 Command Type을 WT5000으로 확인하세요.")
            return self.identity
        except Exception:
            self.disconnect()
            raise

    def _wait_data_flag(self, command, mask, timeout_message):
        deadline = time.monotonic() + self.transport.settings.timeout
        while True:
            if self.cancel.is_set():
                raise InterruptedError("DUT 조회를 중지했습니다.")
            if time.monotonic() >= deadline:
                raise TimeoutError(timeout_message)
            reply = self.transport.query(command).strip()
            if not re.fullmatch(r"\+?[0-9]{1,10}", reply):
                raise ValueError(f"DUT 데이터 갱신 상태를 확인할 수 없습니다: {reply[:80]}")
            if (int(reply) & mask) == mask:
                return
            if self.cancel.wait(min(.1, max(0, deadline - time.monotonic()))):
                raise InterruptedError("DUT 조회를 중지했습니다.")

    def _require_wt_live_reading(self):
        t = self.transport
        hold = t.query(":HOLD?").strip().upper()
        if hold not in ("0", "OFF"):
            raise ValueError("WT 본체의 HOLD를 해제한 뒤 다시 실행하세요.")
        if self.profile.model.startswith(("WT310", "WT332", "WT333")):
            if t.query(":MEASURE:MHOLD?").strip().upper() not in ("0", "OFF"):
                raise ValueError("WT 본체의 MAX HOLD를 해제한 뒤 다시 실행하세요.")

    def _read_wt_fields(self, count, fresh):
        command = ":NUMERIC:NORMAL:VALUE?"
        if not fresh:
            return dut_number_fields(self.transport.query(command), count)
        t = self.transport
        self._require_wt_live_reading()
        raw_filter = t.query(":STATUS:FILTER1?")
        original_filter = self._wt_reply_value(raw_filter)
        if original_filter in {"NEV", "NEVE"}:
            original_filter = "NEVER"
        if original_filter not in {"RISE", "FALL", "BOTH", "NEVER"}:
            raise ValueError(f"WT 데이터 갱신 이벤트 설정을 확인할 수 없습니다: {raw_filter[:80]}")
        changed_filter = original_filter != "FALL"
        failed = True
        try:
            if changed_filter:
                t.write(":STATUS:FILTER1 FALL")
                if self._wt_reply_value(t.query(":STATUS:FILTER1?")) != "FALL":
                    raise ValueError("WT 데이터 갱신 이벤트 설정이 적용되지 않았습니다.")
            # EESR is cleared on read. Discard events before the stabilized
            # output, then wait through a complete post-output update interval.
            t.query(":STATUS:EESR?")
            for _ in range(2):
                self._wait_data_flag(":STATUS:EESR?", 1,
                    "WT 새 측정값 대기 시간 초과: 본체 HOLD·갱신 주기와 DUT 응답 대기 시간을 확인하세요.")
            self._require_wt_live_reading()
            fields = dut_number_fields(t.query(command), count)
            failed = False
            return fields
        finally:
            if changed_filter and not self.cancel.is_set():
                try:
                    t.write(f":STATUS:FILTER1 {original_filter}")
                except Exception:
                    if not failed:
                        raise

    def _ppa_range_overloaded(self, input_channel):
        reply = self.transport.query(f"STATUS,CH{input_channel}?").strip().upper().split(",")
        if len(reply) != 3 or reply[2].strip() not in {"OVER", "LOW", "OK"}:
            raise ValueError("PPA 입력 범위 상태를 확인할 수 없습니다.")
        return reply[2].strip() == "OVER"

    def read(self, channel=1, include_dc=False, fresh=False):
        self.sample = None
        if not self.connected:
            raise ConnectionError("DUT를 먼저 연결하세요.")
        if self.cancel.is_set():
            self.disconnect()
            raise InterruptedError("DUT 조회를 중지했습니다.")
        channels = self.input_channels or range(1, self.profile.channels + 1)
        if channel not in channels:
            raise ValueError(f"{self.profile.model}: 사용 가능한 입력 채널은 {', '.join(map(str, channels))}입니다.")
        try:
            vi_modes = None
            # Instrument record positions are independent of the app's display order.
            metrics = ("voltage", "current", "power", "apparent", "reactive", "pf", "frequency")
            if self.simulated:
                if fresh:
                    raise ValueError("미리보기 값을 실제 측정표에 자동 기록할 수 없습니다.")
                fields = {key: (value, "") for key, value in zip(metrics, (120., 1., 60., 120., 103.923048, .5, 60.))}
                note = "미리보기 · 실제 측정값이 아닙니다."
            elif self.profile.family == "PPA":
                # A POWER result read clears DAV bit 0. Discard the current
                # result and the next update so neither can predate the dwell.
                # This avoids *TRG, which restarts ranging and smoothing.
                if include_dc and fresh:
                    coupling = self.transport.query(f"COUPLI,PHASE{channel}?").strip()
                    if coupling not in ("0", "2"):
                        raise ValueError("PPA의 DC 측정에는 해당 입력의 커플링을 AC+DC 또는 DC로 설정하세요.")
                self.transport.write("RESOLU,HIGH")
                rms_command = f"POWER,PHASE{channel},RMS?"
                if fresh:
                    dut_numbers(self.transport.query(rms_command), 5)
                    for index in range(2):
                        self._wait_data_flag("DAV?", 3,
                            "PPA 새 측정값 대기 시간 초과: 본체 측정 상태·갱신 주기와 DUT 응답 대기 시간을 확인하세요.")
                        if index == 0:
                            dut_numbers(self.transport.query(rms_command), 5)
                rms = dut_number_fields(self.transport.query(rms_command), 5)
                power = dut_number_fields(self.transport.query(f"POWER,PHASE{channel},WATTS?"), 11)
                fields = dict(zip(metrics,
                    (rms[1], rms[3], power[1], power[3], power[5], power[7], power[0])))
                if include_dc:
                    fields.update(dc_voltage=rms[2], dc_current=rms[4])
                if fresh and channel <= 3:
                    voltage_over = self._ppa_range_overloaded(2 * channel - 1)
                    current_over = self._ppa_range_overloaded(2 * channel)
                    for overloaded, keys in ((voltage_over, ("voltage", "dc_voltage", "frequency")),
                                             (current_over, ("current", "dc_current"))):
                        if overloaded:
                            for key in keys:
                                if key in fields:
                                    fields[key] = (None, "")
                    if voltage_over or current_over:
                        fields.update(dict.fromkeys(("power", "apparent", "reactive", "pf"), (None, "")))
                note = ("PPA 새 데이터 갱신 확인 · RMS·전력 순차 조회" if fresh
                        else "PPA RMS·전력 순차 조회 (동시 샘플 아님) · 역률 부호는 본체 설정 기준")
            else:
                # Mapping affects communication output items only, not ranges or
                # scale factors. Reapply per read so another panel cannot silently
                # change which quantity a numeric position represents.
                self.transport.write(":COMMUNICATE:HEADER OFF")
                self.transport.write(":NUMERIC:FORMAT ASCII")
                count = 9 if include_dc and not self.profile.wt3000_numeric else 7
                self.transport.write(f":NUMERIC:NORMAL:NUMBER {count}")
                if self.profile.wt3000_numeric:
                    # Legacy U/I follows the element's measurement mode.
                    voltage_mode = self.transport.query(f":INPUT:VOLTAGE:MODE:ELEMENT{channel}?").strip().upper()
                    current_mode = self.transport.query(f":INPUT:CURRENT:MODE:ELEMENT{channel}?").strip().upper()
                    valid_modes = {"RMS", "MEAN", "DC", "RMEAN"}
                    if voltage_mode not in valid_modes or current_mode not in valid_modes:
                        raise ValueError("WT3000 계열의 전압·전류 측정 모드를 확인할 수 없습니다.")
                    vi_modes = (voltage_mode, current_mode)
                    functions = ("U", "I", "P", "S", "Q", "LAMBDA", "FU")
                else:
                    functions = ("URMS", "IRMS", "P", "S", "Q", "LAMBDA", "FU")
                    if include_dc:
                        functions += ("UDC", "IDC")
                items = []
                for function in functions:
                    order = ",TOTAL" if self.profile.wt3000_numeric and function != "FU" else ""
                    items.append(f"{function},{channel}{order}")
                for index, item in enumerate(items, 1):
                    self.transport.write(f":NUMERIC:NORMAL:ITEM{index} {item}")
                # Readback prevents unsupported firmware from returning older,
                # differently ordered values after silently rejecting a setting.
                for index, item in enumerate(items, 1):
                    query = f":NUMERIC:NORMAL:ITEM{index}?"
                    reply = self._wt_reply_value(self.transport.query(query)).replace(" ", "")
                    if reply.startswith("LAMB,"):
                        reply = reply.replace("LAMB,", "LAMBDA,", 1)
                    if reply.endswith(",TOT"):
                        reply = reply.removesuffix(",TOT") + ",TOTAL"
                    if reply != item and self.profile.model.startswith(("WT310", "WT332", "WT333")):
                        fallback = item.replace("URMS,", "U,", 1).replace("IRMS,", "I,", 1)
                        if fallback != item:
                            self._set_wt_default(":INPUT:MODE", "RMS")
                            self.transport.write(f":NUMERIC:NORMAL:ITEM{index} {fallback}")
                            reply = self._wt_reply_value(self.transport.query(query)).replace(" ", "")
                            item = fallback
                    if reply != item:
                        raise ValueError(f"DUT 조회 항목 설정 확인 실패: {item} / {reply[:100]}")
                self.transport.write(":NUMERIC:HOLD OFF")
                readings = self._read_wt_fields(count, fresh)
                fields = dict(zip(metrics, readings))
                if count == 9:
                    fields.update(dc_voltage=readings[7], dc_current=readings[8])
                elif include_dc and vi_modes:
                    if vi_modes[0] == "DC":
                        fields["dc_voltage"] = readings[0]
                    if vi_modes[1] == "DC":
                        fields["dc_current"] = readings[1]
                note = ("WT 새 데이터 갱신 확인 · 본체 배율 적용값" if fresh
                        else "WT 통신 출력 7항목 · 주파수는 전압 기준 · 본체 배율 적용값")
            if self.cancel.is_set():
                raise InterruptedError("DUT 조회를 중지했습니다.")
            pf, _ = fields["pf"]
            if pf is not None and abs(pf) > 1.000001:
                fields["pf"] = (None, "")
            values = {key: value for key, (value, _) in fields.items()}
            raw_values = {key: token for key, (value, token) in fields.items() if value is not None and token}
            self.sample = DUTSample(self.identity, channel, datetime.datetime.now().isoformat(timespec='seconds'),
                                    values, note, self.simulated, vi_modes, raw_values)
            return self.sample
        except Exception:
            # A late response cannot become the next query's result.
            self.disconnect()
            raise

    def disconnect(self):
        if self.transport is not None:
            self.transport.close()
        self.transport = self.profile = self.sample = None
        self.identity = ""
        self.simulated = False
        self.input_channels = ()

import select
import struct
from contextlib import ExitStack
from dataclasses import replace


@dataclass(frozen=True)
class DUTDiscoveryCandidate:
    settings: DUTSettings
    profile: DUTProfile
    identity: str


def _dut_discovery_check_cancel(cancel):
    if cancel.is_set():
        raise InterruptedError("DUT 자동 검색을 중지했습니다.")


def _dut_vxi11_port_reply(data, xid):
    if len(data) < 28:
        return False
    received_xid, message_type, reply_state, _verifier_type, verifier_length = struct.unpack_from("!5I", data)
    if (received_xid, message_type, reply_state) != (xid, 1, 0) or verifier_length > 400:
        return False
    offset = 20 + ((verifier_length + 3) & ~3)
    if len(data) != offset + 8:
        return False
    accepted, port = struct.unpack_from("!2I", data, offset)
    return accepted == 0 and 1 <= port <= 65535


def _dut_discover_vxi11(cancel, adapters, diagnostics):
    # LXI VXI-11 Discovery and Identification Extended Function §10.1.
    # RFC 1057 Appendix A: portmapper v2 GETPORT, TCP device core v1.
    interfaces = {}
    for adapter in adapters:
        if adapter.get("state") != "Up" or not adapter.get("hardware", True):
            continue
        for address in adapter.get("addresses") or ():
            try:
                interface = ipaddress.IPv4Interface(f"{address['ip']}/{address['prefix']}")
            except (KeyError, ValueError):
                continue
            if (1 <= interface.network.prefixlen < 31 and not interface.ip.is_loopback
                    and not interface.ip.is_unspecified and not interface.ip.is_multicast
                    and interface.ip not in (interface.network.network_address, interface.network.broadcast_address)):
                interfaces.setdefault(str(interface.ip), interface.network)

    if not interfaces:
        diagnostics.append("LAN 자동 검색에 사용할 활성 IPv4 어댑터가 없습니다.")
        return []

    xid = int.from_bytes(os.urandom(4), "big")
    request = struct.pack("!14I", xid, 0, 2, 100000, 2, 3, 0, 0, 0, 0, 0x0607AF, 1, 6, 0)
    hosts = set()
    with ExitStack() as stack:
        receivers = {}
        for source, network in list(interfaces.items())[:8]:
            _dut_discovery_check_cancel(cancel)
            try:
                sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(.2)
                sock.bind((source, 0))
                sock.sendto(request, (str(network.broadcast_address), 111))
                receivers[sock] = network
            except OSError as exc:
                diagnostics.append(f"LAN 검색 요청 실패 ({source}): {exc}")

        deadline = time.monotonic() + 1.2
        while receivers and len(hosts) < 12:
            _dut_discovery_check_cancel(cancel)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select(list(receivers), [], [], min(.1, remaining))
            except OSError as exc:
                diagnostics.append(f"LAN 검색 수신 실패: {exc}")
                break
            for sock in readable:
                try:
                    data, (source, port) = sock.recvfrom(512)
                except OSError:
                    continue
                if port != 111 or source in interfaces or not _dut_vxi11_port_reply(data, xid):
                    continue
                host, network = ipaddress.IPv4Address(source), receivers[sock]
                if host in network and host not in (network.network_address, network.broadcast_address):
                    hosts.add(source)
    _dut_discovery_check_cancel(cancel)
    diagnostics.append(f"같은 네트워크에서 VXI-11 응답 {len(hosts)}개를 받았습니다.")
    return sorted(hosts, key=ipaddress.IPv4Address)


def _dut_discovery_usb_key(resource):
    match = re.fullmatch(r"USB\d*::(0X[0-9A-F]+|\d+)::(0X[0-9A-F]+|\d+)::([^:]+)(?:::\d+)?::INSTR", resource.upper())
    if not match:
        return None
    vendor, product, serial_no = match.groups()
    return (int(vendor, 16 if vendor.startswith("0X") else 10),
            int(product, 16 if product.startswith("0X") else 10), serial_no)


def _dut_discovery_ipv4(address):
    try:
        ip = ipaddress.IPv4Address(AutoNetworkAligner.normalize_ipv4(address))
    except ValueError:
        return None
    return None if ip.is_loopback or ip.is_reserved else str(ip)


def _dut_discovery_resource_ip(resource):
    kind, delimiter, rest = resource.upper().partition("::")
    if delimiter and re.fullmatch(r"TCPIP\d*", kind):
        return _dut_discovery_ipv4(rest.split("::", 1)[0])
    return None


def discover_duts(cancel, *, excluded_resource="", excluded_ip="", include_usb=True, include_lan=True, adapters=None):
    """Return all recognized, closed probe sessions and discovery diagnostics.

    Discovery never needs network configuration changes or address-range scans.
    Literal IPv4 VISA resources avoid probing DNS aliases for the calibrator.
    Raw PPA/WT5000 sockets are considered only when explicitly listed in VISA.
    """
    _dut_discovery_check_cancel(cancel)
    diagnostics, candidates = [], []
    if not (include_usb or include_lan):
        return candidates, diagnostics
    if not HAS_PYVISA:
        diagnostics.append(dependency_import_message("pyvisa", "PyVISA", PYVISA_IMPORT_ERROR))
        return candidates, diagnostics

    manager = None
    try:
        manager = SharedVisaManager()
        try:
            resources = manager.list_resources("?*")
        finally:
            manager.close()
    except Exception as exc:
        resources = ()
        diagnostics.append(f"VISA 장치 목록을 읽지 못했습니다: {exc}")
        if manager is None:
            diagnostics.append("VISA 드라이버를 시작할 수 없습니다. NI-VISA 설치 상태를 확인하세요.")
            return candidates, diagnostics

    _dut_discovery_check_cancel(cancel)
    excluded_resource = excluded_resource.strip().upper()
    excluded_usb = _dut_discovery_usb_key(excluded_resource)
    excluded_hosts = {_dut_discovery_ipv4(excluded_ip), _dut_discovery_resource_ip(excluded_resource)}

    targets = {}
    listed_lan = {}
    for resource in resources:
        resource = resource.strip()
        upper = resource.upper()
        if upper == excluded_resource:
            continue
        usb_key = _dut_discovery_usb_key(resource) if include_usb else None
        if usb_key is not None and usb_key != excluded_usb and usb_key[0] != 0x0F7E:
            targets.setdefault(("VISA", usb_key), DUTSettings(mode="VISA", address=resource, eol="LF"))
        if not include_lan:
            continue
        match = re.fullmatch(r"TCPIP\d*::([^:]+)(?:::([^:]+))?::(INSTR|SOCKET)", upper)
        if not match:
            continue
        host, endpoint, kind = match.groups()
        host = _dut_discovery_ipv4(host)
        if host is None or host in excluded_hosts:
            continue
        if kind == "INSTR" and endpoint in (None, "INST0"):
            settings = DUTSettings(mode="WT_LAN", address=host, eol="LF")
        elif kind == "SOCKET" and endpoint == "10001":
            settings = DUTSettings(mode="PPA_LAN", address=host)
        elif kind == "SOCKET" and endpoint == "10002":
            settings = DUTSettings(mode="WT5000_TCP", address=host, port=10002, eol="LF")
        else:
            continue
        listed_lan.setdefault((settings.mode, host), settings)

    usb_count = len(targets)
    if include_usb:
        diagnostics.append(f"VISA USB-TMC 후보 {usb_count}개를 확인했습니다.")
    if include_lan:
        if adapters is None:
            adapters = AutoNetworkAligner.list_ethernet_interfaces()
            if AutoNetworkAligner.last_error:
                diagnostics.append(AutoNetworkAligner.last_error)
        for host in _dut_discover_vxi11(cancel, adapters, diagnostics):
            if host not in excluded_hosts:
                targets.setdefault(("WT_LAN", host), DUTSettings(mode="WT_LAN", address=host, eol="LF"))
        for key, settings in listed_lan.items():
            targets.setdefault(key, settings)

    deadline = time.monotonic() + 8.0
    for index, settings in enumerate(targets.values()):
        _dut_discovery_check_cancel(cancel)
        if index >= 12 or time.monotonic() >= deadline:
            diagnostics.append("자동 확인 범위에 도달했습니다. 표시되지 않은 장비는 주소를 직접 입력하세요.")
            break
        probe = DUTSession()
        probe.cancel = cancel
        try:
            identity = probe.connect(replace(settings, timeout=1.0))
            candidates.append(DUTDiscoveryCandidate(settings, probe.profile, identity))
        except InterruptedError:
            raise
        except Exception as exc:
            diagnostics.append(f"{settings.address}: {str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}")
        finally:
            probe.disconnect()

    _dut_discovery_check_cancel(cancel)
    if candidates:
        diagnostics.append(f"식별이 확인된 DUT {len(candidates)}대가 있습니다.")
    else:
        message = ""
        if include_usb:
            message = ("USB 계측기는 목록에 있지만 지원 DUT로 식별되지 않았습니다. 본체의 USB 원격 설정과 통신 기록을 확인하세요."
                       if usb_count else
                       "VISA 목록에 USB DUT가 없습니다. 케이블과 본체의 USB 원격 설정을 확인하세요.")
        if include_lan:
            message += (" " if message else "") + "LAN에서 찾지 못한 DUT는 연결 방식을 선택해 IP를 직접 입력하세요."
        diagnostics.append(message)
    return candidates, diagnostics

class Fluke5560AConnectionManager:
    def __init__(self):
        self.info = ConnectionInfo()
        self._visa_rm = None
        self._visa_inst = None
        self._lan_sock = None
        self._transport = None
        self._serial_port = None
        self.expected_model = "5560A"
        self.profile = MODEL_PROFILES["5560A"]
        self._lock = threading.RLock()
        self._sim_oper_state = False
        self._sim_last_out = "0 V, 0 HZ"
        self.last_error = ""
        self.source_ip = ""
        self._last_lan_source = None
        self._lan_tcp_failures = []
        self.last_diagnostic = ""
        self.operation_timeout = 60.0
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self._log_lock = threading.Lock()
        application_path = sys.executable if getattr(sys, "frozen", False) else __file__
        self.log_path = os.path.join(os.path.dirname(os.path.abspath(application_path)), "logs",
                                     f"fluke_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}_{os.getpid()}.log")

    def record_event(self, message, notify=False):
        with self._log_lock:
            try:
                os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as log:
                    log.write(f"[{datetime.datetime.now().isoformat(timespec='milliseconds')}] {message}\n")
            except OSError:
                if notify:
                    message += " (로그 파일 저장 실패)"
        if notify:
            self.events.put(message)

    def request_abort(self):
        self.cancel_event.set()

    def _waiting(self, elapsed, limit):
        self.events.put(f"[출력 안정화 대기] {elapsed:.0f}/{limit:g}초 — 완료 응답을 기다리고 있습니다.")

    @staticmethod
    def get_local_pc_ipv4_list():
        return [item['ip'] for adapter in AutoNetworkAligner.list_ethernet_interfaces()
                if adapter['state'] == 'Up' for item in (adapter.get('addresses') or [])]

    def _validate_idn(self, value):
        fields = [part.strip() for part in value.split(",")]
        return (len(fields) >= 4 and fields[0].upper() == "FLUKE"
                and fields[1].upper() == self.expected_model)

    def _identify(self, value, interface):
        if not self._validate_idn(value):
            raise InstrumentProtocolError(f"선택 모델 {self.expected_model}과 식별 응답이 다릅니다: {value}")
        profile = MODEL_PROFILES[value.split(",")[1].strip().upper()]
        if interface not in profile.interfaces:
            raise InstrumentProtocolError(f"{profile.model}은 {interface} 포트를 지원하지 않습니다.")
        self.profile = profile

    def _prepare_remote_stream(self, transport, interface):
        # 552x/5080A do not have 5560A's COMM_MODE command. Their TERM echo
        # and prompts are handled by the reader without changing saved settings.
        if self.profile.model == "5560A":
            port = "TELNET" if interface == "LAN" else "SERIAL"
            mode = transport.query(f"COMM_MODE? {port}").strip().upper()
            if mode == "TERM":
                transport.write(f"COMM_MODE {port}, COMP")
                mode = transport.query(f"COMM_MODE? {port}").strip().upper()
            if mode != "COMP":
                raise InstrumentProtocolError("Computer 모드 전환을 확인하지 못했습니다.")
        transport.write("REMOTE")
        if transport.query("*OPC?").strip() != "1":
            raise InstrumentProtocolError("REMOTE 처리 완료 응답이 올바르지 않습니다.")

    @staticmethod
    def _validate_command(command):
        command = command.strip()
        if not command or any(c in command for c in "\r\n\x00"):
            raise ValueError("명령은 비어 있지 않은 한 줄이어야 합니다.")
        command.encode("ascii")
        return command

    @staticmethod
    def _validate_port(port):
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("포트는 1~65535 사이여야 합니다.")
        return port

    def _lan_source_ips(self, ip, port, source_ip, adapter_idx):
        if source_ip and not adapter_idx:
            return (AutoNetworkAligner.normalize_ipv4(source_ip),)
        primary = AutoNetworkAligner.select_source_ip(adapter_idx, ip)
        candidates = list(AutoNetworkAligner.link_local_source_ips(primary, ip, adapter_idx))
        previous = self._last_lan_source
        if previous and previous[:2] == (ip, port) and previous[2] in candidates:
            candidates.remove(previous[2])
            candidates.insert(0, previous[2])
        return tuple(candidates[:3])

    def _open_lan_socket(self, ip, port, timeout, source_ips):
        self._lan_tcp_failures = []
        for index, source_ip in enumerate(source_ips):
            self.source_ip = source_ip
            if source_ip == ip:
                raise ValueError("5560A IP와 PC IP가 같습니다. 장비 화면의 IP를 확인하세요.")
            try:
                sock = socket.create_connection((ip, port), timeout,
                                                source_address=(source_ip, 0) if source_ip else None)
            except (TimeoutError, ConnectionRefusedError) as exc:
                self._lan_tcp_failures.append(f"PC {source_ip or '자동 경로'} · {type(exc).__name__}: {exc}")
                if index + 1 == len(source_ips):
                    raise
                self.record_event(f"[LAN 재시도] {self._lan_tcp_failures[-1]} → PC {source_ips[index + 1]}", notify=True)
            else:
                self.source_ip = sock.getsockname()[0]
                return sock

    def scan_and_auto_connect(self, preferred_mode="AUTO", lan_ip="", lan_port=3490,
                              gpib_addr=1, timeout_sec=3.0, allow_raw_socket_fallback=False,
                              source_ip="", eol="CRLF", visa_resource="", adapter_idx="",
                              operation_timeout_sec=60.0, model="5560A", serial_port="",
                              baudrate=9600, data_bits=8, parity="N", stop_bits=1, flow_control="XON/XOFF"):
        # Legacy fallback argument is intentionally ignored: IDN verification is mandatory.
        mode = preferred_mode.strip().upper()
        if mode not in ("AUTO", "USB", "GPIB", "RS232", "LAN", "LAN (SOCKET)", "LAN (VISA)", "SIMULATION"):
            raise ValueError(f"지원하지 않는 연결 모드: {mode}")
        model = model.strip().upper()
        if model not in MODEL_PROFILES:
            raise ValueError(f"지원하지 않는 모델: {model}")
        profile = MODEL_PROFILES[model]
        interface = "LAN" if mode.startswith("LAN") else mode
        if interface not in ("AUTO", "SIMULATION") and interface not in profile.interfaces:
            raise ValueError(f"{model} 연결 가능: {', '.join(profile.interfaces)}. USB–RS-232 컨버터는 RS232를 선택하세요.")
        if not math.isfinite(timeout_sec) or not 0.2 <= timeout_sec <= 60:
            raise ValueError("통신 제한 시간은 0.2~60초 사이여야 합니다.")
        if not math.isfinite(operation_timeout_sec) or not 1 <= operation_timeout_sec <= 300:
            raise ValueError("출력 안정화 제한 시간은 1~300초 사이여야 합니다.")
        self.disconnect()
        self.expected_model = model
        self.profile = profile
        self.cancel_event.clear()
        self.operation_timeout = operation_timeout_sec
        self.last_error = ""
        self.source_ip = source_ip
        if mode == "SIMULATION":
            self._sim_oper_state = False
            self._sim_last_out = "0 V, 0 HZ"
            self.info = ConnectionInfo(True, "SIMULATION (가상 장비 모드)", f"SIM::FLUKE_{model}::INSTR", f"FLUKE,{model},SIM-9901234,1.0")
            return self.info
        errors = []
        # Explicit LAN address in AUTO takes precedence over unrelated instruments.
        if "LAN" in profile.interfaces and mode in ("AUTO", "LAN", "LAN (SOCKET)", "LAN (VISA)") and lan_ip.strip():
            ip = AutoNetworkAligner.normalize_ipv4(lan_ip)
            port = self._validate_port(lan_port)
            source_ips = self._lan_source_ips(ip, port, source_ip, adapter_idx)
            result = self._try_lan_socket(ip, port, timeout_sec, eol=eol, source_ips=source_ips)
            if result.connected:
                return result
            errors.append(self.last_error)
            if mode != "AUTO":
                raise ConnectionError(self.build_lan_diagnostic_report(ip, port))
        elif mode.startswith("LAN"):
            raise ValueError("5560A 본체 Ethernet 화면에 표시된 현재 IP 주소를 입력하세요.")
        if mode == "RS232" or (mode == "AUTO" and serial_port.strip()):
            result = self._try_serial(serial_port, timeout_sec, eol, baudrate, data_bits, parity, stop_bits, flow_control)
            if result.connected:
                return result
            errors.append(self.last_error)
        if mode in ("AUTO", "USB", "GPIB") and set(profile.interfaces) & {"USB", "GPIB"}:
            result = self._try_visa_auto_scan(mode, gpib_addr, timeout_sec, visa_resource)
            if result.connected:
                return result
            errors.append(self.last_error)
        self.last_error = "\n".join(errors)
        raise ConnectionError(self.last_error or "연결할 인터페이스와 장비 IP/리소스를 지정하세요.")

    def _try_lan_socket(self, ip, port=3490, timeout_sec=3.0, allow_open_port_fallback=False,
                        source_ip="", eol="CRLF", *, source_ips=None):
        sock = None
        self._lan_tcp_failures = []
        stage = "TCP 연결"
        try:
            ip = AutoNetworkAligner.normalize_ipv4(ip)
            port = self._validate_port(port)
            sock = self._open_lan_socket(ip, port, timeout_sec, source_ips or (source_ip,))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            transport = TelnetLineTransport(sock, timeout_sec, eol)
            stage = "장비 식별 (*IDN?)"
            idn = transport.query("*IDN?", identity=True)
            self._identify(idn, "LAN")
            # Identification is complete before sending any state-changing commands.
            stage = "Computer 통신 모드 확인"
            self._prepare_remote_stream(transport, "LAN")
            self._lan_sock, self._transport = sock, transport
            self.info = ConnectionInfo(True, "LAN (Ethernet Socket)", f"TCPIP::{ip}::{port}::SOCKET", idn, ip, port)
            self._last_lan_source = (ip, port, self.source_ip)
            self.last_error = ""
            return self.info
        except Exception as exc:
            self.last_error = f"[{stage}] {ip}:{port} / PC {self.source_ip or source_ip or '자동 경로'}\n{type(exc).__name__}: {exc}"
            if len(self._lan_tcp_failures) > 1:
                self.last_error += "\n시도한 PC 주소:\n" + "\n".join(self._lan_tcp_failures)
            if sock is not None:
                close_lan_socket(sock)
            self._lan_sock = self._transport = None
            self.info = ConnectionInfo()
            return self.info

    @staticmethod
    def list_serial_ports():
        if not HAS_PYSERIAL:
            return []
        return [(p.device, p.description) for p in serial.tools.list_ports.comports()]

    def _try_serial(self, port, timeout, eol, baudrate, data_bits, parity, stop_bits, flow_control):
        if not HAS_PYSERIAL:
            self.last_error = dependency_import_message("pyserial", "pyserial", PYSERIAL_IMPORT_ERROR)
            return ConnectionInfo()
        handle = None
        stage = "COM 포트 열기"
        try:
            if not port.strip():
                raise ValueError("장치 관리자 또는 COM 목록에서 컨버터의 COM 번호를 선택하세요.")
            rates = (300, 600, 1200, 2400, 4800, 9600)
            if self.profile.model in ("5560A", "5080A"):
                rates += (19200, 38400, 57600, 115200)
            if self.profile.model == "5560A":
                rates = rates[5:]
            if int(baudrate) not in rates or int(data_bits) not in (7, 8) or parity not in ("N", "E", "O") or float(stop_bits) not in (1, 2):
                raise ValueError(f"{self.profile.model}의 속도/데이터/패리티/정지 비트 설정을 확인하세요.")
            if flow_control not in ("NONE", "XON/XOFF", "RTS/CTS"):
                raise ValueError("흐름 제어는 NONE, XON/XOFF, RTS/CTS 중 선택하세요.")
            handle = serial.Serial(port=port.strip(), baudrate=int(baudrate), bytesize=int(data_bits),
                                   parity=parity, stopbits=float(stop_bits), timeout=.2, write_timeout=timeout,
                                   xonxoff=flow_control == "XON/XOFF", rtscts=flow_control == "RTS/CTS", dsrdtr=False)
            transport = SerialLineTransport(SerialEndpoint(handle), timeout, eol)
            stage = "장비 식별 (*IDN?)"
            idn = transport.query("*IDN?", identity=True)
            self._identify(idn, "RS232")
            stage = "원격 제어 준비"
            self._prepare_remote_stream(transport, "RS232")
            self._serial_port, self._transport = handle, transport
            self.info = ConnectionInfo(True, "RS232 (COM / USB–RS-232)",
                                       f"{port} / {baudrate} / {data_bits}{parity}{stop_bits} / {flow_control}", idn)
            self.last_error = ""
            return self.info
        except Exception as exc:
            if handle is not None:
                handle.close()
            self._serial_port = self._transport = None
            self.info = ConnectionInfo()
            self.last_error = (f"[{stage}] {port}: {type(exc).__name__}: {exc}\n"
                               "본체의 RS-232 선택, 속도/데이터/패리티/정지 비트/흐름 제어/EOL 일치 여부를 확인하세요. "
                               "5522A·5520A는 SERIAL 1 FROM HOST에 연결하고 null-modem 결선이 필요합니다.")
            return self.info

    def _try_visa_auto_scan(self, filter_mode, gpib_addr, timeout_sec, visa_resource=""):
        if not HAS_PYVISA:
            self.last_error = dependency_import_message("pyvisa", "PyVISA", PYVISA_IMPORT_ERROR)
            return ConnectionInfo()
        errors = []
        try:
            self._visa_rm = SharedVisaManager()
            resources = [visa_resource.strip()] if visa_resource.strip() else list(self._visa_rm.list_resources())
            if filter_mode == "GPIB" and not visa_resource.strip():
                if not 0 <= int(gpib_addr) <= 30:
                    raise ValueError("GPIB 주소는 0~30 사이여야 합니다.")
                # Honor the requested address, not whichever device answers first.
                resources = [f"GPIB0::{int(gpib_addr)}::INSTR"]
            for resource in resources:
                kind = resource.upper().split("::", 1)[0]
                if not kind.startswith(("USB", "GPIB")):
                    continue
                interface = "USB" if kind.startswith("USB") else "GPIB"
                if interface not in self.profile.interfaces:
                    continue
                if filter_mode in ("USB", "GPIB") and not kind.startswith(filter_mode):
                    continue
                inst = None
                try:
                    inst = self._visa_rm.open_resource(resource, open_timeout=int(timeout_sec * 1000))
                    inst.timeout = int(timeout_sec * 1000)
                    inst.read_termination = "\n"
                    inst.write_termination = "\n"
                    idn = inst.query("*IDN?").strip()
                    self._identify(idn, interface)
                    if interface == "GPIB":
                        inst.control_ren(pyvisa.constants.RENLineOperation.asrt_address)
                    self._visa_inst = inst
                    # REMOTE/LOCAL strings are Ethernet/serial commands, not GPIB/USBTMC.
                    self.info = ConnectionInfo(True, "USB (USBTMC)" if kind.startswith("USB") else "GPIB (IEEE-488.2)", resource, idn)
                    return self.info
                except Exception as exc:
                    errors.append(f"{resource}: {exc}")
                    if inst is not None:
                        self._visa_rm.close_resource(inst)
            self.last_error = "\n".join(errors) or "VISA에서 해당 장비를 찾지 못했습니다. USB는 후면 USB Device(B형) 포트와 USBTMC 활성화, GPIB는 주소/어댑터 드라이버를 확인하세요."
        except Exception as exc:
            self.last_error = (f"VISA 초기화/검색 실패: {type(exc).__name__}: {exc}\n"
                               f"실행 Python: {sys.executable}\n"
                               "PyVISA는 불러왔습니다. NI-VISA 또는 사용 중인 USB/GPIB 어댑터의 VISA 드라이버를 확인하세요.")
        if self._visa_rm is not None:
            self._visa_rm.close()
            self._visa_rm = None
        return ConnectionInfo()

    def build_lan_diagnostic_report(self, target_ip, target_port):
        if self.expected_model == "5080A":
            return (f"대상: 5080A {target_ip}:{target_port}\nPC 원본 IP: {self.source_ip or 'Windows 자동 경로'}\n"
                    f"{self.last_error}\n\n{AutoNetworkAligner.describe(target_ip, self.source_ip)}\n\n"
                    "5080A 확인 순서:\n1. 본체 Remote Interface Port를 LAN으로 선택하세요.\n"
                    "2. LAN 메뉴의 현재 IP/마스크/포트와 맞추세요. 기본 포트는 3490입니다.\n"
                    "3. 다른 프로그램의 연결을 해제하세요. LAN은 한 번에 한 연결만 허용합니다.\n"
                    "4. 직결이면 PC와 장비가 같은 서브넷의 서로 다른 주소여야 합니다.\n"
                    "PC IP/방화벽/장비 설정은 이 진단으로 변경하지 않습니다.")
        return (f"대상: {target_ip}:{target_port}\nPC 원본 IP: {self.source_ip or 'Windows 자동 경로'}\n"
                f"{self.last_error}\n\n{AutoNetworkAligner.describe(target_ip, self.source_ip)}\n\n"
                "확인 순서:\n"
                "1. 장비 Setup > System Settings > Remote Port Setup > Ethernet 활성화 및 현재 IP/마스크/포트를 확인하세요. 기본 포트는 3490입니다.\n"
                "2. Network Security에서 실제 PC 원본 IP를 포함하는 허용 범위를 Enable 하세요. 세 범위가 모두 Disable이면 모든 접속이 차단됩니다.\n"
                "3. 다른 PC, PuTTY, NI MAX, 기존 제어 프로그램의 LAN 세션을 닫으세요. 5560A는 LAN 연결을 하나만 허용합니다.\n"
                "4. TCP는 연결되지만 응답이 없으면 Remote Interface=Computer, EOL=CR/LF를 확인하세요.\n"
                "5. 직결 환경에서 DHCP 서버가 없으면 양쪽에 같은 서브넷의 서로 다른 고정 IP를 지정하세요.\n"
                "시간 초과만으로 방화벽 차단을 확정할 수 없습니다. 이 진단은 PC IP나 방화벽을 변경하지 않습니다.")

    def diagnose_lan(self, ip, port=3490, timeout=3.0, source_ip="", eol="CRLF", adapter_idx=""):
        """One read-only IDN session, no REMOTE, STBY, OUT, or configuration commands."""
        if self.info.connected:
            raise ConnectionError("현재 연결을 해제한 후 LAN 진단을 실행하세요.")
        if "LAN" not in MODEL_PROFILES[self.expected_model].interfaces:
            raise ValueError(f"{self.expected_model}은 기본 LAN 포트를 지원하지 않습니다.")
        if not math.isfinite(timeout) or not 0.2 <= timeout <= 60:
            raise ValueError("통신 제한 시간은 0.2~60초 사이여야 합니다.")
        ip = AutoNetworkAligner.normalize_ipv4(ip)
        port = self._validate_port(port)
        source_ips = self._lan_source_ips(ip, port, source_ip, adapter_idx)
        stage = "TCP 연결"
        sock = None
        try:
            sock = self._open_lan_socket(ip, port, timeout, source_ips)
            stage = "TCP 성공, *IDN? 확인"
            idn = TelnetLineTransport(sock, timeout, eol).query("*IDN?", identity=True)
            if not self._validate_idn(idn):
                raise InstrumentProtocolError(f"다른 장비 응답: {idn}")
            self._last_lan_source = (ip, port, self.source_ip)
            self.last_error = f"[진단 성공] TCP 및 {self.expected_model} 식별 확인: {idn}"
        except Exception as exc:
            self.last_error = f"[{stage}] {type(exc).__name__}: {exc}"
        finally:
            if sock is not None:
                close_lan_socket(sock)
        if self._lan_tcp_failures:
            self.last_error += "\n앞선 TCP 연결 실패:\n" + "\n".join(self._lan_tcp_failures)
        self.last_diagnostic = self.build_lan_diagnostic_report(ip, port)
        return self.last_diagnostic

    def _drop_connection(self):
        if self._lan_sock is not None:
            close_lan_socket(self._lan_sock)
        for handle in (self._serial_port, self._visa_rm):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._visa_inst = self._visa_rm = self._lan_sock = self._transport = None
        self._serial_port = None
        self.info = ConnectionInfo()

    def _query(self, command, timeout=None, cancellable=True):
        """Caller holds the transaction lock and owns failure cleanup."""
        started = time.monotonic()
        self.record_event(f"TX QUERY >> {command} | timeout={timeout or '조회 설정'}")
        if cancellable and self.cancel_event.is_set():
            raise OperationCancelled("사용자가 현재 작업을 중단했습니다.")
        if self._visa_inst is not None:
            previous = self._visa_inst.timeout
            try:
                if timeout is not None:
                    self._visa_inst.timeout = int(timeout * 1000)
                response = self._visa_inst.query(command).strip()
            finally:
                self._visa_inst.timeout = previous
            if cancellable and self.cancel_event.is_set():
                raise OperationCancelled("사용자가 현재 작업을 중단했습니다.")
        elif self._transport is not None:
            response = self._transport.query(command, timeout=timeout,
                       cancel_event=self.cancel_event if cancellable else None,
                       progress=self._waiting if command == "*OPC?" and timeout else None)
        else:
            raise ConnectionError("유효한 통신 핸들이 없습니다.")
        self.record_event(f"RX << {response} | {command} | {time.monotonic() - started:.3f}s")
        return response

    def _write(self, command):
        self.record_event(f"TX COMMAND >> {command}")
        if self._visa_inst is not None:
            self._visa_inst.write(command)
        elif self._transport is not None:
            self._transport.write(command)
        else:
            raise ConnectionError("유효한 통신 핸들이 없습니다.")

    def _fail_control_session(self, context, exc):
        # Keep the handle until STBY has been attempted. Never read after an
        # uncertain reply: a late OPC '1' could be mistaken for a new query.
        message = f"{context}: {type(exc).__name__}: {exc}"
        try:
            self._write("STBY")
            message += "\nSTBY 전송 시도 후 연결 종료. 출력 차단 확인은 되지 않았으므로 본체의 STBY 상태를 확인하세요."
        except Exception as standby_error:
            message += f"\nSTBY 전송 실패: {standby_error}. 본체의 STBY 상태를 확인하세요."
        finally:
            self._drop_connection()
        self.last_error = message
        self.record_event(message, notify=True)

    def query(self, cmd):
        command = self._validate_command(cmd)
        if ";" in command or "?" not in command:
            raise ValueError("조회는 물음표가 포함된 단일 명령만 사용하세요. 출력 설정은 전송 버튼을 사용하세요.")
        with self._lock:
            if not self.info.connected:
                raise ConnectionError("장비가 연결되어 있지 않습니다.")
            if self.info.interface_type.startswith("SIMULATION"):
                responses = {"*IDN?": self.info.idn_string, "OPER?": "1" if self._sim_oper_state else "0",
                             "OUT?": self._sim_last_out, "ERR?": '0,"No error"', "*OPC?": "1"}
                return responses.get(command.upper(), f"SIM_ACK ({command})")
            try:
                return self._query(command, timeout=self.operation_timeout if command.upper() == "*OPC?" else None)
            except Exception as exc:
                self._fail_control_session(f"조회 {command}", exc)
                raise

    def _stop_after_command_error(self, error, phase):
        # A valid error reply means the stream is still synchronized.
        self._write("STBY")
        if self._query("*OPC?", timeout=self.operation_timeout) != "1" or self._query("OPER?") != "0":
            raise InstrumentProtocolError(f"{self.profile.model} 오류 {error}; STBY 확인 실패")
        raise InstrumentCommandError(
            f"{self.profile.model} 오류 ({phase}): {error}. STBY 확인 완료, 연결 유지. 다음 행은 실행하지 않습니다."
        )

    def send_command(self, cmd, *, check_before=False):
        command = self._validate_command(cmd)
        with self._lock:
            if not self.info.connected:
                raise ConnectionError("장비가 연결되어 있지 않습니다.")
            if "?" in command:
                raise ValueError("조회 명령(?)은 조회 버튼 또는 query()를 사용하세요.")
            # Reject invalid settings before any output command reaches any transport.
            self.profile.validate_command(command)
            if self.info.interface_type.startswith("SIMULATION"):
                for part in command.upper().split(";"):
                    part = part.strip()
                    if part == "OPER":
                        self._sim_oper_state = True
                    elif part in ("STBY", "*RST"):
                        self._sim_oper_state = False
                    elif part.startswith("OUT "):
                        self._sim_last_out = part[4:]
                return
            started = time.monotonic()
            try:
                if self.cancel_event.is_set():
                    raise OperationCancelled("사용자가 현재 작업을 중단했습니다.")
                if check_before:
                    pending = self._query("ERR?")
                    if not re.match(r"^\+?0\s*(?:,|$)", pending):
                        self._stop_after_command_error(pending, "출력 전 대기 중이던 오류")
                self._write(command)
                if self._query("*OPC?", timeout=self.operation_timeout) != "1":
                    raise InstrumentProtocolError("명령 처리 완료를 확인하지 못했습니다.")
                error = self._query("ERR?")
                if not re.match(r"^\+?0\s*(?:,|$)", error):
                    self._stop_after_command_error(error, command)
                self.record_event(f"[명령 완료] {command} | {time.monotonic() - started:.3f}s", notify=True)
            except InstrumentCommandError as exc:
                self.last_error = str(exc)
                self.record_event(str(exc), notify=True)
                raise
            except Exception as exc:
                context = f"명령 [{command}] / {time.monotonic() - started:.3f}초 경과 / 완료 대기 한도 {self.operation_timeout:g}초"
                self._fail_control_session(context, exc)
                raise

    def _record_shutdown_error(self, phase):
        error = self.query("ERR?")
        self.record_event(f"[연결 해제 · {phase}] {error}",
                          notify=not re.match(r"^\+?0\s*(?:,|$)", error))

    def disconnect(self, standby=True):
        with self._lock:
            try:
                self.cancel_event.clear()
                if self.info.connected and standby:
                    checked_shutdown = self.profile.model == "5560A" and self._lan_sock is not None
                    if not checked_shutdown or self.query("OPER?") != "0":
                        self.send_command("STBY")
                        if checked_shutdown and self.query("OPER?") != "0":
                            raise InstrumentProtocolError("연결 해제 전 STBY를 확인하지 못했습니다.")
                    if checked_shutdown:
                        self.record_event("[연결 해제] STBY 확인")
                        self._record_shutdown_error("LOCAL 전")
                    if self._transport is not None:
                        self._write("LOCAL")
                        if self._query("*OPC?", timeout=self.operation_timeout) != "1":
                            raise InstrumentProtocolError("LOCAL 완료 응답을 확인하지 못했습니다.")
                        if checked_shutdown:
                            if self.query("OPER?") != "0":
                                raise InstrumentProtocolError("LOCAL 이후 STBY를 확인하지 못했습니다.")
                            self._record_shutdown_error("LOCAL 후")
                    elif self._visa_inst is not None:
                        try:
                            self._visa_inst.control_ren(pyvisa.constants.RENLineOperation.deassert_gtl)
                        except Exception:
                            pass
            finally:
                self._drop_connection()



# =====================================================================
# 6. 실제 엑셀(Excel)처럼 칸이 나뉘고 Ctrl+V 시 밑으로 바로 들어가는 시트 위젯
# =====================================================================
if HAS_TKINTER:
    class ExcelSheetGrid(tk.Frame):
        COL_SPECS = [
            ("행", 44, "center", False), ("제어", 76, "center", False),
            ("전압·전류 조건", 140, "center", True), ("주파수·조건", 105, "center", True),
            ("역률", 130, "center", True), ("목표값", 125, "e", True),
            ("실제 출력", 240, "w", False), ("전송 명령", 230, "w", False),
            ("상태", 110, "center", False),
            ("측정값", 130, "e", True),
        ]

        def __init__(
            self,
            master: tk.Widget,
            on_paste_text_callback,
            on_cell_edited_callback,
            on_row_start_callback,
            on_row_breakpoint_callback,
            on_row_select_callback,
        ) -> None:
            super().__init__(master, bg="#CBD5E1", bd=1, relief="solid")
            self.on_paste_text_callback = on_paste_text_callback
            self.on_cell_edited_callback = on_cell_edited_callback
            self.on_row_start_callback = on_row_start_callback
            self.on_row_breakpoint_callback = on_row_breakpoint_callback
            self.on_row_select_callback = on_row_select_callback

            self.selected_row_idx: int = 0
            self.selected_col_idx: int = 2
            self._row_widgets: List[Dict[str, object]] = []

            self._steps = []
            self._rendering_measurements = False
            self.show_commands = False
            self.show_measurements = True
            self._init_measurement_selection()
            self._build_grid_skeleton()
            self._bind_measurement_header()
            self.show_commands = False
            self.set_command_visibility(False)

        MEASUREMENT_COLUMN = 9

        @staticmethod
        def _display_column(index):
            return 6 if index == 9 else (index + 1 if index >= 6 else index)

        def set_measurement_visibility(self, visible):
            self.show_measurements = bool(visible)
            if not visible:
                self._reset_measurement_selection()
            self._apply_optional_columns()
            if not visible and self.selected_col_idx == self.MEASUREMENT_COLUMN:
                self.selected_col_idx = 5
                if 0 <= self.selected_row_idx < len(self._row_widgets):
                    self._row_widgets[self.selected_row_idx]["entries"][5].focus_set()

        def _apply_optional_columns(self):
            for column, visible in ((7, self.show_commands), (9, self.show_measurements)):
                cells = [self.header_frame.winfo_children()[column]] + [row["frames"][column] for row in self._row_widgets]
                for cell in cells:
                    if visible:
                        cell.grid()
                    else:
                        cell.grid_remove()
            self.header_frame.update_idletasks()
            self.cells_frame.update_idletasks()
            self._resize_columns()
            self._on_frame_configure()

        def _measurement_changed(self, row):
            if self._rendering_measurements or not 0 <= row < len(self._steps):
                return
            step = self._steps[row]
            measured = self._row_widgets[row]["measurement_var"].get()
            step.measured_raw = SIUnits.with_nominal_unit(measured, step.nominal_raw)

        def _refresh_measurement_value(self, row, *, preserve_entry=False):
            step = self._steps[row]
            widgets = self._row_widgets[row]
            measured = widgets["measurement_var"]
            if (preserve_entry and self.focus_get() is widgets["entries"][9]
                    and SIUnits.with_nominal_unit(measured.get(), step.nominal_raw) == step.measured_raw):
                return
            display = SIUnits.format_measurement(step.measured_raw, step.nominal_raw)
            if measured.get() != display:
                self._rendering_measurements = True
                try:
                    measured.set(display)
                finally:
                    self._rendering_measurements = False

        def _init_measurement_selection(self):
            self._measurement_anchor = self._measurement_end = None
            self._measurement_drag_active = self._measurement_dragging = False
            self._measurement_scroll_after = None
            self._measurement_pointer_y = 0
            self._measurement_menu = tk.Menu(self, tearoff=False)
            self._measurement_menu.add_command(label="선택한 측정값 복사  Ctrl+C",
                                               command=lambda: self._copy_measurements(force_cells=True))
            self._measurement_menu.add_command(label="현재 행부터 끝까지 복사", command=self._copy_measurements_to_end)
            self._measurement_menu.add_command(label="측정값 전체 복사", command=self._copy_all_measurements)
            self.bind("<Destroy>", self._measurement_destroyed, add="+")

        def _bind_measurement_header(self):
            header = self.header_frame.winfo_children()[self.MEASUREMENT_COLUMN]
            for widget in [header] + header.winfo_children():
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", self._copy_all_measurements)
            for label in header.winfo_children():
                label.configure(text="측정값 · 전체 복사")

        def _copy_all_measurements(self, _event=None):
            if self._steps:
                self._stop_measurement_drag()
                self._set_measurement_selection(0, len(self._steps) - 1)
                self._copy_measurements(force_cells=True)
            return "break"

        def _copy_measurements_to_end(self):
            if self._steps:
                self._stop_measurement_drag()
                self._set_measurement_selection(self.selected_row_idx, len(self._steps) - 1)
                self._copy_measurements(force_cells=True)
            return "break"

        def _measurement_destroyed(self, event):
            if event.widget is self:
                self._stop_measurement_drag()

        def _bind_measurement_selection(self, row):
            entry = self._row_widgets[row]["entries"][9]
            entry.configure(exportselection=False)
            for widget in (entry, self._row_widgets[row]["frames"][9]):
                widget.bind("<ButtonPress-1>", lambda event, r=row: self._measurement_press(event, r))
                widget.bind("<B1-Motion>", self._measurement_motion)
                widget.bind("<ButtonRelease-1>", self._measurement_release)
                widget.bind("<Button-3>", lambda event, r=row: self._measurement_context(event, r))
            entry.bind("<<Copy>>", lambda _event, r=row: self._copy_measurements(r))
            entry.bind("<Shift-Up>", lambda _event, r=row: self._measurement_extend_key(r, -1))
            entry.bind("<Shift-Down>", lambda _event, r=row: self._measurement_extend_key(r, 1))
            entry.bind("<Escape>", lambda _event: self._reset_measurement_selection())

        def _measurement_bounds(self):
            if self._measurement_anchor is None or self._measurement_end is None or not self._steps:
                return None
            lo, hi = sorted((self._measurement_anchor, self._measurement_end))
            return (lo, hi) if 0 <= lo <= hi < len(self._steps) else None

        def _measurement_contains(self, row):
            bounds = self._measurement_bounds()
            return bounds is not None and bounds[0] <= row <= bounds[1]

        def _set_measurement_selection(self, anchor, end):
            if not self._steps:
                return
            last = len(self._steps) - 1
            self._measurement_anchor = max(0, min(last, anchor))
            self._measurement_end = max(0, min(last, end))
            self._paint_measurement_selection()

        def _paint_measurement_selection(self):
            for row in range(len(self._row_widgets)):
                self._paint_measurement_cell(row)

        def _paint_measurement_cell(self, row):
            widgets = self._row_widgets[row]
            bg, fg, border = widgets.get("measurement_base_colors", ("white", "#0F172A", "#CBD5E1"))
            if self._measurement_contains(row):
                bg, fg, border = "#DBEAFE", "#1E3A8A", "#2563EB"
            widgets["entries"][9].configure(bg=bg, fg=fg)
            widgets["frames"][9].configure(bg=bg, highlightbackground=border)

        def _stop_measurement_drag(self):
            self._measurement_drag_active = self._measurement_dragging = False
            if self._measurement_scroll_after is not None:
                self.after_cancel(self._measurement_scroll_after)
                self._measurement_scroll_after = None

        def _reset_measurement_selection(self):
            self._stop_measurement_drag()
            self._measurement_anchor = self._measurement_end = None
            self._paint_measurement_selection()
            return "break"

        def _measurement_press(self, event, row):
            self._stop_measurement_drag()
            extend = bool(event.state & 1) and self._measurement_bounds() is not None
            anchor = self._measurement_anchor if extend else row
            self._set_measurement_selection(anchor, row)
            self._measurement_drag_active = True
            self._measurement_dragging = extend
            self._measurement_pointer_y = event.y_root
            self.selected_row_idx, self.selected_col_idx = row, self.MEASUREMENT_COLUMN
            self._row_widgets[row]["entries"][9].focus_set()
            if extend:
                self._clear_measurement_text_selection()
                return "break"
            # Keep Tk's normal cursor placement and horizontal text selection in one cell.
            return None

        def _measurement_row_at(self, root_y):
            position = self.canvas.canvasy(root_y - self.canvas.winfo_rooty())
            for row, widgets in enumerate(self._row_widgets):
                frame = widgets["frames"][9]
                if position < frame.winfo_y() + frame.winfo_height():
                    return row
            return max(0, len(self._row_widgets) - 1)

        def _clear_measurement_text_selection(self):
            for widgets in self._row_widgets:
                widgets["entries"][9].selection_clear()

        def _measurement_motion(self, event):
            if not self._measurement_drag_active or not self._steps:
                return None
            self._measurement_pointer_y = event.y_root
            row = self._measurement_row_at(event.y_root)
            if not self._measurement_dragging and row == self._measurement_anchor:
                return None
            self._measurement_dragging = True
            self._clear_measurement_text_selection()
            self._set_measurement_selection(self._measurement_anchor, row)
            if self._measurement_scroll_after is None:
                self._measurement_autoscroll()
            return "break"

        def _measurement_autoscroll(self):
            self._measurement_scroll_after = None
            if not self._measurement_drag_active or not self.show_measurements or not self._steps:
                return
            y = self._measurement_pointer_y - self.canvas.winfo_rooty()
            height = self.canvas.winfo_height()
            direction = -1 if y < 16 else (1 if y >= height - 16 else 0)
            before = self.canvas.yview()
            if direction:
                self.canvas.yview_scroll(direction, "units")
                row = self._measurement_row_at(self._measurement_pointer_y)
                self._set_measurement_selection(self._measurement_anchor, row)
            if direction and self.canvas.yview() != before:
                self._measurement_scroll_after = self.after(60, self._measurement_autoscroll)

        def _measurement_release(self, _event):
            self._stop_measurement_drag()
            # Tk's Entry class must also cancel its own horizontal-selection repeat timer.
            return None

        def _measurement_extend_key(self, row, direction):
            bounds = self._measurement_bounds()
            anchor = self._measurement_anchor if bounds else row
            end = self._measurement_end if bounds else row
            self._set_measurement_selection(anchor, end + direction)
            self._clear_measurement_text_selection()
            self.scroll_to_row(self._measurement_end)
            return "break"

        def measurement_selection_text(self):
            bounds = self._measurement_bounds()
            if bounds is None:
                return None
            lo, hi = bounds
            # Include empty rows and a final record separator, including a trailing empty cell.
            return "\r\n".join(
                SIUnits.format_measurement(step.measured_raw, step.nominal_raw)
                for step in self._steps[lo:hi + 1]
            ) + "\r\n"

        def _copy_measurements(self, row=None, force_cells=False):
            bounds = self._measurement_bounds()
            if row is None:
                row = self.selected_row_idx
            if not 0 <= row < len(self._row_widgets):
                return "break"
            if not force_cells and (bounds is None or bounds[0] == bounds[1]):
                if self._row_widgets[row]["entries"][9].selection_present():
                    return None  # Let Tk copy a substring selected inside one cell.
            if bounds is None:
                self._set_measurement_selection(row, row)
            text = self.measurement_selection_text()
            if text is not None:
                self.clipboard_clear()
                self.clipboard_append(text)
            return "break"

        def _measurement_context(self, event, row):
            self._stop_measurement_drag()
            if not self._measurement_contains(row):
                self._set_measurement_selection(row, row)
            self.selected_row_idx, self.selected_col_idx = row, self.MEASUREMENT_COLUMN
            self._row_widgets[row]["entries"][9].focus_set()
            try:
                self._measurement_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._measurement_menu.grab_release()
            return "break"

        def _paste_measurements(self, text, start_row):
            values = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if values and values[-1] == "":
                values.pop()
            if any("\t" in value for value in values):
                return False  # Only one result column; never reinterpret results as output rows.
            for offset, value in enumerate(values):
                row = start_row + offset
                if not 0 <= row < len(self._row_widgets):
                    break
                self._row_widgets[row]["measurement_var"].set(value)
                self._refresh_measurement_value(row)
            return True


        def _build_grid_skeleton(self) -> None:
            self.header_canvas = tk.Canvas(self, height=30, bg="#E2E8F0", highlightthickness=0)
            self.header_frame = tk.Frame(self.header_canvas, bg="#94A3B8")
            self.header_window = self.header_canvas.create_window((0, 0), window=self.header_frame, anchor="nw")
            self.header_canvas.pack(fill="x", side="top")

            for c_idx, (title, width_px, _align, is_editable) in enumerate(self.COL_SPECS):
                hdr_bg = "#DBEAFE" if is_editable else "#E2E8F0"
                hdr_fg = "#1E3A8A" if is_editable else "#0F172A"
                cell_f = tk.Frame(self.header_frame, width=width_px, height=29, bg=hdr_bg, highlightbackground="#94A3B8", highlightthickness=1)
                cell_f.pack_propagate(False)
                cell_f.grid(row=0, column=self._display_column(c_idx), sticky="nsew")
                tk.Label(
                    cell_f,
                    text=title,
                    bg=hdr_bg,
                    fg=hdr_fg,
                    font=("Malgun Gothic", 9, "bold"),
                ).pack(fill="both", expand=True)

            body_container = tk.Frame(self, bg="#FFFFFF")
            body_container.pack(fill="both", expand=True)

            self.canvas = tk.Canvas(body_container, bg="#FFFFFF", highlightthickness=0)
            self.vsb = ttk.Scrollbar(body_container, orient="vertical", command=self.canvas.yview)
            self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self._on_xscroll)
            self.canvas.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)

            self.vsb.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.hsb.pack(side="bottom", fill="x")

            self.cells_frame = tk.Frame(self.canvas, bg="#94A3B8")
            self.canvas_window = self.canvas.create_window((0, 0), window=self.cells_frame, anchor="nw")

            self.cells_frame.bind("<Configure>", self._on_frame_configure)
            self.header_frame.grid_columnconfigure(7, weight=1)
            self.cells_frame.grid_columnconfigure(7, weight=1)
            self.canvas.bind("<Configure>", self._resize_columns)
            self.canvas.bind("<Enter>", lambda _e: self._bind_mousewheel(True))
            self.canvas.bind("<Leave>", lambda _e: self._bind_mousewheel(False))

        def _on_xscroll(self, *args) -> None:
            self.canvas.xview(*args)
            self.header_canvas.xview(*args)

        def set_command_visibility(self, visible):
            self.show_commands = bool(visible)
            self._apply_optional_columns()

        def _resize_columns(self, _event=None):
            base = sum(spec[1] for index, spec in enumerate(self.COL_SPECS)
                       if (index != 7 or self.show_commands) and (index != 9 or self.show_measurements))
            width = max(base, self.canvas.winfo_width())
            self.canvas.itemconfigure(self.canvas_window, width=width)
            self.header_canvas.itemconfigure(self.header_window, width=width)

        def _on_frame_configure(self, _event: Optional[tk.Event] = None) -> None:
            bbox = self.canvas.bbox("all")
            if bbox:
                self.canvas.configure(scrollregion=bbox)
                self.header_canvas.configure(scrollregion=(0, 0, bbox[2], 30))

        def _bind_mousewheel(self, bind_flag: bool) -> None:
            if bind_flag:
                self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
                self.canvas.bind_all("<Button-4>", self._on_mousewheel)
                self.canvas.bind_all("<Button-5>", self._on_mousewheel)
            else:
                self.canvas.unbind_all("<MouseWheel>")
                self.canvas.unbind_all("<Button-4>")
                self.canvas.unbind_all("<Button-5>")

        def _on_mousewheel(self, event: tk.Event) -> None:
            if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
                self.canvas.yview_scroll(-2, "units")
            elif getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
                self.canvas.yview_scroll(2, "units")

        def render_steps(self, steps: List[SequenceStep], active_idx: int = -1) -> None:
            if self._steps is not steps or len(self._row_widgets) != len(steps):
                self._reset_measurement_selection()
            self._steps = steps
            while len(self._row_widgets) > len(steps):
                rw = self._row_widgets.pop()
                for w in rw["frames"]:
                    w.destroy()

            while len(self._row_widgets) < len(steps):
                r_idx = len(self._row_widgets)
                self._create_row_widgets(r_idx)

            for r_idx, st in enumerate(steps):
                self._update_single_row_visual(r_idx, st, is_active=(r_idx == active_idx))

            self.set_command_visibility(self.show_commands)
            self.cells_frame.update_idletasks()
            self._on_frame_configure()
            if 0 <= active_idx < len(steps):
                self.scroll_to_row(active_idx)

        def _create_row_widgets(self, r_idx: int) -> None:
            row_frames: List[tk.Frame] = []
            entries: Dict[int, tk.Entry] = {}
            labels: Dict[int, tk.Label] = {}

            for c_idx, (_title, width_px, align, is_editable) in enumerate(self.COL_SPECS):
                cell_box = tk.Frame(
                    self.cells_frame,
                    width=width_px,
                    height=28,
                    bg="#FFFFFF",
                    highlightbackground="#CBD5E1",
                    highlightthickness=1,
                )
                cell_box.pack_propagate(False)
                cell_box.grid(row=r_idx, column=self._display_column(c_idx), sticky="nsew")
                row_frames.append(cell_box)

                if c_idx == 0:
                    lbl = tk.Label(cell_box, text=str(r_idx + 1), bg="#F1F5F9", fg="#334155", font=("Consolas", 9, "bold"), cursor="hand2")
                    lbl.pack(fill="both", expand=True)
                    lbl.bind("<Button-1>", lambda _e, r=r_idx: self._select_row(r))
                    labels[c_idx] = lbl

                elif c_idx == 1:
                    btn_bar = tk.Frame(cell_box, bg="#F8FAFC")
                    btn_bar.pack(fill="both", expand=True)
                    btn_go = tk.Button(
                        btn_bar,
                        text="시작",
                        bg="#F1F5F9",
                        fg="#475569",
                        font=("Malgun Gothic", 8, "bold"),
                        relief="flat",
                        padx=3,
                        cursor="hand2",
                        command=lambda r=r_idx: self.on_row_start_callback(r),
                    )
                    btn_go.pack(side="left", fill="both", expand=True, padx=(1, 1), pady=1)

                    btn_bp = tk.Button(
                        btn_bar,
                        text="🛑",
                        bg="#F1F5F9",
                        fg="#64748B",
                        font=("Malgun Gothic", 8),
                        relief="flat",
                        width=2,
                        cursor="hand2",
                        command=lambda r=r_idx: self.on_row_breakpoint_callback(r),
                    )
                    btn_bp.pack(side="right", fill="y", padx=(0, 1), pady=1)
                    labels[10] = btn_go
                    labels[11] = btn_bp

                elif is_editable:
                    justify_map = {"center": "center", "e": "right", "w": "left"}
                    ent = tk.Entry(
                        cell_box,
                        font=("Consolas", 10),
                        bg="#FFFFFF",
                        fg="#0F172A",
                        relief="flat",
                        justify=justify_map.get(align, "center"),
                        insertbackground="#0284C7",
                    )
                    ent.pack(fill="both", expand=True, padx=3, pady=2)
                    ent.bind("<FocusIn>", lambda _e, r=r_idx, c=c_idx: self._on_cell_focus(r, c))
                    ent.bind("<Return>", lambda _e, r=r_idx, c=c_idx: self._commit_and_move(r, c, dr=1, dc=0))
                    ent.bind("<Down>", lambda _e, r=r_idx, c=c_idx: self._commit_and_move(r, c, dr=1, dc=0))
                    ent.bind("<Up>", lambda _e, r=r_idx, c=c_idx: self._commit_and_move(r, c, dr=-1, dc=0))
                    ent.bind("<FocusOut>", lambda _e, r=r_idx, c=c_idx: self._commit_cell(r, c))
                    ent.bind("<Control-v>", lambda e, r=r_idx, c=c_idx: self._on_cell_paste_event(e, r, c))
                    ent.bind("<Control-V>", lambda e, r=r_idx, c=c_idx: self._on_cell_paste_event(e, r, c))
                    entries[c_idx] = ent

                else:
                    anchor_map = {"center": "center", "e": "e", "w": "w"}
                    lbl = tk.Label(
                        cell_box,
                        text="",
                        bg="#FFFFFF",
                        fg="#0F172A",
                        font=("Consolas", 9),
                        anchor=anchor_map.get(align, "w"),
                        padx=5,
                    )
                    lbl.pack(fill="both", expand=True)
                    lbl.bind("<Button-1>", lambda _e, r=r_idx: self._select_row(r))
                    labels[c_idx] = lbl

            measured = tk.StringVar(master=self, value="")
            entries[9].configure(textvariable=measured)
            self._row_widgets.append({"frames": row_frames, "entries": entries, "labels": labels, "measurement_var": measured})
            measured.trace_add("write", lambda *_args, r=r_idx: self._measurement_changed(r))
            self._bind_measurement_selection(r_idx)

        def _update_single_row_visual(self, r_idx: int, st: SequenceStep, is_active: bool = False) -> None:
            self._steps[r_idx] = st
            rw = self._row_widgets[r_idx]
            frames: List[tk.Frame] = rw["frames"]
            entries: Dict[int, tk.Entry] = rw["entries"]
            labels: Dict[int, tk.Label] = rw["labels"]

            if is_active or "측정 진행 중" in st.status:
                row_bg = "#DBEAFE"
                row_fg = "#000000"
                border_col = "#CA8A04"
                row_font = ("Consolas", 10, "bold")
            elif "중단됨" in st.status or "중단점 도달" in st.status:
                row_bg = "#FED7AA"
                row_fg = "#9A3412"
                border_col = "#EA580C"
                row_font = ("Consolas", 10, "bold")
            elif st.breakpoint:
                row_bg = "#FECACA"
                row_fg = "#991B1B"
                border_col = "#DC2626"
                row_font = ("Consolas", 10, "bold")
            elif st.status == "✓ 완료":
                row_bg = "#DCFCE7"
                row_fg = "#166534"
                border_col = "#86EFAC"
                row_font = ("Consolas", 10)
            else:
                row_bg = "#FFFFFF" if r_idx % 2 == 0 else "#F8FAFC"
                row_fg = "#0F172A"
                border_col = "#CBD5E1"
                row_font = ("Consolas", 10)

            for f in frames:
                f.configure(bg=row_bg, highlightbackground=border_col)

            num_txt = f"▶{st.step_no}" if is_active else (f"🛑{st.step_no}" if st.breakpoint else str(st.step_no))
            num_bg = "#FACC15" if is_active else ("#FCA5A5" if st.breakpoint else "#E2E8F0")
            labels[0].configure(text=num_txt, bg=num_bg, fg="#0F172A")
            labels[11].configure(bg="#DC2626" if st.breakpoint else "#E2E8F0", fg="#FFFFFF" if st.breakpoint else "#475569")

            col_vals = {
                2: st.vi_cond_raw,
                3: st.freq_cond_raw,
                4: st.pf_cond_raw,
                5: st.nominal_raw,
            }
            self._refresh_measurement_value(r_idx, preserve_entry=True)
            entries[9].configure(bg=row_bg, fg=row_fg, font=row_font)
            rw["measurement_base_colors"] = (row_bg, row_fg, border_col)
            self._paint_measurement_cell(r_idx)
            for c_idx, val_str in col_vals.items():
                ent = entries[c_idx]
                ent.configure(bg=row_bg, fg=row_fg, font=row_font)
                if ent.get() != val_str:
                    ent.delete(0, "end")
                    ent.insert(0, val_str)

            labels[6].configure(text=st.actual_out_desc, bg=row_bg, fg="#0369A1" if not is_active else "#000000", font=row_font)
            labels[7].configure(text=st.scpi_cmd, bg=row_bg, fg="#334155" if not is_active else "#000000", font=row_font)
            labels[8].configure(text=st.status, bg=row_bg, fg=row_fg, font=("Malgun Gothic", 9, "bold"))

        def _select_row(self, r_idx: int) -> None:
            self.selected_row_idx = r_idx
            self.on_row_select_callback(r_idx)

        def _on_cell_focus(self, r_idx: int, c_idx: int) -> None:
            self.selected_row_idx = r_idx
            self.selected_col_idx = c_idx
            if c_idx != self.MEASUREMENT_COLUMN:
                self.on_row_select_callback(r_idx)
            elif not self._measurement_drag_active and not self._measurement_contains(r_idx):
                self._set_measurement_selection(r_idx, r_idx)

        def _commit_cell(self, r_idx: int, c_idx: int) -> None:
            if c_idx == self.MEASUREMENT_COLUMN:
                if 0 <= r_idx < len(self._steps):
                    self._refresh_measurement_value(r_idx)
                return
            if not (0 <= r_idx < len(self._row_widgets)):
                return
            entries = self._row_widgets[r_idx]["entries"]
            vi_c = entries[2].get().strip()
            fq_c = entries[3].get().strip()
            pf_c = entries[4].get().strip()
            nom_c = entries[5].get().strip()
            self.on_cell_edited_callback(r_idx, vi_c, fq_c, pf_c, nom_c)

        def _commit_and_move(self, r_idx: int, c_idx: int, dr: int, dc: int) -> None:
            self._commit_cell(r_idx, c_idx)
            target_r = max(0, min(len(self._row_widgets) - 1, r_idx + dr))
            target_c = c_idx + dc
            if c_idx == self.MEASUREMENT_COLUMN:
                self._set_measurement_selection(target_r, target_r)
            if target_c in self._row_widgets[target_r]["entries"]:
                ent = self._row_widgets[target_r]["entries"][target_c]
                ent.focus_set()
                ent.select_range(0, "end")
                self.scroll_to_row(target_r)
            return "break"

        def _on_cell_paste_event(self, _event: tk.Event, r_idx: int, c_idx: int) -> str:
            try:
                clip_text = self.clipboard_get()
            except Exception:
                return "break"

            if not clip_text or not clip_text.strip():
                return "break"

            if c_idx == self.MEASUREMENT_COLUMN:
                if not self._paste_measurements(clip_text, r_idx):
                    self.bell()
                return "break"
            self.on_paste_text_callback(clip_text, start_row_idx=r_idx, start_col_idx=c_idx)
            return "break"

        def scroll_to_row(self, r_idx: int) -> None:
            total = len(self._row_widgets)
            if total <= 0:
                return
            frac = max(0.0, min(1.0, (r_idx - 3) / max(1, total)))
            self.canvas.yview_moveto(frac)


# =====================================================================
# 7. 메인 GUI 애플리케이션 (Tkinter / TTK)
# =====================================================================
if HAS_TKINTER:
    class Fluke5560AControllerApp(tk.Tk):
        """Fluke 5560A 통합 원격 제어 GUI 프로그램 v6.10.4"""

        def __init__(self) -> None:
            super().__init__()
            self.title("Fluke 교정기 통합 제어 프로그램 v6.10.4 (USB / LAN / RS232 / GPIB)")
            self.geometry("1280x930")
            self.minsize(1140, 840)
            self.configure(bg="#F4F6F9")

            self.conn_mgr = Fluke5560AConnectionManager()
            self.is_operating = None

            self.seq_steps: List[SequenceStep] = []
            self.seq_current_idx: int = -1
            self.seq_paused_idx: int = -1
            self.seq_running: bool = False
            self.seq_after_id: Optional[str] = None
            self._seq_dut_run = False
            self._seq_dut_pending = None
            self._seq_dut_reading = False
            self._seq_dut_resume_required = False
            self._seq_close_standby = False
            self._standby_in_progress = False
            self.adapters = AutoNetworkAligner.list_ethernet_interfaces()

            self._setup_styles()
            self._build_ui()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._refresh_oper_badge()
            self._load_sample_into_sheet(SAMPLE_POWER_TABLE, silent=True)

            self._log(f"통신 기록 자동 저장: {self.conn_mgr.log_path}")
            self._log(f"실행 Python: {sys.executable} | PyVISA={HAS_PYVISA} | pyserial={HAS_PYSERIAL}")
            local_ips = self.conn_mgr.get_local_pc_ipv4_list()
            pc_ip_info = ", ".join(local_ips) if local_ips else "감지 안 됨"
            self._log(
                f"시스템 준비 완료 (현재 내 PC IP: {pc_ip_info}). "
                f"장비의 현재 IP와 실제 랜카드를 선택한 뒤 [LAN 진단]으로 확인하세요."
            )

        def _setup_styles(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            self.option_add("*Font", ("Malgun Gothic", 9))
            style.configure("TFrame", background="#F5F7FA")
            style.configure("Card.TFrame", background="white", borderwidth=0)
            style.configure("TLabel", background="white", foreground="#334155", font=("Malgun Gothic", 9))
            style.configure("SubHeader.TLabel", background="white", foreground="#0F172A", font=("Malgun Gothic", 11, "bold"))
            style.configure("TButton", font=("Malgun Gothic", 9), padding=(10, 5), background="#FFFFFF", foreground="#334155", bordercolor="#CBD5E1")
            style.map("TButton", background=[("active", "#F1F5F9")], foreground=[("disabled", "#94A3B8")])
            style.configure("Primary.TButton", background="#2563EB", foreground="white", bordercolor="#2563EB")
            style.map("Primary.TButton", background=[("disabled", "#CBD5E1"), ("active", "#1D4ED8")], foreground=[("disabled", "#64748B"), ("!disabled", "white")])
            style.configure("Stop.TButton", background="#B91C1C", foreground="white", bordercolor="#B91C1C")
            style.map("Stop.TButton", background=[("active", "#991B1B")], foreground=[("!disabled", "white")])
            style.configure("TCheckbutton", background="white", foreground="#334155", font=("Malgun Gothic", 9))
            style.map("TCheckbutton", background=[("active", "white")])
            style.configure("TEntry", padding=4)
            style.configure("TCombobox", padding=4)
            style.configure("TNotebook", background="#F5F7FA", borderwidth=0)
            style.configure("TNotebook.Tab", padding=(22, 9), background="#F5F7FA", foreground="#64748B", font=("Malgun Gothic", 10))
            style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", "#1D4ED8")])
            style.configure("Horizontal.TProgressbar", troughcolor="#E2E8F0", background="#2563EB", borderwidth=0, thickness=5)

        def _build_dut_tab(self):
            self.dut_session = DUTSession()
            self._dut_results = queue.Queue()
            self._dut_busy = self._dut_disconnect_requested = self._dut_closing = False
            self._dut_poll_id = None
            self._dut_next_read = self._dut_received_monotonic = 0
            self._dut_old = False
            self._dut_candidates = {}
            self._dut_first_visit = True
            self._dut_modes = {
                "자동 · USB / LAN": "AUTO",
                "USB / GPIB · VISA": "VISA", "LAN · WT": "WT_LAN",
                "LAN · WT5000 TCP": "WT5000_TCP",
                "LAN · PPA": "PPA_LAN", "COM · USB / RS232": "SERIAL", "미리보기 · 가상 DUT": "DEMO",
            }
            self.var_dut_mode = tk.StringVar(value="자동 · USB / LAN")
            self.var_dut_address = tk.StringVar()
            self.var_dut_port = tk.StringVar(value="10001")
            self.var_dut_channel = tk.StringVar(value="1")
            self.var_dut_auto = tk.BooleanVar(value=False)
            self.var_dut_interval = tk.StringVar(value="2")
            self.var_dut_timeout = tk.StringVar(value="10")
            self.var_dut_eol = tk.StringVar(value="CRLF")
            self.var_dut_baud = tk.StringVar(value="9600")
            self.var_dut_bits = tk.StringVar(value="8")
            self.var_dut_parity = tk.StringVar(value="N")
            self.var_dut_stop = tk.StringVar(value="1")
            self.var_dut_flow = tk.StringVar(value="RTS/CTS")
            self.var_dut_status = tk.StringVar(value="연결 안 됨 · 캘리브레이터와 별도로 연결합니다.")
            self.var_dut_identity = tk.StringVar(value="USB 또는 LAN에 연결한 DUT를 찾습니다.")
            self.var_dut_note = tk.StringVar(value="WT310 계열, WT3000·WT3000E, WT5000, PPA 등 지원 장비의 채널별 측정값을 조회합니다.")
            self.var_dut_time = tk.StringVar(value="수신 기록 없음")
            self.var_dut_hint = tk.StringVar()
            self.dut_canvas = tk.Canvas(self.tab_dut, bg="white", highlightthickness=0)
            scrollbar = ttk.Scrollbar(self.tab_dut, orient="vertical", command=self.dut_canvas.yview)
            scrollbar.pack(side="right", fill="y")
            self.dut_canvas.configure(yscrollcommand=scrollbar.set)
            self.dut_canvas.pack(fill="both", expand=True)
            pad = ttk.Frame(self.dut_canvas, style="Card.TFrame", padding=16)
            self.dut_content = pad
            content_window = self.dut_canvas.create_window((0, 0), window=pad, anchor="nw")
            self.dut_canvas.bind("<Configure>", lambda event: self.dut_canvas.itemconfigure(content_window, width=event.width))
            pad.bind("<Configure>", lambda _event: self.dut_canvas.configure(scrollregion=self.dut_canvas.bbox("all")))
            ttk.Label(pad, text="DUT 연결", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
            bar = ttk.Frame(pad, style="Card.TFrame")
            bar.pack(fill="x")
            self.combo_dut_mode = self._ui_combo(bar, self.var_dut_mode, tuple(self._dut_modes), 21)
            self.combo_dut_mode.bind("<<ComboboxSelected>>", self._dut_mode_changed)
            self.combo_dut_address = ttk.Combobox(bar, textvariable=self.var_dut_address, width=40)
            self.combo_dut_address.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.btn_dut_scan = self._ui_button(bar, "자동 검색", self._dut_scan)
            self.btn_dut_connect = self._ui_button(bar, "연결", self._dut_connect, "Primary.TButton")
            self.btn_dut_disconnect = self._ui_button(bar, "해제", self._dut_disconnect)
            ttk.Label(pad, textvariable=self.var_dut_hint, foreground="#64748B", wraplength=940).pack(anchor="w", pady=(8, 4))
            self._ui_button(bar, "설정", self._dut_toggle_details)
            self.dut_details = ttk.Frame(pad, style="Card.TFrame")
            self._dut_details_open = False
            self._dut_config_widgets = []
            row = ttk.Frame(self.dut_details, style="Card.TFrame")
            row.pack(fill="x", pady=4)
            for title, variable, width in (("응답 대기(초)", self.var_dut_timeout, 5), ("PPA TCP 포트", self.var_dut_port, 6)):
                self._ui_label(row, title)
                self._dut_config_widgets.append(self._ui_entry(row, variable, width))
            self._ui_label(row, "줄 끝")
            self._dut_config_widgets.append(self._ui_combo(row, self.var_dut_eol, ("CRLF", "LF", "CR"), 6))
            row = ttk.Frame(self.dut_details, style="Card.TFrame")
            row.pack(fill="x", pady=4)
            for title, variable, options, width in (
                    ("속도", self.var_dut_baud, (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200), 8),
                    ("데이터", self.var_dut_bits, (7, 8), 3), ("패리티", self.var_dut_parity, ("N", "E", "O"), 3),
                    ("정지", self.var_dut_stop, (1, 2), 3), ("흐름", self.var_dut_flow, ("RTS/CTS", "NONE", "XON/XOFF"), 10)):
                self._ui_label(row, title)
                self._dut_config_widgets.append(self._ui_combo(row, variable, options, width))
            self.dut_status_frame = ttk.Frame(pad, style="Card.TFrame")
            self.dut_status_frame.pack(fill="x", pady=10)
            ttk.Label(self.dut_status_frame, textvariable=self.var_dut_identity, wraplength=940).pack(anchor="w")
            ttk.Label(self.dut_status_frame, textvariable=self.var_dut_status, foreground="#2563EB", wraplength=940).pack(anchor="w", pady=(4, 0))
            readbar = ttk.Frame(pad, style="Card.TFrame")
            readbar.pack(fill="x", pady=(0, 10))
            self._ui_label(readbar, "입력 채널")
            self.combo_dut_channel = self._ui_combo(readbar, self.var_dut_channel, (1, 2, 3), 3)
            self.combo_dut_channel.bind("<<ComboboxSelected>>", self._dut_channel_changed)
            self.btn_dut_read = self._ui_button(readbar, "한 번 읽기", lambda: self._dut_start("read"))
            self.check_dut_auto = ttk.Checkbutton(readbar, text="자동 조회", variable=self.var_dut_auto, command=self._dut_auto_changed)
            self.check_dut_auto.pack(side="left", padx=(6, 6))
            self._ui_entry(readbar, self.var_dut_interval, 4)
            self._ui_label(readbar, "초 간격")
            ttk.Label(readbar, textvariable=self.var_dut_time, foreground="#64748B").pack(side="right")
            self.dut_tree = ttk.Treeview(pad, columns=("metric", "value", "unit", "state"), show="headings", height=7, selectmode="browse")
            for key, title, width, anchor in (("metric", "측정 항목", 190, "w"), ("value", "DUT 측정값", 250, "e"),
                    ("unit", "단위", 70, "center"), ("state", "상태", 180, "w")):
                self.dut_tree.heading(key, text=title)
                self.dut_tree.column(key, width=width, minwidth=60, anchor=anchor)
            self.dut_tree.tag_configure("old", foreground="#64748B")
            for key, title, unit in DUT_METRICS:
                self.dut_tree.insert("", "end", iid=key, values=(title, "—", unit, "수신 전"))
            self.dut_tree.pack(fill="x")
            ttk.Label(pad, textvariable=self.var_dut_note, foreground="#64748B", wraplength=940).pack(anchor="w", pady=(10, 4))
            ttk.Label(pad, text="연결한 DUT의 채널을 선택하세요. 측정표에서 ‘DUT 자동 기록’을 켜면 각 행의 측정값이 자동 입력됩니다.\n"
                      "이 화면의 ‘한 번 읽기’와 ‘자동 조회’는 이 탭의 수신값만 갱신합니다.",
                      foreground="#64748B", wraplength=940).pack(anchor="w")
            self._dut_mode_changed()
            self._dut_sync_controls()
            def bind_wheel(widget):
                widget.bind("<MouseWheel>", self._dut_scroll)
                for child in widget.winfo_children():
                    bind_wheel(child)
            bind_wheel(self.dut_canvas)
            self._dut_poll_id = self.after(100, self._dut_poll)
            self.notebook.bind("<<NotebookTabChanged>>", self._dut_on_tab_open, add="+")

        def _dut_on_tab_open(self, _event=None):
            if not self._dut_first_visit or self.notebook.select() != str(self.tab_dut):
                return
            if (not self._dut_busy and not self._dut_closing and not self.dut_session.connected
                    and self._dut_modes[self.var_dut_mode.get()] == "AUTO"):
                self._dut_scan()

        def _dut_scroll(self, event):
            if self.dut_content.winfo_height() > self.dut_canvas.winfo_height():
                self.dut_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def _dut_mode_changed(self, _event=None):
            mode = self._dut_modes[self.var_dut_mode.get()]
            hints = {
                "AUTO": "USB 계측기와 같은 네트워크의 LAN DUT를 찾습니다. 한 대면 바로 연결합니다. 목록에 없으면 연결 방식을 골라 주소를 직접 입력하세요.",
                "VISA": "목록에서 USB/GPIB 리소스를 선택하세요. VISA 드라이버에 표시되는 장치만 조회합니다.",
                "WT_LAN": "WT의 IP 주소를 입력하세요. VXI-11과 VISA 드라이버가 필요합니다. WT3000 계열은 GPIB 또는 장착된 RS232를 선택하세요.",
                "WT5000_TCP": "소켓 통신을 지원하는 WT5000의 IP를 입력하세요. TCP 10002와 LF를 사용합니다. 본체 Command Type은 WT5000으로 설정하세요.",
                "PPA_LAN": "PPA의 IP 주소를 입력하세요 (기본 TCP 포트 10001). 본체 REMOTE 인터페이스도 LAN으로 선택하세요.",
                "SERIAL": "COM 포트를 선택하세요. USB가 COM으로 인식되는 PPA 또는 USB–RS232 변환기를 사용합니다. 속도·흐름 설정은 본체와 맞추세요.",
                "DEMO": "화면 확인용 고정 예제입니다. 실제 장비 연결이나 측정은 하지 않습니다.",
            }
            self.var_dut_hint.set(hints[mode])
            self._dut_candidates.clear()
            self.combo_dut_address.configure(values=())
            if _event is not None:
                self.var_dut_address.set("")
                self.var_dut_eol.set("LF" if mode in ("WT_LAN", "WT5000_TCP") else "CRLF")
            if mode == "PPA_LAN" and not self._dut_details_open:
                self._dut_toggle_details()
            self._dut_sync_controls()

        def _dut_toggle_details(self):
            self._dut_details_open = not self._dut_details_open
            if self._dut_details_open:
                self.dut_details.pack(fill="x", before=self.dut_status_frame)
            else:
                self.dut_details.pack_forget()

        def _dut_settings(self):
            settings = DUTSettings(mode=self._dut_modes[self.var_dut_mode.get()], address=self.var_dut_address.get().strip(),
                port=int(self.var_dut_port.get() or 0), timeout=float(self.var_dut_timeout.get()), eol=self.var_dut_eol.get(),
                baudrate=int(self.var_dut_baud.get()), data_bits=int(self.var_dut_bits.get()), parity=self.var_dut_parity.get(),
                stop_bits=int(self.var_dut_stop.get()), flow=self.var_dut_flow.get())
            settings.validate()
            return settings

        def _dut_scan(self):
            if self._dut_busy or self._dut_closing or self.dut_session.connected:
                return
            if self._dut_modes[self.var_dut_mode.get()] == "AUTO":
                if self.seq_running or self._io_busy:
                    self.var_dut_status.set("기준기 작업이 끝난 뒤 DUT 자동 검색을 시작하세요.")
                    return
                self._dut_first_visit = False
                self._dut_candidates.clear()
                self.combo_dut_address.configure(values=())
                self.var_dut_address.set("")
                self.var_dut_identity.set("USB 또는 LAN에 연결한 DUT를 찾습니다.")
                self._dut_start("discover")
            else:
                self._dut_start("scan")

        def _dut_connect(self):
            if self._dut_busy or self._dut_closing or self.dut_session.connected:
                return
            try:
                mode = self._dut_modes[self.var_dut_mode.get()]
                if mode == "AUTO":
                    candidate = self._dut_candidates.get(self.var_dut_address.get())
                    if candidate is None:
                        raise ValueError("자동 검색 결과에서 연결할 DUT를 선택하세요.")
                    if self.seq_running or self._io_busy:
                        raise ValueError("기준기 작업이 끝난 뒤 DUT를 연결하세요.")
                    settings = replace(candidate.settings, timeout=float(self.var_dut_timeout.get()))
                    settings.validate()
                else:
                    settings = self._dut_settings()
                info = self.conn_mgr.info
                target, used = settings.address.upper(), info.resource_name.upper()
                target_ip = _dut_discovery_resource_ip(target) if settings.mode == "VISA" else _dut_discovery_ipv4(target)
                used_ip = _dut_discovery_ipv4(info.discovered_ip) or _dut_discovery_resource_ip(used)
                target_usb = _dut_discovery_usb_key(target) if settings.mode == "VISA" else None
                if settings.mode != "DEMO" and info.connected and (
                        target == used or
                        (settings.mode == "SERIAL" and target == used.split(" / ")[0]) or
                        (target_ip is not None and target_ip == used_ip) or
                        (target_usb is not None and target_usb == _dut_discovery_usb_key(used))):
                    raise ValueError("현재 기준기로 연결한 주소입니다. 별도 DUT의 주소를 선택하세요.")
            except (ValueError, KeyError) as exc:
                self.var_dut_status.set(f"설정 확인: {exc}")
                return
            self._dut_clear_values("수신 전")
            self._dut_start("connect", settings)

        def _dut_show_candidates(self, result):
            candidates, diagnostics = result
            for line in diagnostics:
                self._log(f"[DUT 자동 검색] {line}")
            self._dut_candidates = {}
            for candidate in candidates:
                settings = candidate.settings
                if settings.mode == "VISA":
                    transport = "USB"
                    fields = candidate.identity.split(",")
                    address = (fields[2].strip() if len(fields) > 2 else "") or settings.address
                else:
                    transport = "LAN"
                    address = settings.address
                    if settings.mode in ("PPA_LAN", "WT5000_TCP"):
                        address += f":{settings.port}"
                label = f"{candidate.profile.model} · {transport} · {address}"
                if label in self._dut_candidates:
                    label = f"{label} · {settings.address}"
                self._dut_candidates[label] = candidate
            self.combo_dut_address.configure(values=tuple(self._dut_candidates))
            if len(self._dut_candidates) == 1:
                self.var_dut_address.set(next(iter(self._dut_candidates)))
                self._dut_connect()
            elif self._dut_candidates:
                self.var_dut_status.set(f"DUT {len(self._dut_candidates)}개 발견 · 장비를 선택하고 ‘선택 연결’을 누르세요.")
            else:
                self.var_dut_status.set(diagnostics[-1] if diagnostics else "연결할 DUT를 찾지 못했습니다.")

        def _dut_interval_seconds(self):
            interval = float(self.var_dut_interval.get())
            if not math.isfinite(interval) or not 1 <= interval <= 3600:
                raise ValueError("자동 조회 간격은 1~3600초로 입력하세요.")
            return interval

        def _dut_auto_changed(self):
            if self.var_dut_auto.get():
                try:
                    self._dut_interval_seconds()
                    if not self.dut_session.connected:
                        raise ValueError("DUT를 먼저 연결하세요.")
                except ValueError as exc:
                    self.var_dut_auto.set(False)
                    self.var_dut_status.set(str(exc))
            self._dut_next_read = 0

        def _dut_channel_changed(self, _event=None):
            self.dut_session.sample = None
            self._dut_clear_values("채널 변경 · 조회 대기")
            self._dut_next_read = 0

        def _dut_start(self, operation, settings=None):
            if self._sequence_dut_reserved() and operation not in ("sequence_read", "disconnect"):
                return
            if self._dut_busy or (self._dut_closing and operation != "disconnect"):
                return
            try:
                channel = int(self.var_dut_channel.get())
                mode = self._dut_modes[self.var_dut_mode.get()]
            except (ValueError, KeyError) as exc:
                self.var_dut_status.set(f"설정 확인: {exc}")
                return
            info = self.conn_mgr.info
            excluded_ip = info.discovered_ip or (self.var_lan_ip.get() if operation == "discover" else "")
            selected = self._dut_candidates.get(self.var_dut_address.get()) if mode == "AUTO" else None
            self._dut_busy = True
            self._seq_dut_reading = operation == "sequence_read"
            self.dut_session.cancel.clear()
            self._dut_mark_old("이전 수신값")
            self.var_dut_status.set({"connect": "DUT 연결 중…", "disconnect": "DUT 연결 해제 중…",
                                     "sequence_read": "새 DUT 측정값 대기 중…",
                                     "read": "조회 중… · 표시된 값은 이전 수신값입니다.", "scan": "연결 목록 조회 중…",
                                     "discover": "USB / LAN DUT 검색 중…"}[operation])
            self._dut_sync_controls()
            def run():
                result, error = None, None
                try:
                    if operation == "connect":
                        identity = self.dut_session.connect(settings)
                        if selected is not None and identity != selected.identity:
                            self.dut_session.disconnect()
                            raise ConnectionError("검색 때 확인한 장비와 다릅니다. 다시 자동 검색하세요.")
                        try:
                            defaults = self.dut_session.apply_measurement_defaults()
                        except Exception as exc:
                            self.dut_session.disconnect()
                            raise ConnectionError(f"DUT 기본 설정 적용 중 오류: {exc}") from exc
                        result = identity, defaults
                    elif operation == "discover":
                        result = discover_duts(self.dut_session.cancel, excluded_resource=info.resource_name,
                                               excluded_ip=excluded_ip)
                    elif operation == "disconnect":
                        self.dut_session.disconnect()
                    elif operation == "read":
                        result = self.dut_session.read(channel)
                    elif operation == "sequence_read":
                        result = self.dut_session.read(settings[0], include_dc=settings[1], fresh=True)
                    elif mode == "SERIAL":
                        if not HAS_PYSERIAL:
                            raise RuntimeError("COM 조회를 위한 pyserial을 불러올 수 없습니다.")
                        result = [item.device for item in serial.tools.list_ports.comports()]
                    else:
                        if not HAS_PYVISA:
                            raise RuntimeError(dependency_import_message("pyvisa", "PyVISA", PYVISA_IMPORT_ERROR))
                        manager = SharedVisaManager()
                        try:
                            result = [r for r in manager.list_resources() if r.upper().startswith(("USB", "GPIB", "TCPIP"))]
                        finally:
                            manager.close()
                except Exception as exc:
                    error = str(exc)
                finally:
                    if self.dut_session.cancel.is_set():
                        self.dut_session.disconnect()
                        result, error = None, "DUT 작업을 중지하고 연결을 해제했습니다."
                    if operation == "sequence_read":
                        result = (settings[2], result)
                    self._dut_results.put((operation, result, error))
            threading.Thread(target=run, name="DUT-IO", daemon=True).start()
            return True

        def _dut_disconnect(self):
            if self._sequence_dut_reserved():
                self._seq_dut_resume_required = True
                self._on_stby_clicked()
            self.var_dut_auto.set(False)
            if self._dut_busy:
                self._dut_disconnect_requested = True
                self.dut_session.cancel.set()
                self.var_dut_status.set("진행 중인 DUT 응답 종료 후 연결을 해제합니다…")
                self._dut_mark_old("연결 해제 중")
            else:
                self._dut_start("disconnect")

        def _dut_clear_values(self, state):
            self._dut_received_monotonic = 0
            self._dut_old = False
            self.var_dut_time.set("수신 기록 없음")
            for key, title, unit in DUT_METRICS:
                self.dut_tree.item(key, values=(title, "—", unit, state), tags=("old",))

        def _dut_mark_old(self, state):
            if not self._dut_received_monotonic:
                return
            self._dut_old = True
            for key, _, _ in DUT_METRICS:
                values = list(self.dut_tree.item(key, "values"))
                values[3] = state
                self.dut_tree.item(key, values=values, tags=("old",))

        def _dut_show_sample(self, sample):
            self._dut_received_monotonic = time.monotonic()
            self._dut_old = False
            modes = dict(zip(("voltage", "current"), sample.vi_modes)) if sample.vi_modes else {}
            for key, title, unit in DUT_METRICS:
                if key in modes:
                    title = title.removesuffix("RMS") + modes[key]
                value = sample.values[key]
                state = "범위 초과 / 값 없음" if value is None else "가상 예제" if sample.simulated else "수신됨"
                self.dut_tree.item(key, values=(title, "—" if value is None else f"{value:.10g}", unit, state), tags=())
            self.var_dut_time.set(f"{'가상' if sample.simulated else '수신'} {sample.received_at[11:]} · CH{sample.channel}")
            self.var_dut_note.set(sample.note)
            self.var_dut_status.set("미리보기 · 실제 측정값 아님" if sample.simulated else "조회 완료 · 실제 장비의 수신값")

        def _dut_sync_controls(self):
            locked = self._dut_busy or self.dut_session.connected or self._dut_closing
            for widget in [self.combo_dut_mode] + self._dut_config_widgets:
                widget.configure(state="disabled" if locked else "readonly" if isinstance(widget, ttk.Combobox) else "normal")
            mode = self._dut_modes[self.var_dut_mode.get()]
            auto = mode == "AUTO"
            self.combo_dut_address.configure(state="disabled" if locked or (auto and not self._dut_candidates)
                                            else "readonly" if auto else "normal")
            self.btn_dut_connect.configure(text="선택 연결" if auto else "연결",
                                           state="disabled" if locked or (auto and not self._dut_candidates) else "normal")
            self.btn_dut_scan.configure(text="자동 검색" if auto else "목록",
                                        style="Primary.TButton" if auto and not self._dut_candidates else "TButton",
                                        state="normal" if not locked and mode in ("AUTO", "VISA", "SERIAL") else "disabled")
            self.btn_dut_disconnect.configure(state="normal" if self._dut_busy or self.dut_session.connected else "disabled")
            reserved = self._sequence_dut_reserved()
            ready = not reserved and self.dut_session.connected and not self._dut_busy and not self._dut_closing
            self.btn_dut_read.configure(state="normal" if ready else "disabled")
            self.check_dut_auto.configure(state="normal" if not reserved and self.dut_session.connected and not self._dut_closing else "disabled")
            self.combo_dut_channel.configure(state="readonly" if ready else "disabled")

        def _dut_poll(self):
            try:
                operation, result, error = self._dut_results.get_nowait()
            except queue.Empty:
                pass
            else:
                self._dut_busy = False
                callback = None
                if operation == "sequence_read":
                    self._seq_dut_reading = False
                    callback, result = result
                if self._dut_disconnect_requested or self._dut_closing:
                    self._dut_disconnect_requested = False
                    self._dut_clear_values("연결 해제됨")
                    if self.dut_session.connected:
                        self._dut_start("disconnect")
                    else:
                        self.var_dut_status.set("DUT 연결 해제됨")
                elif error:
                    self.var_dut_auto.set(False)
                    self._dut_clear_values("조회 실패")
                    self.var_dut_status.set(f"DUT 오류: {error}")
                    self._log(f"[DUT {operation}] {error}")
                elif operation == "connect":
                    identity, defaults = result
                    self.var_dut_identity.set(identity)
                    self.var_dut_status.set("DUT 연결됨 · 측정표에서 DUT 자동 기록을 사용할 수 있습니다." if not self.dut_session.simulated
                                            else "가상 DUT 연결됨 · 화면에서 예제 값을 조회할 수 있습니다.")
                    channels = self.dut_session.input_channels or tuple(range(1, self.dut_session.profile.channels + 1))
                    self.combo_dut_channel.configure(values=channels)
                    self.var_dut_channel.set(str(channels[0]))
                    self._log(f"[DUT 연결] {identity}")
                    if defaults:
                        self.var_dut_note.set(f"연결 시 기본값 적용 · {defaults}")
                        self._log(f"[DUT 기본 설정] {defaults}")
                elif operation in ("read", "sequence_read"):
                    self._dut_show_sample(result)
                elif operation == "discover":
                    self._dut_show_candidates(result)
                elif operation == "scan":
                    self.combo_dut_address.configure(values=result)
                    if len(result) == 1:
                        self.var_dut_address.set(result[0])
                    self.var_dut_status.set(f"연결 목록 {len(result)}개 · 주소 칸에서 선택하세요." if result else
                        "장치가 목록에 없습니다. USB 드라이버·케이블을 확인하거나 LAN/COM 주소를 직접 입력하세요.")
                else:
                    self._dut_clear_values("연결 해제됨")
                    self.var_dut_status.set("DUT 연결 해제됨")
                if self.var_dut_auto.get():
                    try:
                        self._dut_next_read = time.monotonic() + self._dut_interval_seconds()
                    except ValueError as exc:
                        self.var_dut_auto.set(False)
                        self.var_dut_status.set(str(exc))
                self._dut_sync_controls()
                if callback:
                    callback(result, error)
            if self._dut_received_monotonic and not self._dut_old and time.monotonic() - self._dut_received_monotonic > 5:
                self._dut_mark_old("이전 수신값")
            if not self._sequence_dut_reserved() and not self._dut_closing and self.var_dut_auto.get() and not self._dut_busy and self.dut_session.connected and time.monotonic() >= self._dut_next_read:
                self._dut_start("read")
            self._dut_poll_id = self.after(100, self._dut_poll)

        def _dut_close_ready(self):
            self._dut_closing = True
            self.var_dut_auto.set(False)
            if self._dut_busy:
                self.dut_session.cancel.set()
                return False
            if self.dut_session.connected:
                self._dut_start("disconnect")
                return False
            if self._dut_poll_id is not None:
                self.after_cancel(self._dut_poll_id)
                self._dut_poll_id = None
            return True

        def _build_ui(self):
            self.configure(bg="#F5F7FA")
            width = min(1280, max(1060, self.winfo_screenwidth() - 80))
            height = min(900, max(740, self.winfo_screenheight() - 100))
            self.geometry(f"{width}x{height}")
            self.minsize(1060, 740)
            header = tk.Frame(self, bg="#F5F7FA", padx=20, pady=12)
            header.pack(fill="x")
            tk.Label(header, text="Fluke Controller", bg="#F5F7FA", fg="#0F172A",
                     font=("Segoe UI", 18, "bold")).pack(side="left")
            tk.Label(header, text="5560A · 5522A · 5520A · 5080A   /   v6.10.4", bg="#F5F7FA",
                     fg="#64748B", font=("Segoe UI", 9)).pack(side="left", padx=16)
            self.lbl_conn_badge = tk.Label(header, text="연결 안 됨", bg="#E2E8F0", fg="#64748B", padx=12, pady=5)
            self.lbl_conn_badge.pack(side="right")
            self._build_connection_panel()
            self._build_safety_bar()
            # Reserve the footer before the expanding work area.
            self._build_console_panel()
            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True, padx=16, pady=(8, 8))
            for name, title in (("sequence", "측정표"), ("voltage", "전압"), ("current", "전류"),
                                ("resistance", "저항"), ("power", "전력"), ("dut", "DUT 연결")):
                tab = ttk.Frame(self.notebook, style="Card.TFrame")
                setattr(self, "tab_" + name, tab)
                self.notebook.add(tab, text=title)
                getattr(self, "_build_" + name + "_tab")()
            self._polish_manual_tabs()
            self._sync_model_controls()
            self._sync_connection_view()

            self.bind("<F8>", lambda _e: self._on_seq_step_next())
            self.bind("<F9>", lambda _e: self._on_seq_toggle_breakpoint(self.excel_grid.selected_row_idx))

        def _build_connection_panel(self):
            card = tk.Frame(self, bg="white", padx=14, pady=10, highlightbackground="#E2E8F0", highlightthickness=1)
            card.pack(fill="x", padx=16)
            self._io_busy = False
            self._pending_standby = False
            self._io_results = queue.Queue()
            self._last_report = ""
            self._connection_buttons = []
            self._configured_model = "5560A"
            for name, value in (("model", "5560A"), ("conn_mode", "LAN"), ("lan_ip", "169.254.1.1"),
                                ("lan_port", "3490"), ("gpib_addr", "1"), ("timeout", "3"),
                                ("operation_timeout", "60"), ("eol", "CRLF"), ("visa_resource", ""),
                                ("adapter_choice", ""), ("serial_port", ""), ("baudrate", "9600"),
                                ("data_bits", "8"), ("parity", "N"), ("stop_bits", "1"),
                                ("flow_control", "XON/XOFF"), ("model_note", "")):
                setattr(self, "var_" + name, tk.StringVar(value=value))
            row = tk.Frame(card, bg="white")
            row.pack(fill="x")
            self._ui_label(row, "모델")
            self.combo_model = self._ui_combo(row, self.var_model, list(MODEL_PROFILES), 8)
            self.combo_model.bind("<<ComboboxSelected>>", lambda _e: self._on_model_changed())
            self._ui_label(row, "연결")
            self.combo_conn_mode = self._ui_combo(row, self.var_conn_mode, (), 9)
            actions = tk.Frame(row, bg="white")
            actions.pack(side="right")
            self.btn_connect = self._ui_button(actions, "연결", self._on_connect_clicked, "Primary.TButton")
            self.btn_disconnect = self._ui_button(actions, "해제", self._on_disconnect_clicked)
            self.btn_connection_details = self._ui_button(actions, "상세 설정 ▾", self._toggle_connection_details)
            self._connection_buttons.extend((self.btn_connect, self.btn_disconnect))
            self.connection_endpoints = {}
            for key in ("LAN", "RS232", "GPIB", "USB", "AUTO", "SIMULATION"):
                self.connection_endpoints[key] = tk.Frame(row, bg="white")
            endpoint = self.connection_endpoints["LAN"]
            self._ui_label(endpoint, "IP")
            self._ui_entry(endpoint, self.var_lan_ip, 16)
            self._ui_label(endpoint, "포트")
            self._ui_entry(endpoint, self.var_lan_port, 5)
            endpoint = self.connection_endpoints["RS232"]
            self._ui_label(endpoint, "COM")
            self.combo_serial_port = ttk.Combobox(endpoint, textvariable=self.var_serial_port, width=22)
            self.combo_serial_port.pack(side="left", padx=(0, 6))
            self._connection_buttons.append(self._ui_button(endpoint, "찾기", self._on_refresh_serial_ports))
            endpoint = self.connection_endpoints["GPIB"]
            self._ui_label(endpoint, "장비 주소")
            self._ui_entry(endpoint, self.var_gpib_addr, 4)
            self._ui_label(self.connection_endpoints["USB"], "후면 USB Device · USBTMC")
            self._ui_label(self.connection_endpoints["AUTO"], "상세 설정의 주소·포트로 검색")
            self._ui_label(self.connection_endpoints["SIMULATION"], "가상 장비 · 실제 출력 없음")

            self.connection_details = tk.Frame(card, bg="white", pady=8)
            self._connection_details_open = False
            ttk.Separator(self.connection_details).pack(fill="x", pady=(2, 10))
            common = tk.Frame(self.connection_details, bg="white")
            common.pack(fill="x", pady=(0, 7))
            self._ui_label(common, "조회 대기(초)")
            self._ui_entry(common, self.var_timeout, 5)
            self._ui_label(common, "출력 완료 대기(초)")
            self._ui_entry(common, self.var_operation_timeout, 5)
            self._ui_label(common, "줄 끝 문자")
            self._ui_combo(common, self.var_eol, ("CRLF", "LF", "CR"), 7)
            self._connection_buttons.append(self._ui_button(common, "시뮬레이션", self._on_sim_connect_clicked))
            self._connection_buttons.append(self._ui_button(common, "진단 저장", self._on_save_report))
            self.connection_detail_rows = {}
            for key in ("LAN", "RS232", "VISA", "AUTO"):
                self.connection_detail_rows[key] = tk.Frame(self.connection_details, bg="white")
            lan = self.connection_detail_rows["LAN"]
            self._ui_label(lan, "랜카드")
            self.combo_adapter = self._ui_combo(lan, self.var_adapter_choice, (), 32)
            self._connection_buttons.append(self._ui_button(lan, "새로고침", self._on_refresh_adapters))
            self._connection_buttons.append(self._ui_button(lan, "LAN 진단", self._on_lan_diagnose))
            serial_row = self.connection_detail_rows["RS232"]
            self.serial_settings_controls = []
            for title, name, values, width in (("속도", "baudrate", (9600,), 8), ("데이터", "data_bits", (7, 8), 3),
                    ("패리티", "parity", ("N", "E", "O"), 3), ("정지", "stop_bits", (1, 2), 3),
                    ("흐름", "flow_control", ("XON/XOFF", "NONE", "RTS/CTS"), 10)):
                self._ui_label(serial_row, title)
                self.serial_settings_controls.append(self._ui_combo(serial_row, getattr(self, "var_" + name), values, width))
            visa = self.connection_detail_rows["VISA"]
            self._ui_label(visa, "VISA 리소스 (선택)")
            self._ui_entry(visa, self.var_visa_resource, 40)
            self._ui_label(visa, "USB–GPIB는 GPIB, USB–RS232는 RS232 선택")
            auto = self.connection_detail_rows["AUTO"]
            for title, name, width in (("IP", "lan_ip", 16), ("포트", "lan_port", 5),
                                       ("COM", "serial_port", 12), ("GPIB 주소", "gpib_addr", 4)):
                self._ui_label(auto, title)
                self._ui_entry(auto, getattr(self, "var_" + name), width)
            ttk.Label(self.connection_details, textvariable=self.var_model_note, foreground="#64748B").pack(side="bottom", anchor="w", pady=(8, 0))
            self.var_idn_display = tk.StringVar(value="모델과 연결 방식을 선택하세요.")
            self.connection_status = tk.Label(card, textvariable=self.var_idn_display, bg="white", fg="#64748B",
                                              anchor="w", justify="left", wraplength=1100)
            self.connection_status.pack(fill="x", pady=(7, 0))
            card.bind("<Configure>", lambda e: self.connection_status.configure(wraplength=max(400, e.width - 32)))
            self._set_adapter_choices(self.adapters)
            self.var_conn_mode.trace_add("write", lambda *_: self._sync_connection_view())
            self._sync_model_controls()
            self._sync_connection_view()
            self._poll_after_id = self.after(100, self._poll_io_result)

        def _set_adapter_choices(self, adapters):
            current = self.var_adapter_choice.get().split(":", 1)[0]
            choices = [f"{a['idx']}: {a['name']} [{a['state']}]" for a in adapters]
            choices.append("자동: Windows 경로 사용")
            self.combo_adapter.configure(values=choices)
            self.var_adapter_choice.set(next((v for v in choices if v.split(":", 1)[0] == current), choices[0]))

        def _get_selected_adapter_id(self):
            value = self.var_adapter_choice.get().split(":", 1)[0]
            return value if value.isdigit() else ""

        def _connection_arguments(self):
            timeout = float(self.var_timeout.get())
            if not math.isfinite(timeout) or not 0.2 <= timeout <= 60:
                raise ValueError("제한 시간은 0.2~60초 사이여야 합니다.")
            operation_timeout = self._get_operation_timeout()
            mode = self.var_conn_mode.get()
            return dict(preferred_mode=mode, lan_ip=self.var_lan_ip.get(), model=self._selected_model(),
                        lan_port=int(self.var_lan_port.get()) if mode in ("AUTO", "LAN") else 3490,
                        gpib_addr=int(self.var_gpib_addr.get()) if mode in ("AUTO", "GPIB") else 1,
                        timeout_sec=timeout, eol=self.var_eol.get(), adapter_idx=self._get_selected_adapter_id(),
                        visa_resource=self.var_visa_resource.get(), operation_timeout_sec=operation_timeout,
                        serial_port=self.var_serial_port.get().split(" | ", 1)[0].strip(),
                        baudrate=int(self.var_baudrate.get()), data_bits=int(self.var_data_bits.get()),
                        parity=self.var_parity.get(), stop_bits=int(self.var_stop_bits.get()),
                        flow_control=self.var_flow_control.get())

        def _get_operation_timeout(self):
            value = float(self.var_operation_timeout.get())
            if not math.isfinite(value) or not 1 <= value <= 300:
                raise ValueError("출력 완료 대기는 1~300초 사이여야 합니다.")
            return value

        def _start_io(self, label, operation, complete, on_error=None, stop_sequence=True, *, standby=False):
            if (self._seq_close_standby or self._dut_closing) and not standby:
                return False
            if stop_sequence and self._sequence_dut_reserved():
                self._log('DUT 기록을 중단한 뒤 다른 교정기 명령을 실행하세요.')
                return False
            if self._io_busy:
                return False
            if stop_sequence:
                self._on_seq_stop_auto(silent=True)
            self._io_busy = True
            self._standby_in_progress = standby
            self._refresh_connection_controls()
            self.conn_mgr.cancel_event.clear()
            self.conn_mgr.last_error = ""
            for button in self._connection_buttons:
                button.configure(state="disabled")
            self.var_idn_display.set(label)
            self._log(label)
            def worker():
                try:
                    self._io_results.put((complete, operation(), None, on_error))
                except Exception as exc:
                    self._io_results.put((complete, None, exc, on_error))
            threading.Thread(target=worker, daemon=True).start()
            return True

        def _poll_io_result(self):
            while True:
                try:
                    event = self.conn_mgr.events.get_nowait()
                except queue.Empty:
                    break
                self._log(event)
                if event.startswith("[출력 안정화 대기]"):
                    self.var_idn_display.set(event)
            try:
                complete, value, error, on_error = self._io_results.get_nowait()
            except queue.Empty:
                pass
            else:
                self._io_busy = False
                self._standby_in_progress = False
                for button in self._connection_buttons:
                    button.configure(state="normal")
                if error:
                    self.is_operating = False if isinstance(error, InstrumentCommandError) else None
                    self._update_connection_ui(self.conn_mgr.info)
                    self._refresh_oper_badge()
                    self._last_report = self.conn_mgr.last_error or str(error)
                    self._log(f"[오류] {self._last_report}")
                    if on_error:
                        on_error(error)
                    elif not isinstance(error, OperationCancelled):
                        self._show_report("통신 안내", self._last_report)
                else:
                    complete(value)
                if self._pending_standby:
                    self._pending_standby = False
                    if self.conn_mgr.info.connected:
                        self._on_stby_clicked()
            if not self._io_busy and not self.conn_mgr.info.connected:
                if self.is_operating is not None:
                    self.is_operating = None
                    self._refresh_oper_badge()
                self.lbl_conn_badge.configure(text="연결 안 됨", bg="#E2E8F0", fg="#64748B")
            self._refresh_connection_controls()
            self._refresh_connection_controls()
            self._poll_after_id = self.after(100, self._poll_io_result)

        def _on_refresh_adapters(self):
            def done(adapters):
                self.adapters = adapters
                self._set_adapter_choices(adapters)
                self.var_idn_display.set(AutoNetworkAligner.describe())
            self._start_io("실제 랜카드의 연결 상태를 확인하고 있습니다…",
                           lambda: AutoNetworkAligner.list_ethernet_interfaces(refresh=True), done)

        def _on_lan_diagnose(self):
            try:
                args = self._connection_arguments()
                if "LAN" not in MODEL_PROFILES[self._selected_model()].interfaces:
                    raise ValueError("선택한 모델에는 LAN 포트가 없습니다. RS232 또는 GPIB를 사용하세요.")
                def diagnose():
                    self.conn_mgr.expected_model = args['model']
                    self.conn_mgr.profile = MODEL_PROFILES[args['model']]
                    return self.conn_mgr.diagnose_lan(args['lan_ip'], args['lan_port'], args['timeout_sec'],
                                                      eol=args['eol'], adapter_idx=args['adapter_idx'])
                def done(report):
                    self._last_report = report
                    self.var_idn_display.set(self.conn_mgr.last_error)
                    self._log(report)
                    self._show_report("LAN 진단 결과", report)
                self._start_io("LAN 진단 중: PC 주소 → TCP 연결 → 선택 모델 식별 응답…", diagnose, done)
            except Exception as exc:
                messagebox.showerror("입력 확인", str(exc))

        def _show_report(self, title, report):
            window = tk.Toplevel(self)
            window.title(title)
            window.geometry("830x560")
            body = tk.Text(window, wrap="word", font=("Malgun Gothic", 10), padx=12, pady=12)
            body.pack(fill="both", expand=True)
            body.insert("1.0", report)
            body.configure(state="disabled")
            ttk.Button(window, text="닫기", command=window.destroy).pack(pady=8)

        def _on_save_report(self):
            if not self._last_report:
                messagebox.showinfo("진단 저장", "LAN 진단을 먼저 실행하세요.")
                return
            filename = filedialog.asksaveasfilename(title="LAN 진단 저장", defaultextension=".txt",
                         initialfile="fluke_5560a_lan_diagnostic.txt", filetypes=[("Text", "*.txt")])
            if filename:
                with open(filename, "w", encoding="utf-8-sig") as output:
                    output.write(self._last_report)

        def _on_close(self):
            if self._sequence_dut_reserved() and self.conn_mgr.info.connected:
                self._seq_close_standby = True
                self._on_stby_clicked()
                self.after(200, self._on_close)
                return
            if self._io_busy:
                if not (self._seq_close_standby or self._standby_in_progress):
                    self.conn_mgr.request_abort()
                self.after(200, self._on_close)
                return
            self._on_seq_stop_auto(silent=True)
            if not self._dut_close_ready():
                self.after(200, self._on_close)
                return
            try:
                self.conn_mgr.disconnect()
            except Exception as exc:
                self.conn_mgr.record_event(f"[종료 확인 오류] {exc}")
            self.after_cancel(self._poll_after_id)
            self.destroy()


        def _build_safety_bar(self):
            bar = tk.Frame(self, bg="#F5F7FA", padx=2, pady=8)
            bar.pack(fill="x", padx=16)
            self.lbl_output_state = tk.Label(bar, text="출력 상태 미확인", bg="#E2E8F0", fg="#475569",
                                             padx=10, pady=6, font=("Malgun Gothic", 9, "bold"))
            self.lbl_output_state.pack(side="left", padx=(0, 10))
            self._ui_button(bar, "출력 켜기 · OPER", self._on_oper_clicked)
            self.btn_standby = self._ui_button(bar, "출력 끄기 · STBY", self._on_stby_clicked, "Stop.TButton")
            self._ui_button(bar, "장비 초기화", self._on_reset_clicked, side="right")
            self.var_active_cmd_summary = tk.StringVar(value="전송 명령 없음")

        def _build_sequence_tab(self):
            container = tk.Frame(self.tab_sequence, bg="white", padx=14, pady=12)
            container.pack(fill="both", expand=True)
            toolbar = tk.Frame(container, bg="white")
            toolbar.pack(fill="x", pady=(0, 10))
            self._ui_button(toolbar, "붙여넣기  Ctrl+V", lambda: self._on_paste_into_sheet_button(replace_all=False))
            self._ui_button(toolbar, "끝에 붙여넣기", lambda: self._on_paste_into_sheet_button(append=True))
            edit = ttk.Menubutton(toolbar, text="표 편집 ▾")
            edit.pack(side="left", padx=(0, 12))
            menu = tk.Menu(edit, tearoff=False)
            menu.add_command(label="전체 새로 붙여넣기", command=lambda: self._on_paste_into_sheet_button(replace_all=True))
            menu.add_command(label="빈 행 추가", command=self._on_add_blank_row)
            menu.add_command(label="시트 비우기", command=self._on_clear_sheet)
            menu.add_separator()
            samples = tk.Menu(menu, tearoff=False)
            for name, sample in (("AC 전력 · 63행", SAMPLE_POWER_TABLE), ("AC 전압", SAMPLE_ACV_TABLE),
                                 ("AC 전류", SAMPLE_ACI_TABLE), ("DC 전압", SAMPLE_DCV_TABLE)):
                samples.add_command(label=name, command=lambda value=sample: self._load_sample_into_sheet(value, silent=False))
            menu.add_cascade(label="예제 불러오기", menu=samples)
            edit.configure(menu=menu)
            self.var_seq_use_coil = tk.BooleanVar(value=False)
            ttk.Checkbutton(toolbar, text="턴코일", variable=self.var_seq_use_coil,
                            command=self._recalculate_all_grid_steps).pack(side="left", padx=(0, 6))
            self.var_seq_coil_turns = tk.StringVar(value="50")
            turns = self._ui_combo(toolbar, self.var_seq_coil_turns, ("1", "2", "10", "25", "50"), 5)
            turns.configure(state="normal")
            turns.bind("<<ComboboxSelected>>", lambda _e: self._recalculate_all_grid_steps())
            turns.bind("<KeyRelease>", lambda _e: self._recalculate_all_grid_steps())
            self._ui_label(toolbar, "턴")
            self.btn_sequence_details = self._ui_button(toolbar, "범위·그룹 설정 ▾", self._toggle_sequence_details, side="right")
            self.var_show_measurements = tk.BooleanVar(value=True)
            ttk.Checkbutton(toolbar, text="측정값 입력", variable=self.var_show_measurements,
                            command=lambda: self.excel_grid.set_measurement_visibility(self.var_show_measurements.get())).pack(side="right", padx=8)
            self.sequence_details = tk.Frame(container, bg="white", pady=8)
            self._sequence_details_open = False
            self.var_seq_start_row = tk.StringVar(value="1")
            self.var_seq_stop_row = tk.StringVar(value="63")
            self.var_seq_dwell_sec = tk.StringVar(value="3.0")
            self.var_seq_auto_oper = tk.BooleanVar(value=True)
            self.var_seq_dut_capture = tk.BooleanVar(value=True)
            range_row = tk.Frame(self.sequence_details, bg="white")
            range_row.pack(fill="x", pady=(0, 5))
            self._ui_label(range_row, "실행 범위")
            self._ui_entry(range_row, self.var_seq_start_row, 5)
            self._ui_label(range_row, "~")
            self._ui_entry(range_row, self.var_seq_stop_row, 5)
            self._ui_label(range_row, "행")
            self._ui_button(range_row, "선택 행 중단점 · F9", lambda: self._on_seq_toggle_breakpoint(self.excel_grid.selected_row_idx))
            self.var_show_commands = tk.BooleanVar(value=False)
            ttk.Checkbutton(range_row, text="명령어 열 표시", variable=self.var_show_commands,
                            command=lambda: self.excel_grid.set_command_visibility(self.var_show_commands.get())).pack(side="right")
            self._build_current_group_panel(container)
            status = tk.Frame(container, bg="#F8FAFC", padx=10, pady=8)
            status.pack(fill="x", pady=(4, 8))
            self.var_active_row_banner = tk.StringVar(value="측정값을 붙여넣고 시작하세요.")
            self.lbl_active_row_banner = tk.Label(status, textvariable=self.var_active_row_banner, bg="#F8FAFC", fg="#475569",
                                                  anchor="w", justify="left", wraplength=1000)
            self.lbl_active_row_banner.pack(fill="x")
            status.bind("<Configure>", lambda e: self.lbl_active_row_banner.configure(wraplength=max(400, e.width - 24)))
            self.progress_var = tk.DoubleVar(value=0)
            self.progress_bar = ttk.Progressbar(status, variable=self.progress_var, maximum=100)
            self.progress_bar.pack(fill="x", pady=(6, 0))
            controls = tk.Frame(container, bg="white", pady=4)
            controls.pack(side="bottom", fill="x")
            self._ui_button(controls, "시작", self._on_seq_start_from_selected_or_range, "Primary.TButton")
            self._ui_button(controls, "처음부터", lambda: self._on_seq_start_from_selected_or_range(all_rows=True))
            self._ui_button(controls, "일시정지", lambda: self._on_seq_stop_auto(silent=False))
            self._ui_button(controls, "재개", self._on_seq_resume_from_paused)
            self._ui_label(controls, "  유지")
            self._ui_entry(controls, self.var_seq_dwell_sec, 5)
            self._ui_label(controls, "초")
            ttk.Checkbutton(controls, text="자동 출력", variable=self.var_seq_auto_oper).pack(side="left", padx=6)
            ttk.Checkbutton(controls, text="DUT 자동 기록", variable=self.var_seq_dut_capture).pack(side="left", padx=6)
            self._ui_button(controls, "선택 행 출력", self._on_seq_output_selected, side="right")
            self._ui_button(controls, "다음 · F8", self._on_seq_step_next, side="right")
            self._ui_button(controls, "이전", self._on_seq_step_prev, side="right")
            self.excel_grid = ExcelSheetGrid(container,
                on_paste_text_callback=self._handle_excel_paste_at_cell, on_cell_edited_callback=self._handle_excel_cell_edited,
                on_row_start_callback=self._handle_row_direct_start, on_row_breakpoint_callback=self._on_seq_toggle_breakpoint,
                on_row_select_callback=self._handle_row_selected)
            self.excel_grid.pack(fill="both", expand=True, pady=(0, 8))

        def _get_coil_turns(self) -> float:
            try:
                t = float(self.var_seq_coil_turns.get())
                return t if t > 0 else 1.0
            except ValueError:
                return 1.0

        def _load_sample_into_sheet(self, sample_txt: str, silent: bool = False) -> None:
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            self._on_seq_stop_auto(silent=True)
            turns = self._get_coil_turns()
            steps = SmartClipboardSequenceParser.build_sequence_steps(
                model=self._selected_model(), raw_text=sample_txt,
                use_coil=self.var_seq_use_coil.get(),
                coil_turns=turns,
            )
            self.seq_steps = steps
            self._original_row_positions = None
            self.seq_current_idx = -1
            self.seq_paused_idx = -1
            self.var_seq_start_row.set("1")
            self.var_seq_stop_row.set(str(len(steps)) if steps else "1")
            self.progress_var.set(0.0)
            self.excel_grid.render_steps(self.seq_steps, active_idx=-1)
            self._refresh_current_group_preview()
            self.var_active_row_banner.set(
                f"{len(steps)}개 행 준비됨 · 셀을 선택하고 Ctrl+V로 측정값을 붙여넣을 수 있습니다."
            )
            self.lbl_active_row_banner.configure(bg="#F8FAFC", fg="#854D0E")
            if not silent:
                self._log(f"[엑셀 시트 로드] 총 {len(steps)}개 행이 엑셀 칸에 맞춰 등록되었습니다.")

        def _on_paste_into_sheet_button(self, replace_all: bool = False, *, append=False) -> None:
            if not (replace_all or append) and self.excel_grid.selected_col_idx == self.excel_grid.MEASUREMENT_COLUMN:
                try:
                    text = self.clipboard_get()
                except tk.TclError:
                    return
                self._handle_excel_paste_at_cell(text, self.excel_grid.selected_row_idx, self.excel_grid.MEASUREMENT_COLUMN)
                return
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            try:
                clip_text = self.clipboard_get()
            except Exception:
                messagebox.showinfo(
                    "클립보드 안내",
                    "현재 클립보드에 복사된 텍스트가 없습니다.\n엑셀에서 데이터를 복사(Ctrl+C)한 뒤 다시 눌러주세요.",
                )
                return

            if not clip_text or not clip_text.strip():
                return

            start_r = 0 if replace_all else self.excel_grid.selected_row_idx
            self._handle_excel_paste_at_cell(clip_text, start_row_idx=start_r,
                                             start_col_idx=2, replace_all=replace_all, append=append)

        def _handle_excel_paste_at_cell(self, clip_text: str, start_row_idx: int, start_col_idx: int, *, replace_all=False, append=False) -> None:
            if start_col_idx == self.excel_grid.MEASUREMENT_COLUMN:
                if not self.excel_grid._paste_measurements(clip_text, start_row_idx):
                    self._log("측정값은 한 열만 복사해서 붙여넣으세요.")
                return
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            self._on_seq_stop_auto(silent=True)
            lines = [ln for ln in clip_text.splitlines() if ln.strip()]
            if not lines:
                return

            turns = self._get_coil_turns()
            use_coil = self.var_seq_use_coil.get()

            if append:
                start_row_idx = len(self.seq_steps)
            parsed_rows: List[Tuple[str, str, str, str]] = []
            for line_no, ln in enumerate(lines, 1):
                try:
                    pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)
                    if append and pf is None and not SmartClipboardSequenceParser._is_header_line(ln):
                        raise ValueError(f"붙여넣은 {line_no}행의 출력 조건을 인식할 수 없습니다.")
                except ValueError as exc:
                    self._log(f"[붙여넣기 입력 오류] {exc}")
                    messagebox.showerror("붙여넣기 값 오류", str(exc))
                    return
                if pf is not None:
                    parsed_rows.append(pf)

            tabular = any("\t" in line for line in lines)
            if parsed_rows and (append or tabular or len(parsed_rows) == len(lines)):
                new_steps = []
                for offset, (vi_c, fq_c, pf_c, nom_c) in enumerate(parsed_rows):
                    target_idx = start_row_idx + offset
                    step_num = target_idx + 1
                    try:
                        new_step = SmartClipboardSequenceParser.build_Single_step_from_fields(
                            model=self._selected_model(), step_no=step_num,
                            vi_cond_raw=vi_c,
                            freq_cond_raw=fq_c,
                            pf_cond_raw=pf_c,
                            nominal_raw=nom_c,
                            use_coil=use_coil,
                            coil_turns=turns,
                        )
                    except Exception as e:
                        messagebox.showerror("붙여넣기 값 오류", f"{step_num}번 행 변환 오류:\n{e}")
                        return
                    new_steps.append(new_step)

                if replace_all or (not append and start_row_idx == 0 and len(new_steps) > 1):
                    self.seq_steps = []
                    self._original_row_positions = None
                    self.seq_current_idx = self.seq_paused_idx = -1
                    self.progress_var.set(0)
                self.seq_steps[start_row_idx:start_row_idx + len(new_steps)] = new_steps
                if append or replace_all:
                    self.var_seq_start_row.set("1")
                if append:
                    self.var_active_row_banner.set(f"{len(new_steps)}행 추가 · 실행 범위 1~{len(self.seq_steps)}행")
                invalid_rows = [str(step.step_no) for step in new_steps if not step.scpi_cmd]
                if invalid_rows:
                    text = "전력 조건과 명목값 확인 · " + ", ".join(invalid_rows) + "행"
                    self.var_active_row_banner.set(text)
                    self._log(text)
                for i, st in enumerate(self.seq_steps):
                    st.step_no = i + 1

                self.var_seq_stop_row.set(str(len(self.seq_steps)))
                self.excel_grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
                self._refresh_current_group_preview()
                self._log(
                    f"[엑셀 붙여넣기 완료] #{start_row_idx + 1}번 행부터 아래로 총 {len(parsed_rows)}개 행이 칸별로 자동 입력되었습니다."
                )
                return

            if tabular or replace_all or append:
                return
            for offset, ln in enumerate(lines):
                ln = ln.strip()
                target_idx = start_row_idx + offset
                while target_idx >= len(self.seq_steps):
                    dummy = SmartClipboardSequenceParser.build_Single_step_from_fields(
                        model=self._selected_model(), step_no=len(self.seq_steps) + 1,
                        vi_cond_raw="-",
                        freq_cond_raw="DC",
                        pf_cond_raw="-",
                        nominal_raw="1 V",
                        use_coil=use_coil,
                        coil_turns=turns,
                    )
                    self.seq_steps.append(dummy)

                st = self.seq_steps[target_idx]
                vi_c = ln if start_col_idx == 2 else st.vi_cond_raw
                fq_c = ln if start_col_idx == 3 else st.freq_cond_raw
                pf_c = ln if start_col_idx == 4 else st.pf_cond_raw
                nom_c = ln if start_col_idx == 5 else st.nominal_raw
                try:
                    self.seq_steps[target_idx] = SmartClipboardSequenceParser.build_Single_step_from_fields(
                        model=self._selected_model(), step_no=target_idx + 1,
                        vi_cond_raw=vi_c,
                        freq_cond_raw=fq_c,
                        pf_cond_raw=pf_c,
                        nominal_raw=nom_c,
                        use_coil=use_coil,
                        coil_turns=turns,
                        breakpoint=st.breakpoint,
                    )
                    self.seq_steps[target_idx].measured_raw = SIUnits.with_nominal_unit(st.measured_raw, st.nominal_raw)
                except Exception:
                    pass

            self.var_seq_stop_row.set(str(len(self.seq_steps)))
            self.excel_grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
            self._refresh_current_group_preview()
            self._log(f"[열 붙여넣기 완료] #{start_row_idx + 1}번 행부터 아래로 {len(lines)}개 셀을 붙여넣었습니다.")

        def _handle_excel_cell_edited(self, r_idx: int, vi_c: str, fq_c: str, pf_c: str, nom_c: str) -> None:
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                if 0 <= r_idx < len(self.seq_steps):
                    self.excel_grid._update_single_row_visual(r_idx, self.seq_steps[r_idx], is_active=(r_idx == self.seq_current_idx))
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            if not (0 <= r_idx < len(self.seq_steps)):
                return
            st = self.seq_steps[r_idx]
            if vi_c != st.vi_cond_raw and fq_c.strip().upper() in ("", "DC", "0", "0 HZ"):
                try:
                    dc = SmartClipboardSequenceParser.parse_dc_power_row(vi_c)
                except ValueError as exc:
                    invalid = SequenceStep(st.step_no, vi_c, fq_c, pf_c, nom_c, "ERROR", "", str(exc),
                                           "⚠ 설정 확인", st.breakpoint, st.measured_raw)
                    self.seq_steps[r_idx] = invalid
                    self.excel_grid._update_single_row_visual(r_idx, invalid)
                    self._refresh_current_group_preview()
                    self._log(f"[#{st.step_no}행 셀 입력 오류] {exc}")
                    return
                if dc:
                    nom_c, pf_c = dc[3], "1"
            if st.func_type == "PF":
                if pf_c != st.pf_cond_raw and nom_c == st.nominal_raw:
                    nom_c = pf_c
                elif nom_c != st.nominal_raw:
                    pf_c = nom_c
            if (vi_c, fq_c, pf_c, nom_c) == (st.vi_cond_raw, st.freq_cond_raw, st.pf_cond_raw, st.nominal_raw):
                return
            try:
                updated = SmartClipboardSequenceParser.build_Single_step_from_fields(
                    model=self._selected_model(), step_no=st.step_no,
                    vi_cond_raw=vi_c,
                    freq_cond_raw=fq_c,
                    pf_cond_raw=pf_c,
                    nominal_raw=nom_c,
                    use_coil=self.var_seq_use_coil.get(),
                    coil_turns=self._get_coil_turns(),
                    breakpoint=st.breakpoint,
                )
                updated.measured_raw = SIUnits.with_nominal_unit(st.measured_raw, st.nominal_raw)
                if updated.scpi_cmd and st.scpi_cmd:
                    updated.status = st.status
                self.seq_steps[r_idx] = updated
                self.excel_grid._update_single_row_visual(r_idx, updated, is_active=(r_idx == self.seq_current_idx))
                self._refresh_current_group_preview()
            except Exception as e:
                invalid = SequenceStep(st.step_no, vi_c, fq_c, pf_c, nom_c, "ERROR", "", str(e), "⚠ 설정 확인", st.breakpoint, st.measured_raw)
                self.seq_steps[r_idx] = invalid
                self.excel_grid._update_single_row_visual(r_idx, invalid)
                self._refresh_current_group_preview()
                self._log(f"[#{st.step_no}행 셀 입력 오류] {e}")

        def _on_add_blank_row(self) -> None:
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            new_no = len(self.seq_steps) + 1
            step = SmartClipboardSequenceParser.build_Single_step_from_fields(
                model=self._selected_model(), step_no=new_no,
                vi_cond_raw="-",
                freq_cond_raw="DC",
                pf_cond_raw="-",
                nominal_raw="1.0 V",
                use_coil=self.var_seq_use_coil.get(),
                coil_turns=self._get_coil_turns(),
            )
            self.seq_steps.append(step)
            self.var_seq_stop_row.set(str(len(self.seq_steps)))
            self.excel_grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
            self._refresh_current_group_preview()
            self.excel_grid.scroll_to_row(len(self.seq_steps) - 1)

        def _on_clear_sheet(self) -> None:
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            self._on_seq_stop_auto(silent=True)
            self.seq_steps = []
            self._original_row_positions = None
            self._on_add_blank_row()
            self._log("[시트 초기화] 시트를 비우고 1번 빈 행을 준비했습니다. 1번 행을 클릭하고 Ctrl+V를 눌러 붙여넣으세요.")

        def _handle_row_selected(self, r_idx: int) -> None:
            self.var_seq_start_row.set(str(r_idx + 1))

        def _handle_row_direct_start(self, r_idx: int) -> None:
            self.excel_grid.selected_row_idx = r_idx
            self.var_seq_start_row.set(str(r_idx + 1))
            self._on_seq_start_from_selected_or_range()

        def _on_seq_toggle_breakpoint(self, r_idx: int) -> None:
            if not (0 <= r_idx < len(self.seq_steps)):
                return
            st = self.seq_steps[r_idx]
            st.breakpoint = not st.breakpoint
            if st.breakpoint and st.status == "대기":
                st.status = "🛑 중단점"
            elif not st.breakpoint and st.status == "🛑 중단점":
                st.status = "대기"
            self.excel_grid._update_single_row_visual(r_idx, st, is_active=(r_idx == self.seq_current_idx))
            self._log(f"[중단점(Breakpoint)] #{st.step_no}번 행 중단점 {'설정됨' if st.breakpoint else '해제됨'}")

        def _recalculate_all_grid_steps(self):
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            rebuilt_steps = []
            for st in self.seq_steps:
                try:
                    rebuilt = SmartClipboardSequenceParser.build_Single_step_from_fields(
                        model=self._selected_model(), step_no=st.step_no, vi_cond_raw=st.vi_cond_raw,
                        freq_cond_raw=st.freq_cond_raw, pf_cond_raw=st.pf_cond_raw, nominal_raw=st.nominal_raw,
                        use_coil=self.var_seq_use_coil.get(), coil_turns=self._get_coil_turns(), breakpoint=st.breakpoint)
                except Exception as exc:
                    # Never retain a command built for the previous model/coil after an error.
                    rebuilt = SequenceStep(st.step_no, st.vi_cond_raw, st.freq_cond_raw, st.pf_cond_raw,
                                           st.nominal_raw, "ERROR", "", str(exc), "⚠ 설정 확인", st.breakpoint)
                    self._log(f"[#{st.step_no}행 설정 확인] {exc}")
                rebuilt.measured_raw = st.measured_raw
                rebuilt_steps.append(rebuilt)
            self.seq_steps = rebuilt_steps
            self.excel_grid.render_steps(self.seq_steps, active_idx=-1)
            self._refresh_current_group_preview()
            self._refresh_current_group_preview()

        _DUT_SEQUENCE_QUANTITIES = {
            "ACV": ("voltage", "V"), "DCV": ("dc_voltage", "V"),
            "ACI": ("current", "A"), "DCI": ("dc_current", "A"),
            "ACP": ("power", "W"), "DCP": ("power", "W"), "PF": ("pf", ""),
        }

        def _sequence_dut_reserved(self):
            return self._seq_dut_run or self._seq_dut_pending is not None

        def _sequence_dut_enabled(self):
            return (self.var_seq_dut_capture.get() and self.dut_session.connected
                    and not self.dut_session.simulated
                    and not self.conn_mgr.info.interface_type.startswith("SIMULATION"))

        def _sequence_dut_failure(self, index, step, exc):
            self._on_seq_stop_auto(silent=True)
            self.seq_paused_idx = index
            self._seq_dut_resume_required = True
            step.status = "⚠ DUT 기록 실패"
            if index < len(self.seq_steps) and self.seq_steps[index] is step:
                self.excel_grid._update_single_row_visual(index, step, is_active=False)
            self.var_active_row_banner.set(f"#{step.step_no}행 DUT 기록 중단 · {exc}")
            self.lbl_active_row_banner.configure(bg="#FECACA", fg="#991B1B")
            self._log(f"[#{step.step_no}행 DUT 기록 중단] {exc}")
            if self.conn_mgr.info.connected and self.is_operating is not False:
                self._on_stby_clicked()

        def _execute_sequence_step(self, index, on_complete=None, *, dwell_ms=None):
            if self._seq_dut_pending is not None or not (0 <= index < len(self.seq_steps)):
                return False
            step = self.seq_steps[index]
            try:
                if not self._step_in_current_group(step):
                    self._log(f"[#{index + 1}행 제외] 현재 그룹: {self._current_group_label()}")
                    return False
            except ValueError as exc:
                self._show_report("전류 그룹 확인", str(exc))
                return False
            if not self._ensure_connected_or_sim():
                return False
            if not step.scpi_cmd or step.func_type == "ERROR":
                self._show_report("행 설정 확인", f"#{step.step_no}행: {step.actual_out_desc}")
                return False
            self.seq_current_idx = self.seq_paused_idx = index

            quantity = self._DUT_SEQUENCE_QUANTITIES.get(step.func_type)
            linked = quantity is not None and (
                self._seq_dut_run if self.seq_running else self._sequence_dut_enabled()
            )
            token = None
            if linked:
                try:
                    if not self.dut_session.connected or self.dut_session.simulated:
                        raise ConnectionError("DUT 연결을 확인하고 같은 행부터 재개하세요.")
                    if not self.dut_session.supports_fresh_capture:
                        raise ValueError("이 DUT는 자동 기록을 지원하지 않습니다. 직접 측정하려면 DUT 자동 기록을 끄세요.")
                    if self._dut_busy:
                        raise RuntimeError("진행 중인 DUT 조회가 끝난 뒤 다시 시작하세요.")
                    channel = int(self.var_dut_channel.get())
                    if not 1 <= channel <= self.dut_session.profile.channels:
                        raise ValueError("DUT 입력 채널을 확인하세요.")
                    if dwell_ms is None:
                        dwell = float(self.var_seq_dwell_sec.get())
                        if not .2 <= dwell <= 86_400:
                            raise ValueError("유지시간은 0.2~86400초 사이여야 합니다.")
                        dwell_ms = int(dwell * 1000)
                except (ValueError, ConnectionError, RuntimeError, OverflowError) as exc:
                    self._sequence_dut_failure(index, step, exc)
                    return False
                identity = self.dut_session.identity
                calibrator = self.conn_mgr.info
                token = object()
                self._seq_dut_pending = token
                self._dut_sync_controls()

            step.status = "… 출력 안정화 대기"
            self.excel_grid._update_single_row_visual(index, step, is_active=True)
            self.excel_grid.scroll_to_row(index)
            self.var_active_row_banner.set(f"#{step.step_no}행 완료 응답 대기: {step.scpi_cmd}")
            self.lbl_active_row_banner.configure(bg="#FEF3C7", fg="#92400E")
            self._log(f"[#{step.step_no}행 시작] 조건=({step.vi_cond_raw}, {step.freq_cond_raw}, PF={step.pf_cond_raw}) | 명령={step.scpi_cmd}")

            def same_step():
                return index < len(self.seq_steps) and self.seq_steps[index] is step

            def current_capture():
                return self._seq_dut_pending is token and same_step()

            def publish_progress():
                plan = self._seq_active_indices
                self.progress_var.set((plan.index(index) + 1) / len(plan) * 100 if index in plan else 100)

            def failed(exc):
                if linked:
                    if current_capture():
                        self._sequence_dut_failure(index, step, exc)
                    return
                self.seq_running = False
                self.seq_after_id = None
                self._seq_dut_run = False
                self._dut_sync_controls()
                step.status = "⚠ 오류"
                if same_step():
                    self.excel_grid._update_single_row_visual(index, step, is_active=False)
                self.var_active_row_banner.set(f"#{step.step_no}행 중단 — {step.scpi_cmd} / 출력 상태는 상단과 본체에서 확인하세요.")
                self.lbl_active_row_banner.configure(bg="#FECACA", fg="#991B1B")
                self._log(f"[#{step.step_no}행 실행 오류] 명령={step.scpi_cmd} | {exc}")

            def record_after_oper_check(sample, error):
                if not current_capture():
                    return
                if error:
                    failed(error)
                    return
                if (sample is None or sample.simulated or sample.identity != identity or sample.channel != channel
                        or self.conn_mgr.info is not calibrator or not self.dut_session.connected):
                    failed("출력 또는 DUT 연결 상태가 변경됐습니다.")
                    return
                metric, unit = quantity
                if sample.vi_modes and metric in ("voltage", "current"):
                    mode = sample.vi_modes[0 if metric == "voltage" else 1]
                    if mode != "RMS":
                        failed("WT3000 계열의 해당 입력 측정 모드를 RMS로 설정하세요.")
                        return
                value = sample.values.get(metric)
                if value is None or not math.isfinite(value):
                    detail = ("해당 입력의 DC 측정 모드를 확인하세요." if metric.startswith("dc_")
                              else "DUT 입력 범위와 배선을 확인하세요.")
                    failed(f"유효한 DUT 측정값이 없습니다. {detail}")
                    return
                number = (sample.raw_values or {}).get(metric, f"{value:.16g}")
                raw = f"{number} {unit}" if unit else number

                def record(state):
                    if not current_capture():
                        return
                    if state not in ("0", "1"):
                        failed(f"교정기 출력 상태 확인 실패: {state}")
                        return
                    self.is_operating = state == "1"
                    self._refresh_oper_badge()
                    if not self.is_operating or self.conn_mgr.info is not calibrator:
                        failed("교정기의 OPER 상태를 확인하세요.")
                        return
                    step.measured_raw = raw
                    step.status = "✓ 완료 · DUT"
                    self.excel_grid._update_single_row_visual(index, step, is_active=True)
                    publish_progress()
                    result = SIUnits.format_measurement(raw, step.nominal_raw)
                    self.var_active_row_banner.set(f"#{step.step_no}행 DUT 자동 기록 · CH{channel} · {result}")
                    self._log(f"[#{step.step_no}행 DUT 자동 기록] CH{channel} {result}")
                    self._seq_dut_pending = None
                    self._seq_dut_resume_required = False
                    self._dut_sync_controls()
                    if on_complete:
                        on_complete()

                if not self._start_io(f"#{step.step_no}행 DUT 수신 후 OPER 상태 확인…",
                                      lambda: self.conn_mgr.query("OPER?").strip(), record,
                                      on_error=failed, stop_sequence=False):
                    failed("교정기 출력 상태 확인을 시작할 수 없습니다.")

            def capture_after_dwell():
                self.seq_after_id = None
                if not current_capture():
                    return
                if (self.is_operating is not True or self.conn_mgr.info is not calibrator
                        or not self.dut_session.connected or self.dut_session.identity != identity):
                    failed("교정기 출력 또는 DUT 연결을 확인하세요.")
                    return
                step.status = "… DUT 새 측정값 대기"
                self.excel_grid._update_single_row_visual(index, step, is_active=True)
                self.var_active_row_banner.set(f"#{step.step_no}행 DUT 새 측정값 대기 · CH{channel}")
                include_dc = step.func_type in ("DCV", "DCI", "DCP")
                if not self._dut_start("sequence_read", (channel, include_dc, record_after_oper_check)):
                    failed("DUT 조회를 시작할 수 없습니다.")

            def done(state):
                if linked and not current_capture():
                    return
                if not same_step():
                    self.seq_running = False
                    return
                for i, previous in enumerate(self.seq_steps):
                    if i != index and previous.status == "▶ 측정 진행 중":
                        previous.status = "✓ 완료"
                        self.excel_grid._update_single_row_visual(i, previous, is_active=False)
                if linked and not state:
                    failed("교정기의 OPER 상태를 확인하세요.")
                    return
                step.status = "… 유지시간 대기" if linked else "▶ 측정 진행 중"
                self.var_active_row_banner.set(f"[#{step.step_no}/{len(self.seq_steps)}행 출력 확인] {step.nominal_raw} → {step.actual_out_desc}")
                self.lbl_active_row_banner.configure(bg="#DBEAFE", fg="#0F172A")
                self.excel_grid._update_single_row_visual(index, step, is_active=True)
                self._log(f"[#{step.step_no}행 완료 응답 확인] 유지시간 시작")
                if linked:
                    self.seq_after_id = self.after(dwell_ms, capture_after_dwell)
                else:
                    publish_progress()
                    if on_complete:
                        on_complete()

            try:
                started = self._send_and_log(step.scpi_cmd, auto_oper=self.var_seq_auto_oper.get(),
                                             on_done=done, on_error=failed, stop_sequence=False,
                                             verify_setpoint=(step.func_type == "ACV"
                                                              and self.conn_mgr.profile.model == "5560A"))
                if linked and not started:
                    failed("교정기 출력 요청을 시작할 수 없습니다.")
                return started
            except Exception as exc:
                failed(exc)
                return False

        def _get_start_and_stop_indices(self) -> Tuple[int, int]:
            total = len(self.seq_steps)
            try:
                start_r = int(self.var_seq_start_row.get())
            except ValueError:
                start_r = 1
            try:
                stop_r = int(self.var_seq_stop_row.get())
            except ValueError:
                stop_r = total

            start_idx = max(0, min(total - 1, start_r - 1))
            stop_idx = max(start_idx, min(total - 1, stop_r - 1))
            return start_idx, stop_idx

        def _on_seq_start_from_selected_or_range(self, *, all_rows=False):
            if not self.seq_steps:
                self._log("먼저 시트에 측정값을 넣으세요.")
                return
            if all_rows:
                self.var_seq_start_row.set("1")
                self.var_seq_stop_row.set(str(len(self.seq_steps)))
            self._start_filtered_sequence(*self._get_start_and_stop_indices())

        def _on_seq_resume_from_paused(self):
            if (self._seq_dut_resume_required and self.var_seq_dut_capture.get()
                    and not self._sequence_dut_enabled()):
                self._log('DUT를 다시 연결한 뒤 중단된 행부터 재개하세요.')
                return
            if not self.seq_steps:
                return
            if self.seq_paused_idx < 0:
                self._on_seq_start_from_selected_or_range()
                return
            _, stop_idx = self._get_start_and_stop_indices()
            if self.seq_paused_idx > stop_idx:
                stop_idx = len(self.seq_steps) - 1
                self.var_seq_stop_row.set(str(stop_idx + 1))
            self._start_filtered_sequence(self.seq_paused_idx, stop_idx)

        def _run_next_auto_step(self, idx, stop_idx, dwell_ms):
            self.seq_after_id = None
            if not self.seq_running or self._io_busy:
                return
            try:
                while idx <= stop_idx and idx < len(self.seq_steps) and not self._step_in_current_group(self.seq_steps[idx]):
                    self._log(f"[#{idx + 1}행 건너뜀] 다른 전류 그룹")
                    idx += 1
            except ValueError as exc:
                self.seq_running = False
                if self._seq_dut_run:
                    self._on_stby_clicked()
                self._show_report("전류 그룹 확인", str(exc))
                return
            if idx > stop_idx or idx >= len(self.seq_steps):
                self.seq_running = False
                def finished(_state):
                    for r_i, step in enumerate(self.seq_steps):
                        if step.status == "▶ 측정 진행 중":
                            step.status = "✓ 완료"
                            self.excel_grid._update_single_row_visual(r_i, step, is_active=False)
                    self.progress_var.set(100)
                    self.var_active_row_banner.set(f"{self._current_group_label()} 완료 — STBY 확인 완료. 케이블 변경 후 다른 그룹을 선택하세요.")
                    self.lbl_active_row_banner.configure(bg="#DCFCE7", fg="#166534")
                self._on_seq_stop_auto(silent=True)
                self._send_and_log("STBY", on_done=finished)
                return
            linked = self._seq_dut_run and self.seq_steps[idx].func_type in self._DUT_SEQUENCE_QUANTITIES
            def completed():
                if not self.seq_running:
                    return
                if self.seq_steps[idx].breakpoint:
                    self.seq_steps[idx].status = "🛑 중단점 도달"
                    self.seq_running = False
                    self.seq_paused_idx = min(len(self.seq_steps) - 1, idx + 1)
                    self.var_active_row_banner.set(f"#{idx + 1}행 중단점 도달 — 다음 행부터 재개 가능")
                    self.excel_grid._update_single_row_visual(idx, self.seq_steps[idx], is_active=False)
                    if self._seq_dut_run:
                        self._seq_dut_resume_required = True
                        self._on_stby_clicked()
                    return
                self.seq_after_id = self.after(0 if linked else dwell_ms,
                    lambda: self._run_next_auto_step(idx + 1, stop_idx, dwell_ms))
            if not self._execute_sequence_step(idx, on_complete=completed, dwell_ms=dwell_ms):
                if self._seq_dut_run and self.conn_mgr.info.connected:
                    self._on_stby_clicked()
                else:
                    self._on_seq_stop_auto(silent=True)

        def _on_seq_stop_auto(self, silent: bool = False) -> None:
            had_linked_capture = self._sequence_dut_reserved()
            if self._seq_dut_pending is not None or (had_linked_capture and not silent):
                self._seq_dut_resume_required = True
            self._seq_dut_run = False
            self._seq_dut_pending = None
            if self._seq_dut_reading:
                self.dut_session.cancel.set()
            if self.seq_after_id is not None:
                try:
                    self.after_cancel(self.seq_after_id)
                except Exception:
                    pass
                self.seq_after_id = None
            was_running = self.seq_running
            self.seq_running = False
            if 0 <= self.seq_current_idx < len(self.seq_steps) and not silent:
                st = self.seq_steps[self.seq_current_idx]
                st.status = "⏸ 일시중단됨"
                self.seq_paused_idx = self.seq_current_idx
                self.var_active_row_banner.set(
                    f"⏸ [일시중단됨: #{st.step_no}번 행] ({st.vi_cond_raw} | {st.freq_cond_raw} | {st.nominal_raw}) — [⏯ 중단된 행부터 이어서 시작] 가능"
                )
                self.lbl_active_row_banner.configure(bg="#FED7AA", fg="#9A3412")
                self.excel_grid._update_single_row_visual(self.seq_current_idx, st, is_active=False)
            if was_running and not silent:
                self._log(f"[일시중단] #{self.seq_current_idx + 1}번 행에서 순차 실행을 중단했습니다.")
            if had_linked_capture:
                self._dut_sync_controls()
                if not silent and self.conn_mgr.info.connected:
                    self._on_stby_clicked()

        def _on_seq_step_next(self):
            if self._sequence_dut_reserved() or self._seq_dut_reading:
                self._log('DUT 기록을 중단하거나 현재 조회가 끝난 뒤 실행하세요.')
                return
            if self._io_busy:
                return
            self._on_seq_stop_auto(silent=True)
            try:
                index = next((i for i in range(self.seq_current_idx + 1, len(self.seq_steps))
                              if self._step_in_current_group(self.seq_steps[i])), None)
                if index is None:
                    self._log("현재 그룹의 마지막 행입니다. STBY를 요청합니다.")
                    if self.conn_mgr.info.connected:
                        self._on_stby_clicked()
                    return
                self._seq_active_indices = [index]
                self._execute_sequence_step(index)
            except ValueError as exc:
                self._show_report("전류 그룹 확인", str(exc))

        def _on_seq_step_prev(self):
            if self._sequence_dut_reserved() or self._seq_dut_reading:
                self._log('DUT 기록을 중단하거나 현재 조회가 끝난 뒤 실행하세요.')
                return
            if self._io_busy:
                return
            self._on_seq_stop_auto(silent=True)
            try:
                index = next((i for i in range(self.seq_current_idx - 1, -1, -1)
                              if self._step_in_current_group(self.seq_steps[i])), None)
                if index is not None:
                    self._seq_active_indices = [index]
                    self._execute_sequence_step(index)
            except ValueError as exc:
                self._show_report("전류 그룹 확인", str(exc))

        def _on_seq_output_selected(self) -> None:
            if self._sequence_dut_reserved() or self._seq_dut_reading:
                self._log('DUT 기록을 중단하거나 현재 조회가 끝난 뒤 실행하세요.')
                return
            self._on_seq_stop_auto(silent=True)
            idx = self.excel_grid.selected_row_idx
            if not (0 <= idx < len(self.seq_steps)):
                return
            self.var_seq_start_row.set(str(idx + 1))
            self._execute_sequence_step(idx)

        # -----------------------------------------------------------------
        # 탭 1~4: 개별 수동 설정 탭 (전압 / 전류+턴코일 / 저항 / 전력+턴코일)
        # -----------------------------------------------------------------
        def _build_voltage_tab(self) -> None:
            pad = tk.Frame(self.tab_voltage, bg="#FFFFFF", padx=20, pady=16)
            pad.pack(fill="both", expand=True)

            ttk.Label(pad, text="DC / AC 전압 개별 출력 설정 (최대 ±1020 V)", style="SubHeader.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))

            ttk.Label(pad, text="전압 모드:").grid(row=1, column=0, sticky="w", pady=6)
            self.var_v_mode = tk.StringVar(value="DCV")
            frm_vmode = ttk.Frame(pad)
            frm_vmode.grid(row=1, column=1, columnspan=3, sticky="w")
            ttk.Radiobutton(frm_vmode, text="DC 전압 (DCV, 0 Hz)", variable=self.var_v_mode, value="DCV", command=self._update_voltage_preview).pack(side="left", padx=(0, 16))
            ttk.Radiobutton(frm_vmode, text="AC 전압 (ACV, 주파수 지정)", variable=self.var_v_mode, value="ACV", command=self._update_voltage_preview).pack(side="left")

            ttk.Label(pad, text="출력 전압 값:").grid(row=2, column=0, sticky="w", pady=6)
            self.var_v_val = tk.StringVar(value="10.0")
            ent_v = ttk.Entry(pad, textvariable=self.var_v_val, width=18, font=("Consolas", 11))
            ent_v.grid(row=2, column=1, sticky="w", pady=6)
            ent_v.bind("<KeyRelease>", lambda _e: self._update_voltage_preview())

            self.var_v_unit = tk.StringVar(value="V")
            cmb_vunit = ttk.Combobox(pad, textvariable=self.var_v_unit, values=SIUnits.units("V"), width=8, state="readonly")
            cmb_vunit.grid(row=2, column=2, sticky="w", padx=8)
            cmb_vunit.bind("<<ComboboxSelected>>", lambda _e: self._update_voltage_preview())

            ttk.Label(pad, text="주파수 (AC 전용, Hz):").grid(row=3, column=0, sticky="w", pady=6)
            self.var_v_freq = tk.StringVar(value="60.0")
            ent_vf = ttk.Entry(pad, textvariable=self.var_v_freq, width=18, font=("Consolas", 11))
            ent_vf.grid(row=3, column=1, sticky="w", pady=6)
            ent_vf.bind("<KeyRelease>", lambda _e: self._update_voltage_preview())

            frm_vfreq_quick = ttk.Frame(pad)
            frm_vfreq_quick.grid(row=3, column=2, columnspan=2, sticky="w", padx=8)
            for fq in ("50", "60", "100", "400", "1000"):
                tk.Button(
                    frm_vfreq_quick,
                    text=f"{fq} Hz",
                    bg="#E2E8F0",
                    relief="flat",
                    padx=8,
                    command=lambda f=fq: (self.var_v_freq.set(f), self.var_v_mode.set("ACV"), self._update_voltage_preview()),
                ).pack(side="left", padx=2)

            self.var_v_preview = tk.StringVar(value="")
            tk.Label(pad, textvariable=self.var_v_preview, bg="#F8FAFC", fg="#0369A1", font=("Consolas", 11, "bold"), padx=12, pady=8).grid(row=4, column=0, columnspan=4, sticky="ew", pady=(14, 10))

            frm_vbtns = ttk.Frame(pad)
            frm_vbtns.grid(row=5, column=0, columnspan=4, sticky="w", pady=6)
            tk.Button(
                frm_vbtns,
                text="📤 전압 설정값 장비로 전송 (OUT)",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=8,
                command=lambda: self._apply_voltage(auto_oper=False),
            ).pack(side="left", padx=(0, 10))

            tk.Button(
                frm_vbtns,
                text="⚡ 전압 전송 후 즉시 출력 활성화 (OUT + OPER)",
                bg="#16A34A",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=8,
                command=lambda: self._apply_voltage(auto_oper=True),
            ).pack(side="left")

            self._update_voltage_preview()

        def _build_current_tab(self) -> None:
            pad = tk.Frame(self.tab_current, bg="#FFFFFF", padx=20, pady=14)
            pad.pack(fill="both", expand=True)

            ttk.Label(pad, text="DC / AC 전류 출력 및 클램프 미터 턴코일(Turn Coil) 배율 설정", style="SubHeader.TLabel").grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 10))

            ttk.Label(pad, text="전류 모드:").grid(row=1, column=0, sticky="w", pady=5)
            self.var_i_mode = tk.StringVar(value="DCI")
            frm_imode = ttk.Frame(pad)
            frm_imode.grid(row=1, column=1, columnspan=4, sticky="w")
            ttk.Radiobutton(frm_imode, text="DC 전류 (DCI, 0 Hz)", variable=self.var_i_mode, value="DCI", command=self._update_current_preview).pack(side="left", padx=(0, 16))
            ttk.Radiobutton(frm_imode, text="AC 전류 (ACI, 주파수 지정)", variable=self.var_i_mode, value="ACI", command=self._update_current_preview).pack(side="left")

            ttk.Label(pad, text="목표 전류 (클램프 인가 기준):").grid(row=2, column=0, sticky="w", pady=5)
            self.var_i_val = tk.StringVar(value="100.0")
            ent_i = ttk.Entry(pad, textvariable=self.var_i_val, width=16, font=("Consolas", 11))
            ent_i.grid(row=2, column=1, sticky="w", pady=5)
            ent_i.bind("<KeyRelease>", lambda _e: self._update_current_preview())

            self.var_i_unit = tk.StringVar(value="A")
            cmb_iunit = ttk.Combobox(pad, textvariable=self.var_i_unit, values=SIUnits.units("A"), width=8, state="readonly")
            cmb_iunit.grid(row=2, column=2, sticky="w", padx=6)
            cmb_iunit.bind("<<ComboboxSelected>>", lambda _e: self._update_current_preview())

            ttk.Label(pad, text="주파수 (AC 전용, Hz):").grid(row=3, column=0, sticky="w", pady=5)
            self.var_i_freq = tk.StringVar(value="100.0")
            ent_if = ttk.Entry(pad, textvariable=self.var_i_freq, width=16, font=("Consolas", 11))
            ent_if.grid(row=3, column=1, sticky="w", pady=5)
            ent_if.bind("<KeyRelease>", lambda _e: self._update_current_preview())

            coil_box = tk.LabelFrame(
                pad,
                text=" 🧲 클램프 미터 교정용 턴코일 (Turn Coil) 배율 설정 ",
                bg="#F8FAFC",
                fg="#0F172A",
                font=("Malgun Gothic", 10, "bold"),
                padx=14,
                pady=10,
            )
            coil_box.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(10, 10))

            self.var_i_use_coil = tk.BooleanVar(value=True)
            tk.Checkbutton(
                coil_box,
                text="턴코일(Turn Coil) 배율 환산 활성화 (체크 시: 장비 실제 출력 전류 = 목표 클램프 전류 ÷ 코일 배율 N)",
                variable=self.var_i_use_coil,
                bg="#F8FAFC",
                font=("Malgun Gothic", 9, "bold"),
                command=self._update_current_preview,
            ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))

            tk.Label(coil_box, text="턴코일 배율 (N Turns):", bg="#F8FAFC", font=("Malgun Gothic", 9, "bold")).grid(row=1, column=0, sticky="w")
            self.var_i_coil_turns = tk.StringVar(value="50")
            ent_turns = ttk.Entry(coil_box, textvariable=self.var_i_coil_turns, width=10, font=("Consolas", 11, "bold"))
            ent_turns.grid(row=1, column=1, sticky="w", padx=8)
            ent_turns.bind("<KeyRelease>", lambda _e: self._update_current_preview())

            tk.Label(coil_box, text="빠른 배율 선택 (직접 입력 가능):", bg="#F8FAFC").grid(row=1, column=2, sticky="w", padx=(10, 6))
            frm_coil_presets = tk.Frame(coil_box, bg="#F8FAFC")
            frm_coil_presets.grid(row=1, column=3, columnspan=3, sticky="w")
            for t_val, t_label in (("1", "1x (직결)"), ("2", "2-Turn (2x)"), ("10", "10-Turn (10x)"), ("25", "25-Turn (25x)"), ("50", "50-Turn (50x)")):
                tk.Button(
                    frm_coil_presets,
                    text=t_label,
                    bg="#E0F2FE",
                    fg="#0369A1",
                    font=("Malgun Gothic", 9, "bold"),
                    relief="flat",
                    padx=8,
                    pady=2,
                    command=lambda v=t_val: (self.var_i_use_coil.set(v != "1"), self.var_i_coil_turns.set(v), self._update_current_preview()),
                ).pack(side="left", padx=3)

            tk.Label(coil_box, text="출력 단자(CUR_POST):", bg="#F8FAFC").grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.var_i_post = tk.StringVar(value="AUTO")
            cmb_post = ttk.Combobox(coil_box, textvariable=self.var_i_post, values=["AUTO", "AUX", "A20"], width=10, state="readonly")
            self.combo_current_post = cmb_post
            cmb_post.configure(values=["AUTO"])
            cmb_post.grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))
            cmb_post.bind("<<ComboboxSelected>>", lambda _e: self._update_current_preview())

            self.var_i_preview = tk.StringVar(value="")
            tk.Label(
                pad,
                textvariable=self.var_i_preview,
                bg="#EFF6FF",
                fg="#1D4ED8",
                font=("Consolas", 10, "bold"),
                justify="left",
                padx=12,
                pady=8,
            ).grid(row=5, column=0, columnspan=5, sticky="ew", pady=(6, 10))

            frm_ibtns = ttk.Frame(pad)
            frm_ibtns.grid(row=6, column=0, columnspan=5, sticky="w")
            tk.Button(
                frm_ibtns,
                text="📤 전류 설정값 장비로 전송 (OUT)",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=8,
                command=lambda: self._apply_current(auto_oper=False),
            ).pack(side="left", padx=(0, 10))

            tk.Button(
                frm_ibtns,
                text="⚡ 전류 전송 후 즉시 출력 활성화 (OUT + OPER)",
                bg="#16A34A",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=8,
                command=lambda: self._apply_current(auto_oper=True),
            ).pack(side="left")

            self._update_current_preview()

        def _build_resistance_tab(self) -> None:
            pad = tk.Frame(self.tab_resistance, bg="#FFFFFF", padx=20, pady=16)
            pad.pack(fill="both", expand=True)

            self.var_resistance_title = tk.StringVar(value="저항 출력 및 리드 보상 (0 Ω ~ 1100 MΩ)")
            ttk.Label(pad, textvariable=self.var_resistance_title, style="SubHeader.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))

            ttk.Label(pad, text="출력 저항 값:").grid(row=1, column=0, sticky="w", pady=6)
            self.var_r_val = tk.StringVar(value="1.0")
            ent_r = ttk.Entry(pad, textvariable=self.var_r_val, width=18, font=("Consolas", 11))
            ent_r.grid(row=1, column=1, sticky="w", pady=6)
            ent_r.bind("<KeyRelease>", lambda _e: self._update_resistance_preview())

            self.var_r_unit = tk.StringVar(value="kOHM")
            cmb_runit = ttk.Combobox(pad, textvariable=self.var_r_unit, values=SIUnits.units("OHM"), width=10, state="readonly")
            cmb_runit.grid(row=1, column=2, sticky="w", padx=8)
            cmb_runit.bind("<<ComboboxSelected>>", lambda _e: self._update_resistance_preview())

            ttk.Label(pad, text="와이어 리드 보상 (ZCOMP):").grid(row=2, column=0, sticky="w", pady=6)
            self.var_r_zcomp = tk.StringVar(value="WIRE4")
            frm_zcomp = ttk.Frame(pad)
            frm_zcomp.grid(row=2, column=1, columnspan=3, sticky="w")
            ttk.Radiobutton(frm_zcomp, text="4-Wire 보상 (WIRE4)", variable=self.var_r_zcomp, value="WIRE4", command=self._update_resistance_preview).pack(side="left", padx=(0, 14))
            ttk.Radiobutton(frm_zcomp, text="2-Wire 보상 (WIRE2)", variable=self.var_r_zcomp, value="WIRE2", command=self._update_resistance_preview).pack(side="left", padx=(0, 14))
            ttk.Radiobutton(frm_zcomp, text="보상 없음 (NONE)", variable=self.var_r_zcomp, value="NONE", command=self._update_resistance_preview).pack(side="left")

            self.var_r_preview = tk.StringVar(value="")
            tk.Label(pad, textvariable=self.var_r_preview, bg="#F8FAFC", fg="#0369A1", font=("Consolas", 11, "bold"), padx=12, pady=8).grid(row=3, column=0, columnspan=4, sticky="ew", pady=(14, 10))

            frm_rbtns = ttk.Frame(pad)
            frm_rbtns.grid(row=4, column=0, columnspan=4, sticky="w", pady=6)
            tk.Button(
                frm_rbtns,
                text="📤 저항 설정값 장비로 전송 (OUT + ZCOMP)",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=8,
                command=lambda: self._apply_resistance(auto_oper=False),
            ).pack(side="left", padx=(0, 10))

            tk.Button(
                frm_rbtns,
                text="⚡ 저항 전송 후 즉시 출력 활성화 (OUT + OPER)",
                bg="#16A34A",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=8,
                command=lambda: self._apply_resistance(auto_oper=True),
            ).pack(side="left")

            self._update_resistance_preview()

        def _build_power_tab(self) -> None:
            pad = tk.Frame(self.tab_power, bg="#FFFFFF", padx=20, pady=12)
            pad.pack(fill="both", expand=True)

            ttk.Label(pad, text="DC / AC 전력 동시 출력 (Dual Output) 및 클램프형 전력계 턴코일 연동", style="SubHeader.TLabel").grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))

            ttk.Label(pad, text="전력 모드:").grid(row=1, column=0, sticky="w", pady=4)
            self.var_p_mode = tk.StringVar(value="ACP")
            frm_pmode = ttk.Frame(pad)
            frm_pmode.grid(row=1, column=1, columnspan=5, sticky="w")
            ttk.Radiobutton(frm_pmode, text="AC 전력 (ACP, 주파수/역률/위상 포함)", variable=self.var_p_mode, value="ACP", command=self._update_power_preview).pack(side="left", padx=(0, 16))
            ttk.Radiobutton(frm_pmode, text="DC 전력 (DCP, 0 Hz)", variable=self.var_p_mode, value="DCP", command=self._update_power_preview).pack(side="left")

            ttk.Label(pad, text="전력 설정 방식:").grid(row=2, column=0, sticky="w", pady=4)
            self.var_p_calc_method = tk.StringVar(value="PW")
            frm_pcalc = ttk.Frame(pad)
            frm_pcalc.grid(row=2, column=1, columnspan=5, sticky="w")
            ttk.Radiobutton(frm_pcalc, text="목표 유효전력(W 계열) + 기준 전압(V) 입력 (전류 자동 역산)", variable=self.var_p_calc_method, value="PW", command=self._update_power_preview).pack(side="left", padx=(0, 16))
            ttk.Radiobutton(frm_pcalc, text="전압(V) + 목표 전류(A) 직접 입력", variable=self.var_p_calc_method, value="VI", command=self._update_power_preview).pack(side="left")

            ttk.Label(pad, text="기준 전압 (V):").grid(row=3, column=0, sticky="w", pady=4)
            self.var_p_volt = tk.StringVar(value="120.0")
            ent_pv = ttk.Entry(pad, textvariable=self.var_p_volt, width=14, font=("Consolas", 11))
            ent_pv.grid(row=3, column=1, sticky="w", pady=4)
            ent_pv.bind("<KeyRelease>", lambda _e: self._update_power_preview())

            self.lbl_p_second_input = ttk.Label(pad, text="목표 전력 / 전류 값:")
            self.lbl_p_second_input.grid(row=3, column=2, sticky="w", padx=(12, 4))
            self.var_p_val = tk.StringVar(value="60.0")
            ent_pval = ttk.Entry(pad, textvariable=self.var_p_val, width=14, font=("Consolas", 11))
            ent_pval.grid(row=3, column=3, sticky="w", pady=4)
            ent_pval.bind("<KeyRelease>", lambda _e: self._update_power_preview())

            self.var_p_unit = tk.StringVar(value="W")
            self.cmb_p_unit = ttk.Combobox(pad, textvariable=self.var_p_unit, values=SIUnits.units("W") + ("A",), width=8, state="readonly")
            self.cmb_p_unit.grid(row=3, column=4, sticky="w", padx=6)
            self.cmb_p_unit.bind("<<ComboboxSelected>>", lambda _e: self._update_power_preview())

            ttk.Label(pad, text="AC 주파수 (Hz):").grid(row=4, column=0, sticky="w", pady=4)
            self.var_p_freq = tk.StringVar(value="50.0")
            ent_pfreq = ttk.Entry(pad, textvariable=self.var_p_freq, width=14, font=("Consolas", 11))
            ent_pfreq.grid(row=4, column=1, sticky="w", pady=4)
            ent_pfreq.bind("<KeyRelease>", lambda _e: self._update_power_preview())

            ttk.Label(pad, text="역률/위상 지정:").grid(row=4, column=2, sticky="w", padx=(12, 4))
            self.var_p_phase_mode = tk.StringVar(value="DPF")
            cmb_phmode = ttk.Combobox(pad, textvariable=self.var_p_phase_mode, values=["DPF", "PHASE"], width=8, state="readonly")
            cmb_phmode.grid(row=4, column=3, sticky="w")
            cmb_phmode.bind("<<ComboboxSelected>>", lambda _e: self._update_power_preview())

            frm_pf_detail = ttk.Frame(pad)
            frm_pf_detail.grid(row=5, column=0, columnspan=6, sticky="w", pady=4)
            ttk.Label(frm_pf_detail, text="역률(DPF, 0~1.0):").pack(side="left", padx=(0, 4))
            self.var_p_dpf = tk.StringVar(value="1.00")
            ent_dpf = ttk.Entry(frm_pf_detail, textvariable=self.var_p_dpf, width=8, font=("Consolas", 10))
            ent_dpf.pack(side="left", padx=(0, 8))
            ent_dpf.bind("<KeyRelease>", lambda _e: self._update_power_preview())

            self.var_p_lead_lag = tk.StringVar(value="LEAD")
            cmb_ll = ttk.Combobox(frm_pf_detail, textvariable=self.var_p_lead_lag, values=["LEAD", "LAG"], width=7, state="readonly")
            cmb_ll.pack(side="left", padx=(0, 16))
            cmb_ll.bind("<<ComboboxSelected>>", lambda _e: self._update_power_preview())

            ttk.Label(frm_pf_detail, text="또는 위상각(PHASE, °):").pack(side="left", padx=(0, 4))
            self.var_p_phase_deg = tk.StringVar(value="0.0")
            ent_pdeg = ttk.Entry(frm_pf_detail, textvariable=self.var_p_phase_deg, width=8, font=("Consolas", 10))
            ent_pdeg.pack(side="left")
            ent_pdeg.bind("<KeyRelease>", lambda _e: self._update_power_preview())

            pcoil_box = tk.LabelFrame(
                pad,
                text=" 🧲 클램프형 전력계(Clamp Power Meter) 교정용 턴코일 배율 연동 ",
                bg="#F8FAFC",
                fg="#0F172A",
                font=("Malgun Gothic", 9, "bold"),
                padx=12,
                pady=8,
            )
            pcoil_box.grid(row=6, column=0, columnspan=6, sticky="ew", pady=(8, 8))

            self.var_p_use_coil = tk.BooleanVar(value=False)
            tk.Checkbutton(
                pcoil_box,
                text="전류 채널에 턴코일(Turn Coil) 배율 적용",
                variable=self.var_p_use_coil,
                bg="#F8FAFC",
                font=("Malgun Gothic", 9, "bold"),
                command=self._update_power_preview,
            ).pack(side="left", padx=(0, 12))

            tk.Label(pcoil_box, text="턴코일 배율 (N):", bg="#F8FAFC").pack(side="left", padx=(0, 4))
            self.var_p_coil_turns = tk.StringVar(value="50")
            ent_pturns = ttk.Entry(pcoil_box, textvariable=self.var_p_coil_turns, width=8, font=("Consolas", 10, "bold"))
            ent_pturns.pack(side="left", padx=(0, 12))
            ent_pturns.bind("<KeyRelease>", lambda _e: self._update_power_preview())

            for pt_val, pt_lbl in (("1", "1x(직결)"), ("2", "2x"), ("10", "10x"), ("50", "50x")):
                tk.Button(
                    pcoil_box,
                    text=pt_lbl,
                    bg="#E0F2FE",
                    fg="#0369A1",
                    font=("Malgun Gothic", 8, "bold"),
                    relief="flat",
                    padx=6,
                    command=lambda v=pt_val: (self.var_p_use_coil.set(v != "1"), self.var_p_coil_turns.set(v), self._update_power_preview()),
                ).pack(side="left", padx=2)

            self.var_p_preview = tk.StringVar(value="")
            tk.Label(
                pad,
                textvariable=self.var_p_preview,
                bg="#EFF6FF",
                fg="#1D4ED8",
                font=("Consolas", 10, "bold"),
                justify="left",
                padx=12,
                pady=8,
            ).grid(row=7, column=0, columnspan=6, sticky="ew", pady=(4, 8))

            frm_pbtns = ttk.Frame(pad)
            frm_pbtns.grid(row=8, column=0, columnspan=6, sticky="w")
            tk.Button(
                frm_pbtns,
                text="📤 전력 설정값 장비로 전송 (Dual OUT)",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=7,
                command=lambda: self._apply_power(auto_oper=False),
            ).pack(side="left", padx=(0, 10))

            tk.Button(
                frm_pbtns,
                text="⚡ 전력 전송 후 즉시 출력 활성화 (OUT + OPER)",
                bg="#16A34A",
                fg="#FFFFFF",
                font=("Malgun Gothic", 10, "bold"),
                relief="flat",
                padx=16,
                pady=7,
                command=lambda: self._apply_power(auto_oper=True),
            ).pack(side="left")

            self._update_power_preview()

        def _build_console_panel(self):
            frame = tk.Frame(self, bg="#F5F7FA", padx=16, pady=6)
            frame.pack(side="bottom", fill="x")
            row = tk.Frame(frame, bg="#F5F7FA")
            row.pack(fill="x")
            self.btn_console = self._ui_button(row, "통신 기록·수동 명령 ▾", self._toggle_console)
            tk.Label(row, text="기록 자동 저장", bg="#F5F7FA", fg="#64748B").pack(side="right")
            self.console_details = tk.Frame(frame, bg="white", padx=10, pady=8)
            self._console_open = False
            command_row = tk.Frame(self.console_details, bg="white")
            command_row.pack(fill="x", pady=(0, 6))
            self.var_manual_cmd = tk.StringVar(value="*IDN?")
            entry = self._ui_entry(command_row, self.var_manual_cmd, 42)
            entry.bind("<Return>", lambda _e: self._on_manual_send(is_query=False))
            self._ui_button(command_row, "전송", lambda: self._on_manual_send(is_query=False))
            self._ui_button(command_row, "조회", lambda: self._on_manual_send(is_query=True))
            self._ui_button(command_row, "장비 오류 확인", self._on_check_err)
            self.txt_log = tk.Text(self.console_details, height=5, bg="#F8FAFC", fg="#334155", font=("Consolas", 9),
                                    wrap="word", relief="flat", padx=8, pady=6)
            self.txt_log.pack(fill="both", expand=True)
            # The detailed command remains available without occupying the safety bar.
            self.after_idle(self._attach_command_summary)

        def _update_voltage_preview(self) -> None:
            try:
                val = float(self.var_v_val.get())
                freq = SIUnits.input_value(self.var_v_freq.get(), "HZ") if self.var_v_mode.get() == "ACV" else 0.0
                cmd, volts = self._builder().build_voltage_cmd(
                    mode=self.var_v_mode.get(),
                    value=val,
                    unit=self.var_v_unit.get(),
                    freq_hz=freq,
                )
                warn = "  [⚠ 고전압 주의 (>30V)]" if abs(volts) >= HIGH_VOLTAGE_WARN_V else ""
                self.var_v_preview.set(f"생성될 SCPI 명령: {cmd}   |   실제 출력 전압: {volts:g} V{warn}")
            except Exception as e:
                self.var_v_preview.set(f"[입력 오류] {e}")

        def _update_current_preview(self) -> None:
            try:
                val = float(self.var_i_val.get())
                freq = SIUnits.input_value(self.var_i_freq.get(), "HZ") if self.var_i_mode.get() == "ACI" else 0.0
                turns = float(self.var_i_coil_turns.get())
                cmd, actual_a, effective_a = self._builder().build_current_cmd(
                    mode=self.var_i_mode.get(),
                    input_value=val,
                    unit=self.var_i_unit.get(),
                    freq_hz=freq,
                    use_coil=self.var_i_use_coil.get(),
                    coil_turns=turns,
                    current_post=self.var_i_post.get(),
                )
                coil_desc = f"{turns:g}턴 코일 적용 (배율 {turns:g}x)" if self.var_i_use_coil.get() else "직결 모드 (1x, 코일 미사용)"
                self.var_i_preview.set(
                    f"• [{coil_desc}] 클램프 미터 목표 전류: {effective_a:.6g} A  ➔  장비 실제 출력 전류: {actual_a:.6g} A (선택 모델 범위 확인 완료)\n"
                    f"• 생성될 SCPI 명령: {cmd}"
                )
            except Exception as e:
                self.var_i_preview.set(f"[설정 범위/입력 오류] {e}")

        def _update_resistance_preview(self) -> None:
            try:
                val = float(self.var_r_val.get())
                cmd, ohms = self._builder().build_resistance_cmd(
                    value=val,
                    unit=self.var_r_unit.get(),
                    zcomp=self.var_r_zcomp.get(),
                )
                self.var_r_preview.set(f"생성될 SCPI 명령: {cmd}   |   환산 저항값: {ohms:g} Ω")
            except Exception as e:
                self.var_r_preview.set(f"[입력 오류] {e}")

        def _update_power_preview(self) -> None:
            try:
                calc_method = self.var_p_calc_method.get()
                if calc_method == "PW":
                    if self.var_p_unit.get() == "A":
                        self.var_p_unit.set("W")
                    self.lbl_p_second_input.configure(text="목표 유효전력 (W 계열):")
                else:
                    self.var_p_unit.set("A")
                    self.lbl_p_second_input.configure(text="목표 클램프 전류 (A):")

                res = self._builder().build_power_cmd(
                    mode=self.var_p_mode.get(),
                    calc_method=calc_method,
                    voltage_v=SIUnits.input_value(self.var_p_volt.get(), "V"),
                    current_or_power_val=float(self.var_p_val.get()),
                    power_unit=self.var_p_unit.get(),
                    freq_hz=SIUnits.input_value(self.var_p_freq.get(), "HZ"),
                    phase_mode=self.var_p_phase_mode.get(),
                    dpf_val=float(self.var_p_dpf.get()),
                    dpf_lead_lag=self.var_p_lead_lag.get(),
                    phase_deg=float(self.var_p_phase_deg.get()),
                    use_coil=self.var_p_use_coil.get(),
                    coil_turns=float(self.var_p_coil_turns.get()),
                )
                turns_txt = f"{float(self.var_p_coil_turns.get()):g}x 턴코일" if self.var_p_use_coil.get() else "1x 직결"
                self.var_p_preview.set(
                    f"• [전력 산출 결과 ({turns_txt})] 유효전력(P): {res['active_power_w']:.6g} W | 피상전력(S): {res['apparent_power_va']:.6g} VA | 역률(PF): {res['pf']:.4f} (위상 {res['phase_deg']:+.2f}°)\n"
                    f"• [전류 배분] 클램프 인가 유효 전류: {res['effective_current_a']:.6g} A  ➔  장비 실제 출력 전류: {res['actual_current_a']:.6g} A (전압: {res['voltage_v']:g} V)\n"
                    f"• 생성될 SCPI 명령: {res['cmd']}"
                )
            except Exception as e:
                self.var_p_preview.set(f"[전력/턴코일 설정 오류] {e}")

        def _on_connect_clicked(self):
            try:
                args = self._connection_arguments()
                def connect():
                    info = self.conn_mgr.scan_and_auto_connect(**args)
                    state = self.conn_mgr.query("OPER?").strip()
                    if state not in ("0", "1"):
                        self.conn_mgr.disconnect(standby=False)
                        raise InstrumentProtocolError(f"출력 상태 응답이 올바르지 않습니다: {state}")
                    return info, state == "1"
                def done(result):
                    info, self.is_operating = result
                    self._update_connection_ui(info)
                    self._refresh_oper_badge()
                    self._log(f"[연결 성공] {info.interface_type} | {info.idn_string} | PC 원본 IP: {self.conn_mgr.source_ip}")
                self._start_io("장비 연결 및 식별 확인 중…", connect, done)
            except Exception as exc:
                messagebox.showerror("입력 확인", str(exc))

        def _on_sim_connect_clicked(self):
            if self._io_busy:
                return
            self.var_conn_mode.set("SIMULATION")
            self._on_connect_clicked()

        def _on_disconnect_clicked(self):
            if self._sequence_dut_reserved():
                if self._io_busy:
                    self._on_stby_clicked()
                    return
                self._on_seq_stop_auto(silent=True)
            def done(_value):
                self.is_operating = None
                self._update_connection_ui(self.conn_mgr.info)
                self._refresh_oper_badge()
                self._log("[연결 해제] 연결을 종료했습니다.")
            self._start_io("STBY 확인 후 연결 해제 중…", self.conn_mgr.disconnect, done)


        def _update_connection_ui(self, info):
            if info.connected:
                simulation = info.interface_type.startswith("SIMULATION")
                self.lbl_conn_badge.configure(text="시뮬레이션" if simulation else f"{self.conn_mgr.profile.model} 연결됨",
                                               bg="#E0E7FF" if simulation else "#DCFCE7", fg="#3730A3" if simulation else "#166534")
                self.var_idn_display.set(f"{info.interface_type}  ·  {info.resource_name}  ·  {info.idn_string}")
            else:
                self.lbl_conn_badge.configure(text="연결 안 됨", bg="#E2E8F0", fg="#64748B")
                self.var_idn_display.set("모델과 연결 방식을 선택하세요.")
            self._refresh_connection_controls()

        def _ensure_connected_or_sim(self) -> bool:
            if self._io_busy:
                self._log("현재 명령의 완료를 기다려 주세요. 중단하려면 STBY를 누르세요.")
                return False
            if not self.conn_mgr.info.connected:
                ans = messagebox.askyesno(
                    "장비 미연결 안내",
                    "현재 연결된 Fluke 교정기가 없습니다.\n가상 시뮬레이션 모드(SIMULATION)로 자동 전환하여 명령 전송을 테스트하시겠습니까?",
                )
                if ans:
                    self._on_sim_connect_clicked()
                    return False
                return False
            return True

        def _send_and_log(self, cmd, auto_oper=False, on_done=None, on_error=None, stop_sequence=True, *, verify_setpoint=False):
            standby = cmd == "STBY" and not auto_oper
            if (self._seq_close_standby or self._dut_closing) and not standby:
                return False
            if stop_sequence and self._sequence_dut_reserved():
                self._log('DUT 기록을 중단한 뒤 다른 교정기 명령을 실행하세요.')
                return False
            if not self._ensure_connected_or_sim():
                return False
            if not standby:
                self.conn_mgr.operation_timeout = self._get_operation_timeout()
            full_cmd = f"{cmd}; OPER" if auto_oper else cmd
            self.conn_mgr.profile.validate_command(full_cmd)
            self.var_active_cmd_summary.set(f"전송 요청: {full_cmd}")
            self.is_operating = None
            self._refresh_oper_badge()
            def operation():
                if verify_setpoint:
                    self.conn_mgr.send_command(cmd, check_before=True)
                    if auto_oper:
                        self.conn_mgr.send_command("OPER")
                else:
                    self.conn_mgr.send_command(full_cmd)
                state = self.conn_mgr.query("OPER?").strip()
                if state not in ("0", "1"):
                    error = InstrumentProtocolError(f"출력 상태 확인 실패: {state}")
                    with self.conn_mgr._lock:
                        self.conn_mgr._fail_control_session("OPER? 응답 확인", error)
                    raise error
                if standby and state != "0":
                    raise InstrumentProtocolError("STBY 요청 후에도 OPER 상태입니다. 본체에서 출력을 끄세요.")
                return state == "1"
            def done(state):
                self.is_operating = state
                self._update_connection_ui(self.conn_mgr.info)
                self._refresh_oper_badge()
                self.var_active_cmd_summary.set(f"처리 완료: {full_cmd}")
                self._log(f"[완료 확인] {full_cmd} | 출력={'OPER' if state else 'STBY'}")
                if on_done:
                    on_done(state)
            return self._start_io(f"TX >> {full_cmd} | 완료 대기 최대 {self.conn_mgr.operation_timeout:g}초",
                                  operation, done, on_error=on_error, stop_sequence=stop_sequence,
                                  standby=standby)

        def _apply_voltage(self, auto_oper: bool) -> None:
            try:
                val = float(self.var_v_val.get())
                freq = SIUnits.input_value(self.var_v_freq.get(), "HZ") if self.var_v_mode.get() == "ACV" else 0.0
                cmd, _ = self._builder().build_voltage_cmd(
                    mode=self.var_v_mode.get(),
                    value=val,
                    unit=self.var_v_unit.get(),
                    freq_hz=freq,
                )
                self._send_and_log(cmd, auto_oper=auto_oper)
            except Exception as e:
                messagebox.showerror("전압 설정 오류", str(e))

        def _apply_current(self, auto_oper: bool) -> None:
            try:
                val = float(self.var_i_val.get())
                freq = SIUnits.input_value(self.var_i_freq.get(), "HZ") if self.var_i_mode.get() == "ACI" else 0.0
                turns = float(self.var_i_coil_turns.get())
                cmd, actual_a, effective_a = self._builder().build_current_cmd(
                    mode=self.var_i_mode.get(),
                    input_value=val,
                    unit=self.var_i_unit.get(),
                    freq_hz=freq,
                    use_coil=self.var_i_use_coil.get(),
                    coil_turns=turns,
                    current_post=self.var_i_post.get(),
                )
                self._send_and_log(cmd, auto_oper=auto_oper)
                if self.var_i_use_coil.get():
                    self._log(f"   └─ [턴코일 {turns:g}x 환산] 클램프 목표 전류: {effective_a:g} A ➔ 장비 실제 출력: {actual_a:g} A")
            except Exception as e:
                messagebox.showerror("전류/턴코일 설정 오류", str(e))

        def _apply_resistance(self, auto_oper: bool) -> None:
            try:
                val = float(self.var_r_val.get())
                cmd, _ = self._builder().build_resistance_cmd(
                    value=val,
                    unit=self.var_r_unit.get(),
                    zcomp=self.var_r_zcomp.get(),
                )
                self._send_and_log(cmd, auto_oper=auto_oper)
            except Exception as e:
                messagebox.showerror("저항 설정 오류", str(e))

        def _apply_power(self, auto_oper: bool) -> None:
            try:
                res = self._builder().build_power_cmd(
                    mode=self.var_p_mode.get(),
                    calc_method=self.var_p_calc_method.get(),
                    voltage_v=SIUnits.input_value(self.var_p_volt.get(), "V"),
                    current_or_power_val=float(self.var_p_val.get()),
                    power_unit=self.var_p_unit.get(),
                    freq_hz=SIUnits.input_value(self.var_p_freq.get(), "HZ"),
                    phase_mode=self.var_p_phase_mode.get(),
                    dpf_val=float(self.var_p_dpf.get()),
                    dpf_lead_lag=self.var_p_lead_lag.get(),
                    phase_deg=float(self.var_p_phase_deg.get()),
                    use_coil=self.var_p_use_coil.get(),
                    coil_turns=float(self.var_p_coil_turns.get()),
                )
                self._send_and_log(str(res["cmd"]), auto_oper=auto_oper)
                self._log(
                    f"   └─ [전력 출력 요약] 유효전력={res['active_power_w']:.6g} W, 전압={res['voltage_v']:g} V, "
                    f"클램프 유효전류={res['effective_current_a']:.6g} A (장비 실제 출력={res['actual_current_a']:.6g} A)"
                )
            except Exception as e:
                messagebox.showerror("전력 설정 오류", str(e))

        def _on_oper_clicked(self):
            try:
                self._send_and_log("OPER")
            except Exception as exc:
                messagebox.showerror("출력 요청 오류", str(exc))

        def _on_stby_clicked(self):
            self._on_seq_stop_auto(silent=True)
            if self._standby_in_progress:
                self._log("STBY가 이미 전송되었으며 완료 응답을 기다리고 있습니다.")
                return
            if self._io_busy:
                self._pending_standby = True
                self.conn_mgr.request_abort()
                self.is_operating = None
                self._refresh_oper_badge()
                self._log("[중지 요청] 현재 대기를 중단하고 STBY 전송을 시도합니다. 본체에서 출력 차단을 확인하세요.")
                return
            try:
                self._send_and_log("STBY")
            except Exception as exc:
                messagebox.showerror("STBY 요청 오류", str(exc))

        def _on_reset_clicked(self):
            if self._sequence_dut_reserved() or self._seq_dut_reading:
                self._log('DUT 기록을 중단하거나 현재 조회가 끝난 뒤 실행하세요.')
                return
            self._on_seq_stop_auto(silent=True)
            try:
                self._send_and_log("*RST; *CLS; STBY")
            except Exception as exc:
                messagebox.showerror("초기화 요청 오류", str(exc))

        def _refresh_oper_badge(self):
            if self.is_operating is None:
                text, bg, fg = "출력 상태 미확인 · 본체 확인", "#FEF3C7", "#92400E"
            elif self.is_operating:
                text, bg, fg = "OPER · 출력 중", "#FEE2E2", "#991B1B"
            else:
                text, bg, fg = "STBY · 출력 대기", "#DCFCE7", "#166534"
            self.lbl_output_state.configure(text=text, bg=bg, fg=fg)

        def _on_manual_send(self, is_query):
            if not self._ensure_connected_or_sim():
                return
            cmd = self.var_manual_cmd.get().strip()
            if not cmd:
                return
            try:
                if is_query or "?" in cmd:
                    self.conn_mgr.operation_timeout = self._get_operation_timeout()
                    def done(response):
                        self._log(f"RX << {response}")
                        self._update_connection_ui(self.conn_mgr.info)
                    self._start_io(f"TX QUERY >> {cmd}", lambda: self.conn_mgr.query(cmd), done)
                else:
                    self._send_and_log(cmd)
            except Exception as exc:
                self._log(f"[명령 오류] {exc}")

        def _on_check_err(self) -> None:
            self.var_manual_cmd.set("ERR?")
            self._on_manual_send(is_query=True)

        def _build_current_group_panel(self, container):
            self._group_high_only = False
            self._group_basis = SequenceCurrentFilter.JOB
            self._seq_active_indices = []
            self._original_row_positions = None
            self.var_high_current_only = tk.BooleanVar(value=False)
            self.var_current_group_basis = tk.StringVar(value=self._group_basis)
            self.var_current_group_summary = tk.StringVar()
            self.group_panel = tk.Frame(container, bg="white")
            self.group_panel.pack(fill="x", pady=(0, 4))
            self.chk_high_current_only = ttk.Checkbutton(self.group_panel, text="큰 전류 그룹만 실행", variable=self.var_high_current_only,
                                                        command=self._on_current_group_changed)
            self.chk_high_current_only.pack(side="left", padx=(0, 12))
            self.btn_group_restore = self._ui_button(self.group_panel, "원래 순서", lambda: self._reorder_current_group_rows(restore=True), side="right")
            self.btn_group_restore.state(["disabled"])
            self.btn_group_sort = self._ui_button(self.group_panel, "현재 그룹 위로", self._reorder_current_group_rows, side="right")
            self.group_summary_label = ttk.Label(self.group_panel, textvariable=self.var_current_group_summary,
                                                 foreground="#64748B", wraplength=600)
            self.group_summary_label.pack(side="left", fill="x", expand=True)
            self.group_summary_label.bind("<Configure>", lambda e: self.group_summary_label.configure(wraplength=max(100, e.width)))
            row = tk.Frame(self.sequence_details, bg="white")
            row.pack(fill="x")
            self._ui_label(row, "전류 분리 기준")
            combo = self._ui_combo(row, self.var_current_group_basis, (SequenceCurrentFilter.JOB, SequenceCurrentFilter.POSTS), 28)
            combo.bind("<<ComboboxSelected>>", lambda _e: self._on_current_group_changed())
            self._ui_label(row, "전류·전력 공통 / 코일 환산 후 실제 A 기준")

        def _step_in_current_group(self, step):
            return SequenceCurrentFilter.includes(step, self._group_high_only, self._group_basis, self._selected_model())

        def _current_group_label(self):
            return SequenceCurrentFilter.label(self._group_high_only, self._group_basis, self._selected_model())

        def _reorder_current_group_rows(self, restore=False):
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self.var_active_row_banner.set("행 순서를 바꾸려면 실행을 일시중단하고 현재 응답이 끝날 때까지 기다리세요.")
                return
            if not self.seq_steps or (restore and self._original_row_positions is None):
                return

            grid = self.excel_grid
            focus = grid.focus_get()
            for row, widgets in enumerate(grid._row_widgets):
                for column, entry in widgets["entries"].items():
                    if entry is focus:
                        grid._commit_cell(row, column)
                        grid.focus_set()

            # Original positions follow the row slots through cell edits and
            # command rebuilds. Rows appended after a sort go at the end on restore.
            count = len(self.seq_steps)
            positions = self._original_row_positions or []
            positions = positions + list(range(len(positions), count))
            current_order = list(range(count))
            original_order = sorted(current_order, key=positions.__getitem__)
            order = original_order
            if not restore:
                included, excluded = [], []
                try:
                    for index in original_order:
                        group = included if self._step_in_current_group(self.seq_steps[index]) else excluded
                        group.append(index)
                except ValueError as exc:
                    self.var_active_row_banner.set(str(exc))
                    return
                order = included + excluded
            if order == current_order:
                self.var_active_row_banner.set("이미 선택한 그룹의 정렬 순서입니다.")
                return

            self._on_seq_stop_auto(silent=True)
            new_indices = {old: new for new, old in enumerate(order)}
            self.seq_steps = [self.seq_steps[index] for index in order]
            for number, step in enumerate(self.seq_steps, 1):
                step.step_no = number
            positions = [positions[index] for index in order]
            self._original_row_positions = None if positions == current_order else positions
            self.seq_current_idx = new_indices.get(self.seq_current_idx, -1)
            self.seq_paused_idx = -1
            self._seq_active_indices = []
            grid.selected_row_idx = new_indices.get(grid.selected_row_idx, 0)
            self.var_seq_start_row.set("1")
            self.var_seq_stop_row.set(str(count))
            grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
            self._refresh_current_group_preview()
            grid.canvas.yview_moveto(0)
            if restore:
                message = "원래 순서로 복원했습니다."
            else:
                message = f"현재 그룹 {len(included)}행은 위로, 다른 그룹 {len(excluded)}행은 아래로 정렬했습니다."
            self.var_active_row_banner.set(f"{message} 실행 범위 1~{count}행")

        def _refresh_current_group_preview(self):
            if not hasattr(self, 'var_current_group_summary'):
                return
            self.btn_group_restore.state(["disabled"] if self._original_row_positions is None else ["!disabled"])
            count = 0
            for index, step in enumerate(self.seq_steps):
                try:
                    included = self._step_in_current_group(step)
                except ValueError as exc:
                    self.var_current_group_summary.set(str(exc))
                    return
                if included:
                    count += 1
                    if step.status == "⏭ 다른 전류 그룹":
                        step.status = "대기"
                    step.status = step.status.removesuffix(" · 제외")
                elif step.status.startswith("✓ 완료"):
                    step.status = "✓ 완료 · 제외"
                else:
                    step.status = "⏭ 다른 전류 그룹"
                self.excel_grid._update_single_row_visual(index, step,
                    is_active=included and index == self.seq_current_idx)
            note = MODEL_PROFILES[self._selected_model()].terminal_note
            self.var_current_group_summary.set(f"현재: {self._current_group_label()} | 시트 대상 {count}/{len(self.seq_steps)}행 | {note}")

        def _on_current_group_changed(self):
            high = self.var_high_current_only.get()
            basis = self.var_current_group_basis.get()
            # Display the committed selection until the change is accepted.
            self.var_high_current_only.set(self._group_high_only)
            self.var_current_group_basis.set(self._group_basis)
            if high == self._group_high_only and basis == self._group_basis:
                return
            if self._io_busy or self.seq_running or self._seq_dut_pending is not None:
                self._log("[그룹 변경 보류] 순차 실행을 일시중단하고 현재 응답이 끝난 뒤 변경하세요.")
                return
            def apply(_state=False):
                if _state:
                    self._log("[그룹 변경 실패] STBY 확인이 필요합니다.")
                    return
                self._group_high_only, self._group_basis = high, basis
                self.var_high_current_only.set(high)
                self.var_current_group_basis.set(basis)
                self.seq_current_idx = self.seq_paused_idx = -1
                self._seq_active_indices = []
                self.var_seq_start_row.set("1")
                self.var_seq_stop_row.set(str(max(1, len(self.seq_steps))))
                self.progress_var.set(0)
                self._refresh_current_group_preview()
                self.var_active_row_banner.set(f"그룹 변경: {self._current_group_label()} — 본체 STBY와 케이블 연결 확인 후 시작 버튼을 누르세요.")
                self._log(f"[그룹 변경] {self._current_group_label()} | 시작 범위 초기화, 자동 출력 없음")
            if self.conn_mgr.info.connected:
                self._send_and_log("STBY", on_done=apply)
            else:
                apply()

        def _start_filtered_sequence(self, start_idx, stop_idx):
            if self._sequence_dut_reserved() or self._seq_dut_reading:
                self._log('DUT 기록을 중단하거나 현재 조회가 끝난 뒤 실행하세요.')
                return
            if self._io_busy:
                return
            try:
                dwell = float(self.var_seq_dwell_sec.get())
                if not .2 <= dwell <= 86_400:
                    raise ValueError("유지시간은 0.2~86400초 사이여야 합니다.")
                plan = [i for i in range(start_idx, stop_idx + 1) if self._step_in_current_group(self.seq_steps[i])]
                for i in plan:
                    step = self.seq_steps[i]
                    if not step.scpi_cmd or step.func_type == "ERROR":
                        raise ValueError(f"#{step.step_no}행 설정을 확인하세요: {step.actual_out_desc}")
                    MODEL_PROFILES[self._selected_model()].validate_command(step.scpi_cmd)
            except (ValueError, IndexError) as exc:
                self._show_report("순차 실행 설정 확인", str(exc))
                return
            self._on_seq_stop_auto(silent=True)
            self._seq_active_indices = plan
            if not plan:
                self.var_active_row_banner.set(f"지정 범위에 {self._current_group_label()} 대상이 없습니다.")
                self._log("[실행 대상 없음] 그룹 또는 시작·중단 행 범위를 확인하세요. 출력 명령을 보내지 않았습니다.")
                return
            if not self._ensure_connected_or_sim():
                return
            for i in range(start_idx, stop_idx + 1):
                if i in plan:
                    self.seq_steps[i].status = "대기"
            self._refresh_current_group_preview()
            self.progress_var.set(0)
            self._seq_dut_run = self._sequence_dut_enabled() and any(
                self.seq_steps[i].func_type in self._DUT_SEQUENCE_QUANTITIES for i in plan)
            self._seq_dut_resume_required = False
            self.seq_running = True
            self._dut_sync_controls()
            self._log(f"[그룹 순차 시작] {self._current_group_label()} | 표의 실행 행: {', '.join(str(i + 1) for i in plan)} | 유지 {dwell:g}초")
            self._run_next_auto_step(start_idx, stop_idx, int(dwell * 1000))

        def _selected_model(self):
            if self.conn_mgr.info.connected:
                return self.conn_mgr.profile.model
            return self._configured_model

        def _builder(self):
            return CalibratorCommandBuilder(self._selected_model())

        def _sync_model_controls(self):
            profile = MODEL_PROFILES[self._configured_model]
            choices = list(profile.interfaces) + ["AUTO", "SIMULATION"]
            self.combo_conn_mode.configure(values=choices)
            if self.var_conn_mode.get() not in choices:
                self.var_conn_mode.set(choices[0])
            rates = (300, 600, 1200, 2400, 4800, 9600)
            if profile.model in ("5560A", "5080A"):
                rates += (19200, 38400, 57600, 115200)
            if profile.model == "5560A":
                rates = rates[5:]
            self.serial_settings_controls[0].configure(values=rates)
            if self.var_baudrate.get() not in tuple(map(str, rates)):
                self.var_baudrate.set("9600")
            comp_note = ("단독 턴코일 LCOMP 없음" if profile.model == "5560A" else
                         "턴코일 AC: 지원 범위에서 LCOMP 자동 ON")
            self.var_model_note.set(f"{', '.join(profile.interfaces)} | 실제 전류 ≤{profile.max_current:g} A | {comp_note}")
            if hasattr(self, 'var_resistance_title'):
                self.var_resistance_title.set("5080A 저항: 18개 고정값 + 0 Ω / 최대 190 MΩ" if profile.model == "5080A"
                                              else "저항 출력 및 리드 보상 (0 Ω ~ 1100 MΩ)")
            if hasattr(self, 'combo_current_post'):
                self.combo_current_post.configure(values=["AUTO"] if profile.model == "5560A" else ["AUTO", "AUX", "A20"])
                self.var_i_post.set("AUTO")
            if hasattr(self, 'connection_endpoints'):
                self._sync_connection_view()

        def _on_model_changed(self):
            requested = self.var_model.get()
            if self._io_busy or self.seq_running or self.conn_mgr.info.connected:
                self.var_model.set(self._configured_model)
                self._log("모델 변경은 현재 작업을 멈추고 연결을 해제한 뒤 가능합니다.")
                return
            if requested not in MODEL_PROFILES:
                self.var_model.set(self._configured_model)
                return
            self._configured_model = requested
            self._sync_model_controls()
            self.seq_current_idx = self.seq_paused_idx = -1
            self._seq_active_indices = []
            self.progress_var.set(0)
            self._recalculate_all_grid_steps()
            for method in (self._update_voltage_preview, self._update_current_preview,
                           self._update_resistance_preview, self._update_power_preview):
                method()
            self._refresh_current_group_preview()
            self._log(f"[모델 변경] {requested} — {MODEL_PROFILES[requested].terminal_note}")

        def _on_refresh_serial_ports(self):
            def done(ports):
                choices = [f"{device} | {description}" for device, description in ports]
                current = self.var_serial_port.get().split(" | ", 1)[0]
                self.combo_serial_port.configure(values=choices)
                if not current and choices:
                    self.var_serial_port.set(choices[0])
                note = ("COM 포트를 선택하세요. 다른 장치에는 연결하지 않습니다." if choices else
                        "COM 포트가 없습니다. 컨버터 드라이버/케이블을 확인하세요." if HAS_PYSERIAL else
                        "pyserial이 없습니다. 통신라이브러리_설치.cmd를 실행하세요.")
                self.var_idn_display.set(note)
            self._start_io("PC의 COM 포트 목록 확인 중…", self.conn_mgr.list_serial_ports, done)

        def _ui_label(self, parent, text):
            label = ttk.Label(parent, text=text)
            label.pack(side="left", padx=(0, 6))
            return label

        def _ui_entry(self, parent, variable, width):
            entry = ttk.Entry(parent, textvariable=variable, width=width)
            entry.pack(side="left", padx=(0, 12))
            return entry

        def _ui_combo(self, parent, variable, choices, width):
            combo = ttk.Combobox(parent, textvariable=variable, values=choices, width=width, state="readonly")
            combo.pack(side="left", padx=(0, 12))
            return combo

        def _ui_button(self, parent, text, action, style="TButton", side="left"):
            button = ttk.Button(parent, text=text, command=action, style=style)
            button.pack(side=side, padx=(0, 6))
            return button

        def _sync_connection_view(self):
            if not hasattr(self, "connection_endpoints"):
                return
            mode = self.var_conn_mode.get()
            for key, frame in self.connection_endpoints.items():
                frame.pack_forget()
                if key == mode:
                    frame.pack(side="left", fill="x", expand=True)
            for key, frame in self.connection_detail_rows.items():
                frame.pack_forget()
                visible = key == mode or (key == "VISA" and mode in ("USB", "GPIB"))
                if mode == "AUTO":
                    supported = MODEL_PROFILES[self._configured_model].interfaces
                    visible = key in ("AUTO", "RS232") or (key == "LAN" and "LAN" in supported) or (key == "VISA" and "GPIB" in supported)
                if visible:
                    frame.pack(fill="x", pady=4)

        def _toggle_connection_details(self):
            self._connection_details_open = not self._connection_details_open
            if self._connection_details_open:
                self.connection_details.pack(fill="x", before=self.connection_status)
            else:
                self.connection_details.pack_forget()
            self.btn_connection_details.configure(text="상세 설정 ▴" if self._connection_details_open else "상세 설정 ▾")

        def _refresh_connection_controls(self):
            connected = self.conn_mgr.info.connected
            locked = self._io_busy or connected
            self.combo_model.configure(state="disabled" if locked else "readonly")
            self.combo_conn_mode.configure(state="disabled" if locked else "readonly")
            self.btn_connect.configure(state="disabled" if locked else "normal")
            self.btn_disconnect.configure(state="normal" if connected and not self._io_busy else "disabled")

        def _toggle_sequence_details(self):
            self._sequence_details_open = not self._sequence_details_open
            if self._sequence_details_open:
                self.sequence_details.pack(fill="x", before=self.group_panel)
            else:
                self.sequence_details.pack_forget()
            self.btn_sequence_details.configure(text="범위·그룹 설정 ▴" if self._sequence_details_open else "범위·그룹 설정 ▾")

        def _attach_command_summary(self):
            ttk.Label(self.console_details, textvariable=self.var_active_cmd_summary, foreground="#64748B",
                      wraplength=1050).pack(fill="x", pady=(5, 0))

        def _toggle_console(self):
            self._console_open = not self._console_open
            if self._console_open:
                self.console_details.pack(fill="x", pady=(6, 0))
            else:
                self.console_details.pack_forget()
            self.btn_console.configure(text="통신 기록·수동 명령 ▴" if self._console_open else "통신 기록·수동 명령 ▾")

        def _polish_manual_tabs(self):
            def walk(widget, tab):
                for child in widget.winfo_children():
                    if isinstance(child, tk.Button):
                        text = str(child.cget("text"))
                        if "전송 후" in text:
                            child.configure(text="설정 후 출력", bg="#2563EB", fg="white")
                        elif "전송" in text:
                            child.configure(text="설정만 전송", bg="#F1F5F9", fg="#334155")
                        else:
                            child.configure(bg="#F1F5F9", fg="#334155")
                        child.configure(font=("Malgun Gothic", 9), padx=10, pady=5, relief="flat")
                    elif isinstance(child, (tk.Label, tk.LabelFrame, tk.Checkbutton)):
                        value = str(child.cget("text"))
                        value = re.sub(r"[⚡📤🧲🔋💡⚠📊]+\s*", "", value)
                        child.configure(text=value)
                        if isinstance(child, tk.Label) and child.cget("textvariable"):
                            child.configure(font=("Malgun Gothic", 9), fg="#475569", justify="left", wraplength=1000)
                            tab.bind("<Configure>", lambda e, label=child: label.configure(wraplength=max(400, e.width - 64)), add="+")
                    walk(child, tab)
            for tab in (self.tab_voltage, self.tab_current, self.tab_resistance, self.tab_power):
                walk(tab, tab)

        def _log(self, msg: str) -> None:
            self.conn_mgr.record_event(msg)
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.txt_log.insert("end", f"[{ts}] {msg}\n")
            self.txt_log.see("end")


def run_cli_interactive(model="5560A") -> None:
    mgr = Fluke5560AConnectionManager()
    print("=" * 88)
    print(" Fluke 5560A Multi-Product Calibrator Controller v6.10.4 (CLI Mode)")
    print("=" * 88)
    print(f"현재 PC 로컬 IPv4: {mgr.get_local_pc_ipv4_list()}")
    print(f"감지된 이더넷 어댑터: {AutoNetworkAligner.list_ethernet_interfaces()}")
    info = mgr.scan_and_auto_connect("SIMULATION", model=model)
    print(f"연결 상태: {info.interface_type} | {info.resource_name} | {info.idn_string}")


def main():
    parser = argparse.ArgumentParser(description="Fluke 5560A/5522A/5520A/5080A USB/LAN/RS232/GPIB controller")
    parser.add_argument("--model", choices=list(MODEL_PROFILES), default="5560A")
    parser.add_argument("--cli", action="store_true", help="simulation smoke check")
    parser.add_argument("--diagnose", metavar="IP", help="read-only LAN *IDN? diagnosis")
    parser.add_argument("--port", type=int, default=3490)
    parser.add_argument("--source-ip", default="")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--eol", choices=["CRLF", "CR", "LF"], default="CRLF")
    args = parser.parse_args()
    if args.diagnose:
        mgr = Fluke5560AConnectionManager()
        mgr.expected_model = args.model
        mgr.profile = MODEL_PROFILES[args.model]
        try:
            report = mgr.diagnose_lan(args.diagnose, args.port, args.timeout, args.source_ip, args.eol, args.adapter)
            print(report)
            return 0 if "[진단 성공]" in mgr.last_error else 1
        except Exception as exc:
            print(f"진단 실패: {exc}")
            return 1
    if HAS_TKINTER and not args.cli:
        app = Fluke5560AControllerApp()
        if args.model != "5560A":
            app.var_model.set(args.model)
            app._on_model_changed()
        app.mainloop()
    else:
        run_cli_interactive(args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
