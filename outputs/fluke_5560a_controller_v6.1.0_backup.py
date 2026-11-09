#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fluke 5560A / 5522A / 5520A / 5080A Controller v6.1.0
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
try:
    import pyvisa
    HAS_PYVISA = True
except ImportError:
    pyvisa = None
    HAS_PYVISA = False


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
SAMPLE_DCV_TABLE = """Spec\tRange\t조건1\t명목값\t측정 1
99\t600 mV\t\t100.0 mV\t99.8
-101\t600 mV\t\t-100.0 mV\t-99.8
0.99\t6 V\t\t1.000 V\t1.000
-1.01\t6 V\t\t-1.000 V\t-1.000
54.45\t60 V\t\t55.00 V\t54.99
-55.55\t60 V\t\t-55.00 V\t-54.98
39.6\t60 V\t\t40.00 V\t39.99
-40.4\t60 V\t\t-40.00 V\t-39.99
29.7\t60 V\t\t30.00 V\t30.00
-30.3\t60 V\t\t-30.00 V\t-29.99
19.8\t60 V\t\t20.00 V\t20.00
-20.2\t60 V\t\t-20.00 V\t-20.00
9.9\t60 V\t\t10.00 V\t10.00
-10.1\t60 V\t\t-10.00 V\t-10
594\t600 V\t\t600.0 V\t599.7
-606\t600 V\t\t-600.0 V\t-599.7"""

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


# =====================================================================
# 2. SCPI 명령 생성 및 턴코일/전력 연산 엔진 (비즈니스 로직)
# =====================================================================
class Fluke5560ACommandBuilder:
    """Fluke 5560A Remote Programmers Manual 규격에 맞춘 SCPI/원격 명령 생성기"""

    @staticmethod
    def normalize_unit(unit: str) -> str:
        u = unit.strip()
        u_clean = (
            u.replace("μ", "U")
            .replace("µ", "U")
            .replace("Ω", "OHM")
            .replace("Ω", "OHM")
            .upper()
        )
        alias_map = {
            "UV": "UV", "MV": "MV", "V": "V", "KV": "KV",
            "UA": "UA", "MA": "MA", "A": "A",
            "OHM": "OHM", "OHMS": "OHM",
            "KOHM": "KOHM", "KOHMS": "KOHM",
            "MOHM": "MOHM", "MOHMS": "MOHM",
            "GOHM": "GOHM", "GOHMS": "GOHM",
            "MW": "MW", "W": "W", "KW": "KW",
            "HZ": "HZ", "KHZ": "KHZ", "MHZ": "MHZ",
        }
        if u_clean not in alias_map:
            raise ValueError(f"지원하지 않는 단위입니다: {unit}")
        return alias_map[u_clean]

    @classmethod
    def unit_multiplier(cls, unit: str) -> float:
        u = cls.normalize_unit(unit)
        mapping = {
            "UV": 1e-6, "MV": 1e-3, "V": 1.0, "KV": 1e3,
            "UA": 1e-6, "MA": 1e-3, "A": 1.0,
            "OHM": 1.0, "KOHM": 1e3, "MOHM": 1e6, "GOHM": 1e9,
            "MW": 1e-3, "W": 1.0, "KW": 1e3,
            "HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6,
        }
        return mapping[u]

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
        volts = value * cls.unit_multiplier(norm_unit)
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
            cmd = f"OUT {value:.8g} {norm_unit}, {freq_hz:.6g} HZ"
        else:
            cmd = f"OUT {value:.8g} {norm_unit}, 0 HZ"

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
        target_a = input_value * cls.unit_multiplier(norm_unit)
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
        ohms = value * cls.unit_multiplier(norm_unit)
        if ohms < 0 or ohms > MAX_RESISTANCE_OHM:
            raise ValueError(
                f"설정 저항({ohms:.4g} Ω)이 허용 범위(0 Ω ~ {MAX_RESISTANCE_OHM/1e6:g} MΩ)를 벗어났습니다."
            )
        zcomp_clean = zcomp.strip().upper()
        if zcomp_clean not in ("NONE", "WIRE2", "WIRE4"):
            raise ValueError(f"유효하지 않은 ZCOMP 모드입니다: {zcomp}")

        cmd = f"OUT {value:.8g} {norm_unit}; ZCOMP {zcomp_clean}"
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
            target_power_w = current_or_power_val * cls.unit_multiplier(power_unit)
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
                cmds.append(f"DPF {dpf_val:.2f}, {lead_lag_token}")
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

    VI_SLASH_PATTERN = re.compile(
        r"([+-]?\d+(?:\.\d+)?)\s*([a-zA-Zμµ]+)\s*/\s*([+-]?\d+(?:\.\d+)?)\s*([a-zA-Zμµ]+)"
    )
    TOKEN_FINDER = re.compile(
        r"([+-]?\d+(?:\.\d+)?)\s*([a-zA-ZμµΩΩ]+)"
    )

    FREQ_UNITS = {"HZ", "KHZ", "MHZ"}
    POWER_UNITS = {"MW", "W", "KW"}
    OUTPUT_UNITS = {
        "UV", "MV", "V", "KV",
        "UA", "MA", "A",
        "OHM", "KOHM", "MOHM", "GOHM",
        "MW", "W", "KW",
    }

    @classmethod
    def parse_value_and_unit(cls, text: str) -> Optional[Tuple[float, str]]:
        m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([a-zA-ZμµΩΩ]+)\s*$", text.strip())
        if not m:
            return None
        val = float(m.group(1))
        raw_unit = m.group(2)
        try:
            norm_unit = Fluke5560ACommandBuilder.normalize_unit(raw_unit)
            return val, norm_unit
        except ValueError:
            return None

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
        parsed_nom = cls.parse_value_and_unit(nominal_raw)
        if parsed_nom is None:
            raise ValueError(f"#{step_no}행의 명목값('{nominal_raw}')에서 수치와 단위를 인식할 수 없습니다. (예: 100 mV, 5.5 A, 60 W)")
        nom_val, nom_unit = parsed_nom

        freq_hz = 0.0
        has_freq = False
        freq_display = freq_cond_raw.strip() if freq_cond_raw.strip() and freq_cond_raw.strip() != "-" else "DC"
        for m in cls.TOKEN_FINDER.finditer(freq_cond_raw):
            f_val = float(m.group(1))
            try:
                f_unit = Fluke5560ACommandBuilder.normalize_unit(m.group(2))
                if f_unit in cls.FREQ_UNITS:
                    freq_hz = f_val * Fluke5560ACommandBuilder.unit_multiplier(f_unit)
                    has_freq = freq_hz > 0
                    freq_display = f"{m.group(1)} {m.group(2)}"
            except ValueError:
                continue

        if nom_unit in cls.POWER_UNITS:
            m_vi = cls.VI_SLASH_PATTERN.search(vi_cond_raw)
            if m_vi:
                v_val = float(m_vi.group(1))
                v_unit = Fluke5560ACommandBuilder.normalize_unit(m_vi.group(2))
                i_val = float(m_vi.group(3))
                i_unit = Fluke5560ACommandBuilder.normalize_unit(m_vi.group(4))
                voltage_v = v_val * Fluke5560ACommandBuilder.unit_multiplier(v_unit)
                cond_current_a = i_val * Fluke5560ACommandBuilder.unit_multiplier(i_unit)
                vi_display = f"{m_vi.group(1)} {m_vi.group(2)} / {m_vi.group(3)} {m_vi.group(4)}"
            else:
                voltage_v = 220.0
                for m_v in cls.TOKEN_FINDER.finditer(vi_cond_raw):
                    u_v = Fluke5560ACommandBuilder.normalize_unit(m_v.group(2))
                    if u_v in ("UV", "MV", "V", "KV"):
                        voltage_v = float(m_v.group(1)) * Fluke5560ACommandBuilder.unit_multiplier(u_v)
                        break
                cond_current_a = None
                vi_display = f"{voltage_v:g} V"

            pf_num_match = re.search(r"([+-]?\d+(?:\.\d+)?)", pf_cond_raw)
            pf_raw_num = float(pf_num_match.group(1)) if pf_num_match else 1.0
            dpf_val, dpf_lead_lag, pf_display = Fluke5560ACommandBuilder.decode_signed_pf(pf_raw_num)

            target_power_w = nom_val * Fluke5560ACommandBuilder.unit_multiplier(nom_unit)
            calc_current_a = target_power_w / (voltage_v * dpf_val)
            if cond_current_a is not None and abs(cond_current_a - calc_current_a) <= max(1e-4, 0.02 * cond_current_a):
                target_current_a = cond_current_a
            else:
                target_current_a = calc_current_a

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

        if nom_unit in ("UV", "MV", "V", "KV"):
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

        if nom_unit in ("UA", "MA", "A"):
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

        if nom_unit in ("OHM", "KOHM", "MOHM", "GOHM"):
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
        line = raw_line.strip()
        if not line or cls._is_header_line(line):
            return None

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
                elif u_tok in ("UV", "MV", "V", "KV"):
                    pass

            masked_chars = list(rest)
            for s_idx, e_idx in spans_to_mask:
                for k in range(s_idx, e_idx):
                    masked_chars[k] = " "
            rem_str = "".join(masked_chars)
            pf_match = re.search(r"([+-]?\d+(?:\.\d+)?)", rem_str)
            pf_str = pf_match.group(1) if pf_match else "1"

            if power_nom_str:
                return (vi_cond_str, freq_str, pf_str, power_nom_str)

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
                output_tokens.append((raw_token, float(num_str), norm_unit))

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
        scales = {"UV": ("V", 1e-6), "MV": ("V", .001), "V": ("V", 1),
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
    CURRENT_FUNCTIONS = {"DCI", "ACI", "DCP", "ACP"}
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
        if not adapter_idx:
            return ""
        target = ipaddress.IPv4Address(cls.normalize_ipv4(target_ip))
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


try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    serial = None
    HAS_PYSERIAL = False


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
        self.last_diagnostic = ""
        self.operation_timeout = 60.0
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self._log_lock = threading.Lock()
        self.log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
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
            if adapter_idx:
                source_ip = AutoNetworkAligner.select_source_ip(adapter_idx, ip)
            elif source_ip:
                source_ip = AutoNetworkAligner.normalize_ipv4(source_ip)
            self.source_ip = source_ip
            result = self._try_lan_socket(ip, port, timeout_sec, source_ip=source_ip, eol=eol)
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
                        source_ip="", eol="CRLF"):
        sock = None
        stage = "TCP 연결"
        try:
            ip = AutoNetworkAligner.normalize_ipv4(ip)
            port = self._validate_port(port)
            if source_ip and source_ip == ip:
                raise ValueError("5560A IP와 PC IP가 같습니다. 장비 화면의 IP를 확인하세요.")
            sock = socket.create_connection((ip, port), timeout_sec,
                                             source_address=(source_ip, 0) if source_ip else None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.source_ip = sock.getsockname()[0]
            transport = TelnetLineTransport(sock, timeout_sec, eol)
            stage = "장비 식별 (*IDN?)"
            idn = transport.query("*IDN?", identity=True)
            self._identify(idn, "LAN")
            # Identification is complete before sending any state-changing commands.
            stage = "Computer 통신 모드 확인"
            self._prepare_remote_stream(transport, "LAN")
            self._lan_sock, self._transport = sock, transport
            self.info = ConnectionInfo(True, "LAN (Ethernet Socket)", f"TCPIP::{ip}::{port}::SOCKET", idn, ip, port)
            self.last_error = ""
            return self.info
        except Exception as exc:
            self.last_error = f"[{stage}] {ip}:{port} / PC {self.source_ip or source_ip or '자동 경로'}\n{type(exc).__name__}: {exc}"
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
        handle = None
        stage = "COM 포트 열기"
        try:
            if not HAS_PYSERIAL:
                raise ConnectionError("RS232에는 pyserial이 필요합니다. 제공된 통신라이브러리_설치.cmd를 실행하세요.")
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
            self.last_error = "PyVISA가 설치되어 있지 않습니다. python -m pip install pyvisa 후 NI-VISA 또는 제조사 VISA를 설치하세요. LAN은 PyVISA 없이 사용 가능합니다."
            return ConnectionInfo()
        errors = []
        try:
            self._visa_rm = pyvisa.ResourceManager()
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
                        inst.close()
            self.last_error = "\n".join(errors) or "VISA에서 해당 장비를 찾지 못했습니다. USB는 후면 USB Device(B형) 포트와 USBTMC 활성화, GPIB는 주소/어댑터 드라이버를 확인하세요."
        except Exception as exc:
            self.last_error = f"VISA 초기화/검색 실패: {exc}\nNI-VISA 또는 사용 중인 USB/GPIB 어댑터의 VISA 드라이버를 확인하세요."
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
        self.source_ip = AutoNetworkAligner.select_source_ip(adapter_idx, ip) if adapter_idx else (
            AutoNetworkAligner.normalize_ipv4(source_ip) if source_ip else "")
        stage = "TCP 연결"
        sock = None
        try:
            sock = socket.create_connection((ip, port), timeout,
                    source_address=(self.source_ip, 0) if self.source_ip else None)
            self.source_ip = sock.getsockname()[0]
            stage = "TCP 성공, *IDN? 확인"
            idn = TelnetLineTransport(sock, timeout, eol).query("*IDN?", identity=True)
            if not self._validate_idn(idn):
                raise InstrumentProtocolError(f"다른 장비 응답: {idn}")
            self.last_error = f"[진단 성공] TCP 및 {self.expected_model} 식별 확인: {idn}"
        except Exception as exc:
            self.last_error = f"[{stage}] {type(exc).__name__}: {exc}"
        finally:
            if sock is not None:
                close_lan_socket(sock)
        self.last_diagnostic = self.build_lan_diagnostic_report(ip, port)
        return self.last_diagnostic

    def _drop_connection(self):
        if self._lan_sock is not None:
            close_lan_socket(self._lan_sock)
        for handle in (self._serial_port, self._visa_inst, self._visa_rm):
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

    def send_command(self, cmd):
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
                self._write(command)
                if self._query("*OPC?", timeout=self.operation_timeout) != "1":
                    raise InstrumentProtocolError("명령 처리 완료를 확인하지 못했습니다.")
                error = self._query("ERR?")
                if not re.match(r"^\+?0\s*(?:,|$)", error):
                    # A valid error reply means the stream is still synchronized.
                    # Stop output and verify it, but retain a healthy connection.
                    self._write("STBY")
                    if self._query("*OPC?", timeout=self.operation_timeout) != "1" or self._query("OPER?") != "0":
                        raise InstrumentProtocolError(f"{self.profile.model} 오류 {error}; STBY 확인 실패")
                    raise InstrumentCommandError(f"{self.profile.model} 오류: {error}. STBY 확인 완료, 연결 유지. 다음 행은 실행하지 않습니다.")
                self.record_event(f"[명령 완료] {command} | {time.monotonic() - started:.3f}s", notify=True)
            except InstrumentCommandError as exc:
                self.last_error = str(exc)
                self.record_event(str(exc), notify=True)
                raise
            except Exception as exc:
                context = f"명령 [{command}] / {time.monotonic() - started:.3f}초 경과 / 완료 대기 한도 {self.operation_timeout:g}초"
                self._fail_control_session(context, exc)
                raise

    def disconnect(self, standby=True):
        with self._lock:
            try:
                self.cancel_event.clear()
                if self.info.connected and standby:
                    self.send_command("STBY")
                    if self._transport is not None:
                        self._transport.write("LOCAL")
                        if self._transport.query("*OPC?") != "1":
                            raise InstrumentProtocolError("LOCAL 완료 응답을 확인하지 못했습니다.")
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

            self._build_grid_skeleton()
            self.show_commands = False
            self.set_command_visibility(False)

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
                cell_f.grid(row=0, column=c_idx, sticky="nsew")
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
            self.header_frame.grid_columnconfigure(6, weight=1)
            self.cells_frame.grid_columnconfigure(6, weight=1)
            self.canvas.bind("<Configure>", self._resize_columns)
            self.canvas.bind("<Enter>", lambda _e: self._bind_mousewheel(True))
            self.canvas.bind("<Leave>", lambda _e: self._bind_mousewheel(False))

        def _on_xscroll(self, *args) -> None:
            self.canvas.xview(*args)
            self.header_canvas.xview(*args)

        def set_command_visibility(self, visible):
            self.show_commands = bool(visible)
            cells = [self.header_frame.winfo_children()[7]] + [row["frames"][7] for row in self._row_widgets]
            for cell in cells:
                if visible and not cell.winfo_manager():
                    cell.grid()
                elif not visible and cell.winfo_manager():
                    cell.grid_remove()
            self.header_frame.update_idletasks()
            self.cells_frame.update_idletasks()
            self._resize_columns()
            self._on_frame_configure()

        def _resize_columns(self, _event=None):
            base = sum(spec[1] for index, spec in enumerate(self.COL_SPECS) if index != 7 or getattr(self, "show_commands", False))
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
                cell_box.grid(row=r_idx, column=c_idx, sticky="nsew")
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

            self._row_widgets.append({"frames": row_frames, "entries": entries, "labels": labels})

        def _update_single_row_visual(self, r_idx: int, st: SequenceStep, is_active: bool = False) -> None:
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
            self.on_row_select_callback(r_idx)

        def _commit_cell(self, r_idx: int, c_idx: int) -> None:
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
            if target_c in self._row_widgets[target_r]["entries"]:
                ent = self._row_widgets[target_r]["entries"][target_c]
                ent.focus_set()
                ent.select_range(0, "end")
                self.scroll_to_row(target_r)

        def _on_cell_paste_event(self, _event: tk.Event, r_idx: int, c_idx: int) -> str:
            try:
                clip_text = self.clipboard_get()
            except Exception:
                return "break"

            if not clip_text or not clip_text.strip():
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
        """Fluke 5560A 통합 원격 제어 GUI 프로그램 v6.1.0"""

        def __init__(self) -> None:
            super().__init__()
            self.title("Fluke 교정기 통합 제어 프로그램 v6.1.0 (USB / LAN / RS232 / GPIB)")
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
            self.adapters = AutoNetworkAligner.list_ethernet_interfaces()

            self._setup_styles()
            self._build_ui()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._refresh_oper_badge()
            self._load_sample_into_sheet(SAMPLE_POWER_TABLE, silent=True)

            self._log(f"통신 기록 자동 저장: {self.conn_mgr.log_path}")
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
            tk.Label(header, text="5560A · 5522A · 5520A · 5080A   /   v6.1.0", bg="#F5F7FA",
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
                                ("resistance", "저항"), ("power", "전력")):
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

        def _start_io(self, label, operation, complete, on_error=None, stop_sequence=True):
            if self._io_busy:
                return False
            if stop_sequence:
                self._on_seq_stop_auto(silent=True)
            self._io_busy = True
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
            if self._io_busy:
                self.conn_mgr.request_abort()
                self.after(200, self._on_close)
                return
            self._on_seq_stop_auto(silent=True)
            try:
                self.conn_mgr.disconnect()
            except Exception:
                pass
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
            self.sequence_details = tk.Frame(container, bg="white", pady=8)
            self._sequence_details_open = False
            self.var_seq_start_row = tk.StringVar(value="1")
            self.var_seq_stop_row = tk.StringVar(value="63")
            self.var_seq_dwell_sec = tk.StringVar(value="3.0")
            self.var_seq_auto_oper = tk.BooleanVar(value=True)
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
            self._ui_button(controls, "일시정지", lambda: self._on_seq_stop_auto(silent=False))
            self._ui_button(controls, "재개", self._on_seq_resume_from_paused)
            self._ui_label(controls, "  유지")
            self._ui_entry(controls, self.var_seq_dwell_sec, 5)
            self._ui_label(controls, "초")
            ttk.Checkbutton(controls, text="자동 출력", variable=self.var_seq_auto_oper).pack(side="left", padx=6)
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
            if self._io_busy or self.seq_running:
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

        def _on_paste_into_sheet_button(self, replace_all: bool = False) -> None:
            if self._io_busy or self.seq_running:
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
            if replace_all:
                self.seq_steps = []
            self._handle_excel_paste_at_cell(clip_text, start_row_idx=start_r, start_col_idx=2)

        def _handle_excel_paste_at_cell(self, clip_text: str, start_row_idx: int, start_col_idx: int) -> None:
            if self._io_busy or self.seq_running:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            self._on_seq_stop_auto(silent=True)
            lines = [ln.strip() for ln in clip_text.splitlines() if ln.strip()]
            if not lines:
                return

            turns = self._get_coil_turns()
            use_coil = self.var_seq_use_coil.get()

            parsed_rows: List[Tuple[str, str, str, str]] = []
            for ln in lines:
                pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)
                if pf is not None:
                    parsed_rows.append(pf)

            if len(parsed_rows) == len(lines) and parsed_rows:
                if start_row_idx == 0 and len(parsed_rows) > 1:
                    self.seq_steps = []

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
                        break

                    if target_idx < len(self.seq_steps):
                        self.seq_steps[target_idx] = new_step
                    else:
                        self.seq_steps.append(new_step)

                for i, st in enumerate(self.seq_steps):
                    st.step_no = i + 1

                self.var_seq_stop_row.set(str(len(self.seq_steps)))
                self.excel_grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
                self._refresh_current_group_preview()
                self._log(
                    f"[엑셀 붙여넣기 완료] #{start_row_idx + 1}번 행부터 아래로 총 {len(parsed_rows)}개 행이 칸별로 자동 입력되었습니다."
                )
                return

            for offset, ln in enumerate(lines):
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
                except Exception:
                    pass

            self.var_seq_stop_row.set(str(len(self.seq_steps)))
            self.excel_grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
            self._refresh_current_group_preview()
            self._log(f"[열 붙여넣기 완료] #{start_row_idx + 1}번 행부터 아래로 {len(lines)}개 셀을 붙여넣었습니다.")

        def _handle_excel_cell_edited(self, r_idx: int, vi_c: str, fq_c: str, pf_c: str, nom_c: str) -> None:
            if self._io_busy or self.seq_running:
                if 0 <= r_idx < len(self.seq_steps):
                    self.excel_grid._update_single_row_visual(r_idx, self.seq_steps[r_idx], is_active=(r_idx == self.seq_current_idx))
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            if not (0 <= r_idx < len(self.seq_steps)):
                return
            st = self.seq_steps[r_idx]
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
                updated.status = st.status
                self.seq_steps[r_idx] = updated
                self.excel_grid._update_single_row_visual(r_idx, updated, is_active=(r_idx == self.seq_current_idx))
                self._refresh_current_group_preview()
            except Exception as e:
                self._log(f"[#{st.step_no}행 셀 입력 오류] {e}")

        def _on_add_blank_row(self) -> None:
            if self._io_busy or self.seq_running:
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
            if self._io_busy or self.seq_running:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            self._on_seq_stop_auto(silent=True)
            self.seq_steps = []
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
            if self._io_busy or self.seq_running:
                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")
                return
            if self._io_busy or self.seq_running:
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
                rebuilt_steps.append(rebuilt)
            self.seq_steps = rebuilt_steps
            self.excel_grid.render_steps(self.seq_steps, active_idx=-1)
            self._refresh_current_group_preview()
            self._refresh_current_group_preview()

        def _execute_sequence_step(self, index, on_complete=None):
            if not (0 <= index < len(self.seq_steps)):
                return False
            try:
                if not self._step_in_current_group(self.seq_steps[index]):
                    self._log(f"[#{index + 1}행 제외] 현재 그룹: {self._current_group_label()}")
                    return False
            except ValueError as exc:
                self._show_report("전류 그룹 확인", str(exc))
                return False
            if not self._ensure_connected_or_sim():
                return False
            self.seq_current_idx = self.seq_paused_idx = index
            step = self.seq_steps[index]
            if not step.scpi_cmd or step.func_type == "ERROR":
                self._show_report("행 설정 확인", f"#{step.step_no}행: {step.actual_out_desc}")
                return False
            step.status = "… 출력 안정화 대기"
            self.excel_grid._update_single_row_visual(index, step, is_active=True)
            self.excel_grid.scroll_to_row(index)
            self.var_active_row_banner.set(f"#{step.step_no}행 완료 응답 대기: {step.scpi_cmd}")
            self.lbl_active_row_banner.configure(bg="#FEF3C7", fg="#92400E")
            self._log(f"[#{step.step_no}행 시작] 조건=({step.vi_cond_raw}, {step.freq_cond_raw}, PF={step.pf_cond_raw}) | 명령={step.scpi_cmd}")
            def same_step():
                return index < len(self.seq_steps) and self.seq_steps[index] is step
            def done(_state):
                if not same_step():
                    self.seq_running = False
                    return
                for i, previous in enumerate(self.seq_steps):
                    if i != index and previous.status == "▶ 측정 진행 중":
                        previous.status = "✓ 완료"
                        self.excel_grid._update_single_row_visual(i, previous, is_active=False)
                step.status = "▶ 측정 진행 중"
                plan = self._seq_active_indices
                self.progress_var.set((plan.index(index) + 1) / len(plan) * 100 if index in plan else 100)
                self.var_active_row_banner.set(f"[#{step.step_no}/{len(self.seq_steps)}행 출력 확인] {step.nominal_raw} → {step.actual_out_desc}")
                self.lbl_active_row_banner.configure(bg="#DBEAFE", fg="#0F172A")
                self.excel_grid._update_single_row_visual(index, step, is_active=True)
                self._log(f"[#{step.step_no}행 완료 응답 확인] 유지시간 시작")
                if on_complete:
                    on_complete()
            def failed(exc):
                self.seq_running = False
                self.seq_after_id = None
                step.status = "⚠ 오류"
                if same_step():
                    self.excel_grid._update_single_row_visual(index, step, is_active=False)
                self.var_active_row_banner.set(f"#{step.step_no}행 중단 — {step.scpi_cmd} / 출력 상태는 상단과 본체에서 확인하세요.")
                self.lbl_active_row_banner.configure(bg="#FECACA", fg="#991B1B")
                self._log(f"[#{step.step_no}행 실행 오류] 명령={step.scpi_cmd} | {exc}")
            try:
                return self._send_and_log(step.scpi_cmd, auto_oper=self.var_seq_auto_oper.get(),
                                          on_done=done, on_error=failed, stop_sequence=False)
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

        def _on_seq_start_from_selected_or_range(self):
            if not self.seq_steps:
                self._log("먼저 시트에 측정값을 넣으세요.")
                return
            self._start_filtered_sequence(*self._get_start_and_stop_indices())

        def _on_seq_resume_from_paused(self):
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
                self._send_and_log("STBY", on_done=finished)
                return
            def completed():
                if not self.seq_running:
                    return
                if self.seq_steps[idx].breakpoint:
                    self.seq_steps[idx].status = "🛑 중단점 도달"
                    self.seq_running = False
                    self.seq_paused_idx = min(len(self.seq_steps) - 1, idx + 1)
                    self.var_active_row_banner.set(f"#{idx + 1}행 중단점 도달 — 다음 행부터 재개 가능")
                    self.excel_grid._update_single_row_visual(idx, self.seq_steps[idx], is_active=False)
                    return
                self.seq_after_id = self.after(dwell_ms, lambda: self._run_next_auto_step(idx + 1, stop_idx, dwell_ms))
            if not self._execute_sequence_step(idx, on_complete=completed):
                self.seq_running = False

        def _on_seq_stop_auto(self, silent: bool = False) -> None:
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

        def _on_seq_step_next(self):
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
            cmb_vunit = ttk.Combobox(pad, textvariable=self.var_v_unit, values=["uV", "mV", "V", "kV"], width=8, state="readonly")
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
            cmb_iunit = ttk.Combobox(pad, textvariable=self.var_i_unit, values=["uA", "mA", "A"], width=8, state="readonly")
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

            self.var_r_unit = tk.StringVar(value="KOHM")
            cmb_runit = ttk.Combobox(pad, textvariable=self.var_r_unit, values=["OHM", "KOHM", "MOHM", "GOHM"], width=10, state="readonly")
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
            ttk.Radiobutton(frm_pcalc, text="목표 유효전력(W/kW) + 기준 전압(V) 입력 (전류 자동 역산)", variable=self.var_p_calc_method, value="PW", command=self._update_power_preview).pack(side="left", padx=(0, 16))
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
            self.cmb_p_unit = ttk.Combobox(pad, textvariable=self.var_p_unit, values=["W", "KW", "A"], width=8, state="readonly")
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
                freq = float(self.var_v_freq.get()) if self.var_v_mode.get() == "ACV" else 0.0
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
                freq = float(self.var_i_freq.get()) if self.var_i_mode.get() == "ACI" else 0.0
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
                    self.lbl_p_second_input.configure(text="목표 유효전력 (W/kW):")
                else:
                    self.var_p_unit.set("A")
                    self.lbl_p_second_input.configure(text="목표 클램프 전류 (A):")

                res = self._builder().build_power_cmd(
                    mode=self.var_p_mode.get(),
                    calc_method=calc_method,
                    voltage_v=float(self.var_p_volt.get()),
                    current_or_power_val=float(self.var_p_val.get()),
                    power_unit=self.var_p_unit.get(),
                    freq_hz=float(self.var_p_freq.get()),
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

        def _send_and_log(self, cmd, auto_oper=False, on_done=None, on_error=None, stop_sequence=True):
            if not self._ensure_connected_or_sim():
                return False
            self.conn_mgr.operation_timeout = self._get_operation_timeout()
            full_cmd = f"{cmd}; OPER" if auto_oper else cmd
            self.conn_mgr.profile.validate_command(full_cmd)
            self.var_active_cmd_summary.set(f"전송 요청: {full_cmd}")
            self.is_operating = None
            self._refresh_oper_badge()
            def operation():
                self.conn_mgr.send_command(full_cmd)
                state = self.conn_mgr.query("OPER?").strip()
                if state not in ("0", "1"):
                    self.conn_mgr.disconnect(standby=False)
                    raise InstrumentProtocolError(f"출력 상태 확인 실패: {state}")
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
                                  operation, done, on_error=on_error, stop_sequence=stop_sequence)

        def _apply_voltage(self, auto_oper: bool) -> None:
            try:
                val = float(self.var_v_val.get())
                freq = float(self.var_v_freq.get()) if self.var_v_mode.get() == "ACV" else 0.0
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
                freq = float(self.var_i_freq.get()) if self.var_i_mode.get() == "ACI" else 0.0
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
                    voltage_v=float(self.var_p_volt.get()),
                    current_or_power_val=float(self.var_p_val.get()),
                    power_unit=self.var_p_unit.get(),
                    freq_hz=float(self.var_p_freq.get()),
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
            self.var_high_current_only = tk.BooleanVar(value=False)
            self.var_current_group_basis = tk.StringVar(value=self._group_basis)
            self.var_current_group_summary = tk.StringVar()
            self.group_panel = tk.Frame(container, bg="white")
            self.group_panel.pack(fill="x", pady=(0, 4))
            self.chk_high_current_only = ttk.Checkbutton(self.group_panel, text="큰 전류 그룹만 실행", variable=self.var_high_current_only,
                                                        command=self._on_current_group_changed)
            self.chk_high_current_only.pack(side="left", padx=(0, 12))
            self.group_summary_label = ttk.Label(self.group_panel, textvariable=self.var_current_group_summary,
                                                 foreground="#64748B", wraplength=850)
            self.group_summary_label.pack(side="left", fill="x", expand=True)
            self.group_panel.bind("<Configure>", lambda e: self.group_summary_label.configure(wraplength=max(300, e.width - 210)))
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

        def _refresh_current_group_preview(self):
            if not hasattr(self, 'var_current_group_summary'):
                return
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
            if self._io_busy or self.seq_running:
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
            if self._io_busy:
                return
            try:
                dwell = float(self.var_seq_dwell_sec.get())
                if not math.isfinite(dwell) or dwell < .2:
                    raise ValueError("유지시간은 0.2초 이상의 유한한 숫자여야 합니다.")
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
            self.seq_running = True
            self._log(f"[그룹 순차 시작] {self._current_group_label()} | 원래 행 순서: {', '.join(str(i + 1) for i in plan)} | 유지 {dwell:g}초")
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
    print(" Fluke 5560A Multi-Product Calibrator Controller v6.1.0 (CLI Mode)")
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
