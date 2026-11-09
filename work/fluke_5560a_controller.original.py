#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fluke 5560A Multi-Product Calibrator Integrated Control Program (v5.2)
=======================================================================
핵심 업그레이드 (v5.2):
1. [⚡ 5560A IP 자동 탐지 & 내 PC IP 자동 변경 연결] 기능 탑재 (AutoNetworkAligner):
   - 랜선(LAN) 연결 시 5560A와 내 PC의 IP 대역이 달라도 자동으로 해결:
     ① 브로드캐스트 패킷 송출 후 Windows ARP 테이블(arp -a) 및 링크 이웃(Get-NetNeighbor)에서
        다른 대역에 숨어 있는 5560A 후보 IP를 자동 추출
     ② Windows 'netsh interface ipv4' 명령(관리자 권한 UAC 자동 요청 지원)을 통해
        내 PC 이더넷 어댑터에 다중 대역 IP(192.168.1.x, 192.168.0.x, 10.0.0.x, 169.254.x.x 및 사용자 지정 대역)를
        자동으로 할당하여 5560A의 실제 IP와 포트(3490/5025/23)를 고속 탐지
     ③ 탐지된 5560A의 IP 대역(예: 192.168.1.100이면 내 PC를 192.168.1.249로)에 맞춰
        내 PC의 랜카드 IP를 자동으로 변경한 뒤 즉시 연결!
     ④ 사용 종료 후 클릭 한 번으로 내 PC 랜카드를 원래의 자동 IP(DHCP) 상태로 되돌리는 [🔄 PC IP 자동(DHCP) 복구] 버튼 제공
2. 실제 엑셀(Excel)과 동일한 칸 분할 시트(ExcelSheetGrid) UI:
   - 모든 행과 열(A~G열)이 엑셀 격자 칸으로 나뉘어 있으며, 아무 칸에나 Ctrl+V를 누르면
     띄어쓰기나 탭으로 구분된 데이터가 클릭한 행부터 아래쪽으로 그대로 줄지어 입력됨
3. 직관적인 현재 측정 행(Active Row) 형광 노란색 강조 및 행별 [▶시작] / [🛑중단점] 제어

참고 문서:
- Fluke 5560A/5550A/5540A Remote Programmers Manual:
  https://www.sinetecautomation.com/backend/product_file/388760.pdf
- Fluke 5560A Product Specifications:
  https://xdevs.com/doc/Fluke/5560A/5560A___speng0000_1.pdf
"""

from __future__ import annotations

import os
import math
import re
import sys
import time
import socket
import tempfile
import subprocess
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

# Tkinter 선택적 임포트 (GUI 환경에서는 데스크톱 GUI 실행, 미설치 환경에서는 대화형 CLI 실행)
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
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
CANDIDATE_LAN_PORTS = (3490, 5025, 23)
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
            res = Fluke5560ACommandBuilder.build_power_cmd(
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
            scpi_cmd, _ = Fluke5560ACommandBuilder.build_voltage_cmd(
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
            scpi_cmd, actual_a, effective_a = Fluke5560ACommandBuilder.build_current_cmd(
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
            scpi_cmd, _ = Fluke5560ACommandBuilder.build_resistance_cmd(
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
# 4. 내 PC 랜카드 IP 자동 변경 및 5560A IP 교차 대역 자동 탐지 엔진 (AutoNetworkAligner)
# =====================================================================
class AutoNetworkAligner:
    """
    Fluke 5560A와 내 PC의 IP 대역이 서로 다를 때:
    1) 브로드캐스트 패킷 송출 + ARP 테이블(arp -a) + PowerShell Get-NetNeighbor 조회로
       물리적 랜선에 물려 있는 5560A의 숨겨진 IP 주소를 감지하고,
    2) Windows 'netsh interface ipv4' 명령(필요 시 UAC 관리자 권한 자동 승격)을 실행해
       내 PC의 유선 랜카드에 다중 대역 IP(또는 목표 대역 IP)를 자동으로 부여하여
       사용자가 수동으로 제어판에 들어가지 않아도 알아서 내 PC IP를 맞춰 연결해 줍니다.
    """

    COMMON_FLUKE_SUBNETS = [
        "169.254.1",   # Fluke 5560A 공장 출고 기본 Static IP (169.254.001.001 -> 169.254.1.1) 대역!
        "192.168.1",
        "192.168.0",
        "192.168.2",
        "192.168.10",
        "10.0.0",
        "172.16.0",
    ]

    @staticmethod
    def normalize_ipv4(ip_str: str) -> str:
        """
        Fluke 5560A 전면 패널 표기(예: 169.254.001.001, 192.168.001.100)처럼
        각 옥텟 앞에 0이 붙어 있을 때 8진수(Octal)로 오인되지 않도록 선행 0을 제거합니다.
        (매뉴얼 Page 11 권고사항 반영)
        """
        s = ip_str.strip()
        m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", s)
        if not m:
            return s
        octets = [int(m.group(i)) for i in range(1, 5)]
        if any(o < 0 or o > 255 for o in octets):
            return s
        return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}" 

    @staticmethod
    def is_windows_admin() -> bool:
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    @classmethod
    def list_ethernet_interfaces(cls) -> List[Dict[str, str]]:
        """
        Windows 'netsh interface ipv4 show interfaces'를 파싱하여
        유선 이더넷 어댑터 목록(Idx 번호 및 이름)을 반환합니다.
        Idx 번호를 사용하면 한글/영문 윈도우 인코딩 문제 없이 100% 정확하게 IP를 변경할 수 있습니다.
        """
        adapters: List[Dict[str, str]] = []
        if sys.platform == "win32":
            try:
                raw = subprocess.check_output(
                    ["netsh", "interface", "ipv4", "show", "interfaces"],
                    timeout=2.0,
                )
                try:
                    text = raw.decode("cp949")
                except UnicodeDecodeError:
                    text = raw.decode("utf-8", errors="ignore")

                for line in text.splitlines():
                    # 형식: Idx   Met   MTU   State   Name
                    m = re.match(r"^\s*(\d+)\s+\d+\s+\d+\s+(\S+)\s+(.+?)\s*$", line)
                    if not m:
                        continue
                    idx_str, state_str, name_str = m.group(1), m.group(2), m.group(3).strip()
                    low_name = name_str.lower()
                    if "loopback" in low_name or "루프백" in low_name:
                        continue
                    # 유선 랜카드(Ethernet / 이더넷)를 최우선 정렬
                    is_wired = any(k in low_name for k in ("ethernet", "이더넷", "로컬 영역", "local area", "eth")) and not any(
                        w in low_name for w in ("wi-fi", "wireless", "무선", "wlan", "bluetooth", "vethernet", "vmware", "virtual")
                    )
                    adapters.append(
                        {
                            "idx": idx_str,
                            "name": name_str,
                            "state": state_str,
                            "priority": "0" if is_wired else "1",
                        }
                    )
                adapters.sort(key=lambda x: (x["priority"], int(x["idx"])))
            except Exception:
                pass

        if not adapters:
            adapters.append({"idx": "Ethernet", "name": "이더넷 (기본 랜카드)", "state": "connected", "priority": "0"})
        return adapters

    @classmethod
    def run_netsh_batch(cls, netsh_cmds: List[str]) -> Tuple[bool, str]:
        """
        하나 이상의 netsh 명령을 실행합니다.
        현재 프로세스가 관리자 권한이 아니면 Windows UAC('예/아니요' 권한 승격 창)를 띄워 실행합니다.
        """
        if not netsh_cmds:
            return True, "실행할 명령 없음"

        if sys.platform != "win32":
            return False, "자동 PC IP 변경(netsh)은 Windows 환경에서 동작합니다."

        if cls.is_windows_admin():
            logs = []
            for cmd in netsh_cmds:
                proc = subprocess.run(cmd, shell=True, capture_output=True)
                out = proc.stdout.decode("cp949", errors="ignore").strip()
                err = proc.stderr.decode("cp949", errors="ignore").strip()
                logs.append(f"{cmd} -> {out or err or 'OK'}")
            return True, "\n".join(logs)

        # 일반 권한으로 실행된 경우: 임시 .bat 파일을 생성하고 ShellExecuteW("runas")로 관리자 승격 실행
        try:
            import ctypes
            bat_path = os.path.join(tempfile.gettempdir(), "fluke_5560a_ip_align.bat")
            done_flag = os.path.join(tempfile.gettempdir(), "fluke_5560a_ip_done.txt")
            if os.path.exists(done_flag):
                try:
                    os.remove(done_flag)
                except Exception:
                    pass

            with open(bat_path, "w", encoding="cp949", errors="ignore") as bf:
                bf.write("@echo off\r\n")
                for cmd in netsh_cmds:
                    bf.write(f"{cmd}\r\n")
                bf.write(f'echo DONE > "{done_flag}"\r\n')

            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", f'/c "{bat_path}"', None, 0
            )
            if int(ret) <= 32:
                return False, "Windows 관리자 권한(UAC) 승인이 취소되었거나 거부되었습니다."

            # 배치 파일 실행 완료 대기 (최대 8초)
            for _ in range(32):
                if os.path.exists(done_flag):
                    break
                time.sleep(0.25)
            time.sleep(0.8)  # Windows 네트워크 스택 바인딩 안정화 대기
            return True, "관리자 권한(UAC)으로 PC 랜카드 IP 설정을 완료했습니다."
        except Exception as e:
            return False, f"PC IP 자동 변경 중 오류 발생: {e}"

    @staticmethod
    def choose_safe_pc_ip(target_device_ip: str) -> Tuple[str, str]:
        """
        5560A의 IP(예: 192.168.1.100)와 충돌하지 않으면서 같은 대역으로 통신 가능한
        내 PC용 IP(예: 192.168.1.249)와 서브넷 마스크를 계산합니다.
        """
        norm_ip = AutoNetworkAligner.normalize_ipv4(target_device_ip)
        parts = [int(x) for x in norm_ip.split(".")]
        prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
        last_octet = parts[3]
        pc_host = 249 if last_octet != 249 else 248
        # Fluke 5560A 매뉴얼(Page 13): 공장 기본 서브넷 마스크는 255.255.255.0
        mask = "255.255.255.0"
        return f"{prefix}.{pc_host}", mask

    @classmethod
    def wake_and_collect_wire_ips(cls) -> List[str]:
        """
        랜선에 연결된 5560A가 다른 대역에 있더라도 ARP/Neighbor 캐시에 나타나도록
        브로드캐스트 패킷을 송출한 뒤 arp -a 및 PowerShell Get-NetNeighbor에서 모든 IPv4를 수집합니다.
        """
        # 1) 브로드캐스트 UDP 패킷 송출로 물리 링크 깨우기
        for bcast_ip in ("255.255.255.255", "169.254.1.255", "169.254.255.255"):
            for port in (DEFAULT_LAN_PORT, 5025, 111):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.settimeout(0.1)
                    s.sendto(b"\r\n", (bcast_ip, port))
                    s.close()
                except Exception:
                    pass

        found_ips: List[str] = []

        # 2) arp -a 조회
        try:
            out = subprocess.check_output(["arp", "-a"], timeout=1.5).decode("utf-8", errors="ignore")
            for m in re.finditer(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", out):
                ip = m.group(1)
                p = [int(x) for x in ip.split(".")]
                if p[0] in (0, 127, 224, 239, 255) or p[3] in (0, 255):
                    continue
                if ip not in found_ips:
                    found_ips.append(ip)
        except Exception:
            pass

        # 3) Windows PowerShell Get-NetNeighbor 추가 조회 (서브넷이 다른 직결 장비도 감지 가능)
        if sys.platform == "win32":
            try:
                ps_cmd = "Get-NetNeighbor -AddressFamily IPv4 | Where-Object {$_.State -ne 'Unreachable'} | Select-Object -ExpandProperty IPAddress"
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    timeout=2.0,
                ).decode("utf-8", errors="ignore")
                for m in re.finditer(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", out):
                    ip = m.group(1)
                    p = [int(x) for x in ip.split(".")]
                    if p[0] in (0, 127, 224, 239, 255) or p[3] in (0, 255):
                        continue
                    if ip not in found_ips:
                        found_ips.append(ip)
            except Exception:
                pass

        return found_ips

    @classmethod
    def align_pc_to_subnets_and_find_5560a(
        cls,
        adapter_idx: str,
        user_hint_ip: str = "169.254.1.1",
        target_port: int = DEFAULT_LAN_PORT,
        custom_pc_ip: str = "",
    ) -> Tuple[Optional[str], Optional[int], str, str]:
        """
        [핵심 자동화 루틴]
        1단계: 랜선에서 감지된 ARP/Neighbor IP 대역 + 사용자 입력 IP 대역 + 대표 계측기 대역(192.168.1.x, 192.168.0.x, 10.0.0.x 등)을 수집합니다.
        2단계: 한 번의 UAC 관리자 승인으로 내 PC 랜카드에 주 IP 및 다중 대역 별칭(Alias) IP들을 일괄 부여합니다.
        3단계: 모든 후보 대역에 대해 고속 멀티스레드 포트 스캔(3490, 5025, 23)을 수행하여 실제 5560A의 IP를 찾아냅니다.
        4단계: 찾은 5560A의 IP와 1:1로 매칭되는 PC IP를 반환합니다.
        반환값: (찾은_5560A_IP, 찾은_포트, 설정된_내PC_IP, 상세로그)
        """
        wire_ips = cls.wake_and_collect_wire_ips()
        target_subnets: List[str] = []

        # 사용자 입력 IP의 대역을 1순위로 등록 (선행 0 제거 정규화 포함)
        clean_hint = cls.normalize_ipv4(user_hint_ip)
        if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", clean_hint) and clean_hint != "0.0.0.0":
            sh = ".".join(clean_hint.split(".")[:3])
            target_subnets.append(sh)

        # Fluke 5560A 공장 출고 기본 Static IP 대역(169.254.1.x -> 169.254.001.001)을 최우선 포함!
        if "169.254.1" not in target_subnets:
            target_subnets.append("169.254.1")

        # ARP / Neighbor에서 발견된 대역들을 등록 (169.254 대역도 제외하지 않고 모두 포함!)
        for wip in wire_ips:
            sw = ".".join(wip.split(".")[:3])
            if sw not in target_subnets:
                target_subnets.append(sw)

        # 대표 계측기 대역들을 등록
        for csub in cls.COMMON_FLUKE_SUBNETS:
            if csub not in target_subnets:
                target_subnets.append(csub)

        # 내 PC 랜카드에 첫 번째 대역은 static 주 IP로, 나머지 대역들은 secondary alias(add address)로 한 번에 바인딩!
        # (일부 Windows 빌드에서 끝에 'none' 위치 인자가 거부되는 문제를 방지하기 위해 표준 구문과 키워드 구문을 모두 실행)
        primary_sub = target_subnets[0]
        norm_custom_pc = cls.normalize_ipv4(custom_pc_ip) if custom_pc_ip.strip() and custom_pc_ip.strip().upper() != "AUTO" else ""
        if norm_custom_pc and re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", norm_custom_pc):
            primary_pc_ip = norm_custom_pc
        else:
            primary_pc_ip = f"{primary_sub}.249" if clean_hint != f"{primary_sub}.249" else f"{primary_sub}.248"
        alt_pc_ip = f"{primary_sub}.2" if clean_hint not in (f"{primary_sub}.2", primary_pc_ip) else f"{primary_sub}.3"
        netsh_cmds = [
            f'netsh interface ipv4 set address name="{adapter_idx}" static {primary_pc_ip} 255.255.255.0',
            f'netsh interface ipv4 set address name="{adapter_idx}" source=static addr={primary_pc_ip} mask=255.255.255.0 gateway=none',
            f'netsh interface ipv4 add address name="{adapter_idx}" addr={alt_pc_ip} mask=255.255.255.0',
        ]
        for extra_sub in target_subnets[1:7]:
            netsh_cmds.append(
                f'netsh interface ipv4 add address name="{adapter_idx}" addr={extra_sub}.249 mask=255.255.255.0'
            )

        ok, msg = cls.run_netsh_batch(netsh_cmds)
        if not ok:
            return None, None, "", msg

        # Windows 랜카드가 새 고정 IP를 바인딩하고 물리 링크(Link-Up)를 재협상할 때까지 최대 3.0초 대기
        for _ in range(12):
            cur_ips = Fluke5560AConnectionManager.get_local_pc_ipv4_list()
            if primary_pc_ip in cur_ips or alt_pc_ip in cur_ips:
                time.sleep(0.6)
                break
            time.sleep(0.25)

        # 이제 내 PC가 모든 후보 대역(192.168.1.249, 192.168.0.249, 10.0.0.249 등)과 직접 통신 가능하므로
        # 고속 멀티스레드로 5560A가 열려 있는 IP와 포트를 탐색합니다!
        candidate_ips: List[str] = ["169.254.1.1"]  # 1순위: Fluke 5560A 공장 기본 Static IP (169.254.001.001)
        if clean_hint and clean_hint not in candidate_ips and clean_hint != "0.0.0.0":
            candidate_ips.insert(0, clean_hint)
        for wip in wire_ips:
            if wip not in candidate_ips:
                candidate_ips.append(wip)

        # 각 후보 대역에서 계측기에 자주 쓰이는 번호(.1, .100, .101, .50, .10, .200, .2)를 우선 스캔
        for sub in target_subnets[:8]:
            for host_no in (1, 100, 101, 50, 10, 200, 2, 102, 150, 250):
                cip = f"{sub}.{host_no}"
                if cip not in candidate_ips and host_no != 249:
                    candidate_ips.append(cip)

        ports_to_try = [target_port]
        for p in CANDIDATE_LAN_PORTS:
            if p not in ports_to_try:
                ports_to_try.append(p)

        def _probe(ip_a: str, pt: int, t_out: float = 0.35) -> Optional[Tuple[str, int]]:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(t_out)
            try:
                if s.connect_ex((ip_a, pt)) == 0:
                    s.close()
                    return (ip_a, pt)
            except Exception:
                pass
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            return None

        # 0차: 사용자가 직접 입력한 5560A IP(clean_hint)에 대해 넉넉한 타임아웃(1.0초)으로 2회 집중 연결 시도
        found_list: List[Tuple[str, int]] = []
        if clean_hint and clean_hint != "0.0.0.0":
            for _ in range(2):
                for pt in ports_to_try:
                    r0 = _probe(clean_hint, pt, 1.0)
                    if r0 and r0 not in found_list:
                        found_list.append(r0)
                if found_list:
                    break
                time.sleep(0.4)

        # 1차 고속 탐색
        if not found_list:
            tasks = [(ip, pt) for ip in candidate_ips for pt in ports_to_try]
            with ThreadPoolExecutor(max_workers=48) as ex:
                futs = [ex.submit(_probe, ip, pt, 0.45) for ip, pt in tasks]
                for f in as_completed(futs):
                    r = f.result()
                    if r and r not in found_list:
                        found_list.append(r)

        # 1차에서 못 찾았으면 상위 3개 대역(사용자 지정 대역 + 169.254.1.x + 192.168.1.x)의 1~254 전체를 정밀 스캔
        if not found_list:
            full_ips: List[str] = []
            for sub in target_subnets[:3]:
                for h in range(1, 255):
                    if h in (249, 2):
                        continue
                    cip = f"{sub}.{h}"
                    if cip not in candidate_ips:
                        full_ips.append(cip)
            with ThreadPoolExecutor(max_workers=64) as ex:
                futs = [ex.submit(_probe, ip, target_port, 0.25) for ip in full_ips]
                for f in as_completed(futs):
                    r = f.result()
                    if r and r not in found_list:
                        found_list.append(r)

        if found_list:
            best_ip, best_port = found_list[0]
            matched_sub = ".".join(best_ip.split(".")[:3])
            matched_pc_ip = f"{matched_sub}.249"
            return (
                best_ip,
                best_port,
                matched_pc_ip,
                f"5560A IP({best_ip}:{best_port}) 자동 탐지 성공 및 내 PC IP({matched_pc_ip}) 맞춤 완료!",
            )

        # 포트가 안 열렸을 때: Ping(ICMP) 응답 여부로 [물리/IP 문제]인지 [Network Security 방화벽/포트 차단]인지 정밀 진단!
        ping_ok = False
        if clean_hint and sys.platform == "win32":
            try:
                p_res = subprocess.run(
                    ["ping", "-n", "1", "-w", "900", clean_hint],
                    capture_output=True,
                    timeout=2.0,
                )
                ping_ok = (p_res.returncode == 0)
            except Exception:
                pass

        cur_pc_ips = Fluke5560AConnectionManager.get_local_pc_ipv4_list()
        cur_pc_str = ", ".join(cur_pc_ips) if cur_pc_ips else primary_pc_ip
        ping_diag = (
            f"🟢 [{clean_hint} Ping 응답 성공!] 물리적 랜선과 IP 대역은 연결되었으나 5560A가 포트({target_port}) 접속만 차단하고 있습니다!\n"
            f"👉 100% [Network Security - Allowed Host IP addresses] 제한 또는 [Remote Port Setup]의 Ethernet 스위치가 꺼져 있는 상태입니다!"
            if ping_ok
            else f"🔴 [{clean_hint} Ping 응답 없음] 현재 내 PC 적용 IP: [{cur_pc_str}]"
        )

        return (
            None,
            None,
            primary_pc_ip,
            f"{ping_diag}\n\n"
            f"✅ [5560A 본체 화면 필수 점검 4가지 (Setup > System Settings > Remote Port Setup)]\n"
            f"1. [Network Security - Allowed Host IP addresses] (매우 중요!):\n"
            f"   - Starting Address와 Ending Address에 다른 IP가 적혀 있으면 내 PC({primary_pc_ip} 또는 {alt_pc_ip})의 접속을 5560A가 방화벽처럼 차단합니다!\n"
            f"   - 화면 맨 아래 [Restore Ethernet Defaults (이더넷 기본값 복원)] 버튼을 한 번 누른 뒤 다시 DHCP만 OFF로 끄거나, Starting=0.0.0.0 / Ending=255.255.255.255 (또는 {primary_sub}.1 ~ {primary_sub}.254)로 맞춰주세요.\n"
            f"2. [Remote Port Setup 첫 화면의 Ethernet 스위치]:\n"
            f"   - Ethernet 버튼 옆의 토글 스위치가 반드시 [초록색(ON)]으로 켜져 있어야 합니다.\n"
            f"3. [Current IP Address 확인]:\n"
            f"   - Static IP Settings 아래의 [Current IP Address]에도 동일한 IP가 표시되는지 확인하세요. (0.0.0.0이면 랜선을 꽂은 상태에서 Static IP를 다시 입력 후 Enter)\n"
            f"4. [End of Line Character]: 기본값인 [CR/LF]로 두시면 됩니다.",
        )

    @classmethod
    def restore_dhcp(cls, adapter_idx: str) -> Tuple[bool, str]:
        """내 PC 랜카드를 원래의 자동 IP 받기(DHCP) 상태로 복원"""
        cmds = [f'netsh interface ipv4 set address name={adapter_idx} source=dhcp']
        return cls.run_netsh_batch(cmds)


# =====================================================================
# 5. 멀티 인터페이스 자동 탐색 및 통신 드라이버 (USB / GPIB / LAN)
# =====================================================================
class Fluke5560AConnectionManager:
    def __init__(self) -> None:
        self.info = ConnectionInfo()
        self._visa_rm = None
        self._visa_inst = None
        self._lan_sock: Optional[socket.socket] = None
        self._sim_oper_state: bool = False
        self._sim_last_out: str = "0 V, 0 HZ"

    @staticmethod
    def get_local_pc_ipv4_list() -> List[str]:
        ips: List[str] = []
        try:
            _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
            for ip in host_ips:
                if ip and not ip.startswith("127.") and ":" not in ip:
                    ips.append(ip)
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
            if primary_ip and primary_ip not in ips and not primary_ip.startswith("127."):
                ips.insert(0, primary_ip)
            s.close()
        except Exception:
            pass
        return ips

    @classmethod
    def discover_open_lan_targets(
        cls,
        user_ip: str,
        user_port: int = DEFAULT_LAN_PORT,
        deep_subnet_scan: bool = True,
    ) -> List[Tuple[str, int]]:
        ports_to_check = [user_port]
        for p in CANDIDATE_LAN_PORTS:
            if p not in ports_to_check:
                ports_to_check.append(p)

        priority_ips: List[str] = []
        norm_user_ip = AutoNetworkAligner.normalize_ipv4(user_ip)
        if norm_user_ip and norm_user_ip != "0.0.0.0":
            priority_ips.append(norm_user_ip)

        # Fluke 5560A 공장 출고 기본 Static IP (169.254.001.001 -> 169.254.1.1) 최우선 탐색
        if "169.254.1.1" not in priority_ips:
            priority_ips.append("169.254.1.1")

        for arp_ip in AutoNetworkAligner.wake_and_collect_wire_ips():
            if arp_ip not in priority_ips:
                priority_ips.append(arp_ip)

        local_ips = cls.get_local_pc_ipv4_list()
        for lip in local_ips:
            prefix = ".".join(lip.split(".")[:3])
            for host_num in (1, 100, 101, 200, 50, 10, 2):
                cand = f"{prefix}.{host_num}"
                if cand != lip and cand not in priority_ips:
                    priority_ips.append(cand)

        for fallback_ip in ("169.254.1.1", "192.168.1.100", "192.168.0.100", "10.0.0.100"):
            if fallback_ip not in priority_ips:
                priority_ips.append(fallback_ip)

        open_targets: List[Tuple[str, int]] = []

        def _probe_port(ip_addr: str, port_num: int, timeout_s: float = 0.35) -> Optional[Tuple[str, int]]:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout_s)
            try:
                if s.connect_ex((ip_addr, port_num)) == 0:
                    s.close()
                    return (ip_addr, port_num)
            except Exception:
                pass
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            return None

        tasks = [(ip, pt) for ip in priority_ips for pt in ports_to_check]
        with ThreadPoolExecutor(max_workers=32) as ex:
            futs = [ex.submit(_probe_port, ip, pt, 0.45) for ip, pt in tasks]
            for f in as_completed(futs):
                res = f.result()
                if res and res not in open_targets:
                    open_targets.append(res)

        if open_targets or not deep_subnet_scan:
            return open_targets

        subnet_ips: List[str] = []
        for lip in local_ips[:3]:
            if lip.startswith("169.254."):
                continue
            prefix = ".".join(lip.split(".")[:3])
            for h in range(1, 255):
                cand = f"{prefix}.{h}"
                if cand != lip and cand not in priority_ips:
                    subnet_ips.append(cand)

        if subnet_ips:
            with ThreadPoolExecutor(max_workers=64) as ex:
                futs = [ex.submit(_probe_port, ip, user_port, 0.22) for ip in subnet_ips]
                for f in as_completed(futs):
                    res = f.result()
                    if res and res not in open_targets:
                        open_targets.append(res)

        return open_targets

    def scan_and_auto_connect(
        self,
        preferred_mode: str = "AUTO",
        lan_ip: str = "192.168.1.100",
        lan_port: int = DEFAULT_LAN_PORT,
        gpib_addr: int = 1,
        timeout_sec: float = 2.0,
        allow_raw_socket_fallback: bool = True,
    ) -> ConnectionInfo:
        self.disconnect()
        mode = preferred_mode.strip().upper()

        if mode == "SIMULATION":
            return self._connect_simulation()

        if mode in ("AUTO", "USB", "GPIB", "LAN (VISA)") and HAS_PYVISA:
            visa_result = self._try_visa_auto_scan(
                filter_mode=mode,
                gpib_addr=gpib_addr,
                timeout_sec=timeout_sec,
            )
            if visa_result.connected:
                return visa_result

        if mode in ("AUTO", "LAN", "LAN (SOCKET)"):
            norm_lan_ip = AutoNetworkAligner.normalize_ipv4(lan_ip)
            if norm_lan_ip and norm_lan_ip != "0.0.0.0":
                direct_res = self._try_lan_socket(
                    norm_lan_ip,
                    int(lan_port),
                    timeout_sec=1.5,
                    allow_open_port_fallback=allow_raw_socket_fallback,
                )
                if direct_res.connected:
                    return direct_res

            targets = self.discover_open_lan_targets(lan_ip, int(lan_port), deep_subnet_scan=True)
            for found_ip, found_port in targets:
                lan_result = self._try_lan_socket(
                    found_ip,
                    found_port,
                    timeout_sec=1.5,
                    allow_open_port_fallback=allow_raw_socket_fallback,
                )
                if lan_result.connected:
                    return lan_result

        diag_msg = self.build_lan_diagnostic_report(lan_ip.strip(), int(lan_port))
        raise ConnectionError(diag_msg)

    def _try_visa_auto_scan(
        self,
        filter_mode: str,
        gpib_addr: int,
        timeout_sec: float,
    ) -> ConnectionInfo:
        try:
            if self._visa_rm is None:
                self._visa_rm = pyvisa.ResourceManager()
            resources = list(self._visa_rm.list_resources())
        except Exception:
            return ConnectionInfo(connected=False)

        gpib_target = f"GPIB0::{gpib_addr}::INSTR"
        if gpib_target not in resources:
            resources.insert(0, gpib_target)

        def _priority_key(res: str) -> int:
            r = res.upper()
            if r.startswith("USB"):
                return 0
            if r.startswith("GPIB"):
                return 1
            if r.startswith("TCPIP"):
                return 2
            return 3

        resources.sort(key=_priority_key)

        for res in resources:
            r_up = res.upper()
            if filter_mode == "USB" and not r_up.startswith("USB"):
                continue
            if filter_mode == "GPIB" and not r_up.startswith("GPIB"):
                continue
            if filter_mode == "LAN (VISA)" and not r_up.startswith("TCPIP"):
                continue
            if not (r_up.startswith("USB") or r_up.startswith("GPIB") or r_up.startswith("TCPIP")):
                continue

            try:
                inst = self._visa_rm.open_resource(res)
                inst.timeout = int(timeout_sec * 1000)
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                idn = inst.query("*IDN?").strip()
                if any(k in idn.upper() for k in ("FLUKE", "5560", "5550", "5540", "5522", "5502")):
                    self._visa_inst = inst
                    if r_up.startswith("USB"):
                        itype = "USB (USBTMC)"
                    elif r_up.startswith("GPIB"):
                        itype = "GPIB (IEEE-488.2)"
                    else:
                        itype = "LAN (VISA TCPIP)"

                    self.info = ConnectionInfo(
                        connected=True,
                        interface_type=itype,
                        resource_name=res,
                        idn_string=idn,
                    )
                    self.send_command("REMOTE")
                    return self.info
                inst.close()
            except Exception:
                continue

        return ConnectionInfo(connected=False)

    @staticmethod
    def _strip_telnet_iac(data: bytes, sent_cmd: str = "") -> str:
        """
        Telnet IAC 제어 바이트(0xFF)를 제거하고,
        5560A의 Remote Interface가 'Terminal' 모드일 때 발생하는 명령 에코(Echo) 및 프롬프트('5560A>', '>')까지
        깨끗하게 정리하여 'Terminal' / 'Computer' 두 모드 모두 완벽하게 동작하도록 처리합니다.
        """
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            if b == 255:
                i += 3
                continue
            out.append(b)
            i += 1
        text = out.decode("utf-8", errors="ignore")
        lines = []
        sent_up = sent_cmd.strip().upper()
        for line in text.replace("\r", "\n").splitlines():
            clean_l = re.sub(r"^(?:5560A>|5550A>|5540A>|>)\s*", "", line.strip(), flags=re.IGNORECASE).strip()
            if not clean_l:
                continue
            if sent_up and clean_l.upper() == sent_up:
                continue
            lines.append(clean_l)
        return " ".join(lines).strip()

    def _try_lan_socket(
        self,
        ip: str,
        port: int = DEFAULT_LAN_PORT,
        timeout_sec: float = 1.8,
        allow_open_port_fallback: bool = True,
    ) -> ConnectionInfo:
        norm_ip = AutoNetworkAligner.normalize_ipv4(ip)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect((norm_ip, int(port)))
            sock.settimeout(0.25)
            try:
                _ = sock.recv(1024)
            except Exception:
                pass

            # 5560A가 'Terminal' 모드로 되어 있더라도 자동으로 'Computer'(COMP) 응답 모드로 전환 (매뉴얼 Page 64: COMM_MODE TELNET, COMP)
            sock.settimeout(timeout_sec)
            try:
                sock.sendall(b"\r\nCOMM_MODE TELNET, COMP\r\n")
                time.sleep(0.08)
                sock.settimeout(0.15)
                _ = sock.recv(1024)
            except Exception:
                pass

            sock.settimeout(timeout_sec)
            sock.sendall(b"*IDN?\r\n")
            raw_bytes = b""
            try:
                raw_bytes = sock.recv(2048)
            except Exception:
                pass
            raw = self._strip_telnet_iac(raw_bytes, sent_cmd="*IDN?")

            if any(k in raw.upper() for k in ("FLUKE", "5560", "5550", "5540", "5522", "5502")) or (
                allow_open_port_fallback and int(port) == DEFAULT_LAN_PORT
            ):
                self._lan_sock = sock
                idn_txt = raw if raw else f"FLUKE 5560A (Port {port} Connected at {norm_ip})"
                self.info = ConnectionInfo(
                    connected=True,
                    interface_type="LAN (Ethernet Socket)",
                    resource_name=f"TCPIP::{norm_ip}::{port}::SOCKET",
                    idn_string=idn_txt,
                    discovered_ip=norm_ip,
                    discovered_port=int(port),
                )
                self.send_command("REMOTE")
                return self.info
            sock.close()
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
        return ConnectionInfo(connected=False)

    def build_lan_diagnostic_report(self, target_ip: str, target_port: int) -> str:
        local_ips = self.get_local_pc_ipv4_list()
        arp_ips = AutoNetworkAligner.wake_and_collect_wire_ips()
        local_ip_str = ", ".join(local_ips) if local_ips else "감지된 랜카드 IP 없음"
        arp_str = ", ".join(arp_ips[:8]) if arp_ips else "없음"

        return (
            f"현재 상태로 연결된 Fluke 5560A 장비를 찾을 수 없습니다.\n\n"
            f"💡 [원클릭 자동 해결 방법]\n"
            f"상단의 주황색 [⚡ 5560A IP 자동탐지 + 내 PC IP 자동변경] 버튼을 누르면,\n"
            f"프로그램이 알아서 내 PC 랜카드에 다중 대역 IP를 부여해 5560A를 찾고 내 PC IP를 맞춰 연결합니다!\n\n"
            f"📊 [현재 네트워크 상태]\n"
            f"• 내 PC 랜카드 IP : {local_ip_str}\n"
            f"• 목표 5560A IP : {target_ip} (포트: {target_port})\n"
            f"• 랜선에 감지된 이웃 IP : {arp_str}"
        )

    def _connect_simulation(self) -> ConnectionInfo:
        self._sim_oper_state = False
        self._sim_last_out = "0 V, 0 HZ"
        self.info = ConnectionInfo(
            connected=True,
            interface_type="SIMULATION (가상 장비 모드)",
            resource_name="SIM::FLUKE_5560A::INSTR",
            idn_string="FLUKE,5560A,SIM-9901234,1.12+1.05",
        )
        return self.info

    def send_command(self, cmd: str) -> None:
        if not self.info.connected:
            raise ConnectionError("장비가 연결되어 있지 않습니다.")

        clean_cmd = cmd.strip()
        if self.info.interface_type.startswith("SIMULATION"):
            for part in [p.strip() for p in clean_cmd.split(";") if p.strip()]:
                pu = part.upper()
                if pu == "OPER":
                    self._sim_oper_state = True
                elif pu in ("STBY", "*RST"):
                    self._sim_oper_state = False
                elif pu.startswith("OUT "):
                    self._sim_last_out = part[4:].strip()
            return

        if self._visa_inst is not None:
            self._visa_inst.write(clean_cmd)
        elif self._lan_sock is not None:
            self._lan_sock.sendall((clean_cmd + "\r\n").encode("utf-8"))

    def query(self, cmd: str) -> str:
        if not self.info.connected:
            raise ConnectionError("장비가 연결되어 있지 않습니다.")

        clean_cmd = cmd.strip()
        if self.info.interface_type.startswith("SIMULATION"):
            cu = clean_cmd.upper()
            if cu == "*IDN?":
                return self.info.idn_string
            if cu == "OPER?":
                return "1" if self._sim_oper_state else "0"
            if cu == "OUT?":
                return self._sim_last_out
            if cu == "ERR?":
                return '0,"No error"'
            if cu == "*OPC?":
                return "1"
            return f"SIM_ACK ({clean_cmd})"

        if self._visa_inst is not None:
            return self._visa_inst.query(clean_cmd).strip()
        if self._lan_sock is not None:
            self._lan_sock.sendall((clean_cmd + "\r\n").encode("utf-8"))
            raw = self._lan_sock.recv(2048)
            return self._strip_telnet_iac(raw, sent_cmd=clean_cmd)

        raise ConnectionError("유효한 통신 핸들이 없습니다.")

    def disconnect(self) -> None:
        if self.info.connected:
            try:
                self.send_command("STBY; LOCAL")
            except Exception:
                pass
        if self._visa_inst is not None:
            try:
                self._visa_inst.close()
            except Exception:
                pass
            self._visa_inst = None
        if self._lan_sock is not None:
            try:
                self._lan_sock.close()
            except Exception:
                pass
            self._lan_sock = None
        self.info = ConnectionInfo(connected=False)


# =====================================================================
# 6. 실제 엑셀(Excel)처럼 칸이 나뉘고 Ctrl+V 시 밑으로 바로 들어가는 시트 위젯
# =====================================================================
if HAS_TKINTER:
    class ExcelSheetGrid(tk.Frame):
        COL_SPECS = [
            ("행(#)", 56, "center", False),
            ("제어", 76, "center", False),
            ("A: 전압/전류 조건", 145, "center", True),
            ("B: 주파수 / 조건1", 110, "center", True),
            ("C: 역률 (PF)", 135, "center", True),
            ("D: 명목값 (출력목표)", 125, "e", True),
            ("E: 5560A 실제 출력", 215, "w", False),
            ("F: 전송 SCPI 명령", 230, "w", False),
            ("G: 측정 상태", 110, "center", False),
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

        def _build_grid_skeleton(self) -> None:
            self.header_canvas = tk.Canvas(self, height=30, bg="#E2E8F0", highlightthickness=0)
            self.header_frame = tk.Frame(self.header_canvas, bg="#94A3B8")
            self.header_canvas.create_window((0, 0), window=self.header_frame, anchor="nw")
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
            self.canvas.bind("<Enter>", lambda _e: self._bind_mousewheel(True))
            self.canvas.bind("<Leave>", lambda _e: self._bind_mousewheel(False))

        def _on_xscroll(self, *args) -> None:
            self.canvas.xview(*args)
            self.header_canvas.xview(*args)

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
                        text="▶시작",
                        bg="#DCFCE7",
                        fg="#15803D",
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
                row_bg = "#FDE047"
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
        """Fluke 5560A 통합 원격 제어 GUI 프로그램 v5.2"""

        def __init__(self) -> None:
            super().__init__()
            self.title("Fluke 5560A 통합 제어 프로그램 v5.2 (5560A IP 자동탐지 & 내 PC IP 자동변경 + 엑셀 시트 복붙)")
            self.geometry("1280x930")
            self.minsize(1140, 840)
            self.configure(bg="#F4F6F9")

            self.conn_mgr = Fluke5560AConnectionManager()
            self.is_operating = False

            self.seq_steps: List[SequenceStep] = []
            self.seq_current_idx: int = -1
            self.seq_paused_idx: int = -1
            self.seq_running: bool = False
            self.seq_after_id: Optional[str] = None
            self.adapters = AutoNetworkAligner.list_ethernet_interfaces()

            self._setup_styles()
            self._build_ui()
            self._load_sample_into_sheet(SAMPLE_POWER_TABLE, silent=True)

            local_ips = self.conn_mgr.get_local_pc_ipv4_list()
            pc_ip_info = ", ".join(local_ips) if local_ips else "감지 안 됨"
            self._log(
                f"시스템 준비 완료 (현재 내 PC IP: {pc_ip_info}). "
                f"랜선 연결이 안 될 때는 상단의 주황색 [⚡ 5560A IP 자동탐지 + 내 PC IP 자동변경] 버튼을 누르세요!"
            )

        def _setup_styles(self) -> None:
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass

            style.configure("TFrame", background="#F4F6F9")
            style.configure("Card.TFrame", background="#FFFFFF", relief="solid", borderwidth=1)
            style.configure("TLabel", background="#FFFFFF", foreground="#1E293B", font=("Malgun Gothic", 10))
            style.configure("SubHeader.TLabel", background="#FFFFFF", foreground="#0F172A", font=("Malgun Gothic", 11, "bold"))
            style.configure("TNotebook", background="#F4F6F9")
            style.configure("TNotebook.Tab", font=("Malgun Gothic", 10, "bold"), padding=(14, 7))

        def _build_ui(self) -> None:
            banner = tk.Frame(self, bg="#0F172A", height=50)
            banner.pack(fill="x", side="top")
            tk.Label(
                banner,
                text="FLUKE 5560A MULTI-PRODUCT CALIBRATOR CONTROLLER v5.2",
                bg="#0F172A",
                fg="#FACC15",
                font=("Consolas", 14, "bold"),
                padx=16,
                pady=10,
            ).pack(side="left")

            self.lbl_conn_badge = tk.Label(
                banner,
                text="● DISCONNECTED",
                bg="#334155",
                fg="#E2E8F0",
                font=("Consolas", 10, "bold"),
                padx=12,
                pady=6,
            )
            self.lbl_conn_badge.pack(side="right", padx=16, pady=8)

            self._build_connection_panel()
            self._build_safety_bar()

            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True, padx=14, pady=(4, 4))

            self.tab_sequence = ttk.Frame(self.notebook, style="Card.TFrame")
            self.tab_voltage = ttk.Frame(self.notebook, style="Card.TFrame")
            self.tab_current = ttk.Frame(self.notebook, style="Card.TFrame")
            self.tab_resistance = ttk.Frame(self.notebook, style="Card.TFrame")
            self.tab_power = ttk.Frame(self.notebook, style="Card.TFrame")

            self.notebook.add(self.tab_sequence, text=" 📊 엑셀 시트 복사·붙여넣기 순차 제어 (Excel Grid) ")
            self.notebook.add(self.tab_voltage, text=" ⚡ DC / AC 전압 ")
            self.notebook.add(self.tab_current, text=" 🧲 DC / AC 전류 & 턴코일 ")
            self.notebook.add(self.tab_resistance, text=" Ω 저항 (Resistance) ")
            self.notebook.add(self.tab_power, text=" 🔋 DC / AC 전력 & 턴코일 ")

            self._build_sequence_tab()
            self._build_voltage_tab()
            self._build_current_tab()
            self._build_resistance_tab()
            self._build_power_tab()

            self._build_console_panel()

            self.bind("<F8>", lambda _e: self._on_seq_step_next())
            self.bind("<F9>", lambda _e: self._on_seq_toggle_breakpoint(self.excel_grid.selected_row_idx))

        def _build_connection_panel(self) -> None:
            frame = tk.LabelFrame(
                self,
                text=" 1. 통신 인터페이스 자동 인식 & 5560A IP 자동 탐지 및 내 PC 랜카드 IP 자동 변경 ",
                bg="#FFFFFF",
                fg="#0F172A",
                font=("Malgun Gothic", 9, "bold"),
                padx=10,
                pady=5,
            )
            frame.pack(fill="x", padx=14, pady=(4, 2))

            # 1줄: 기본 연결 컨트롤 + 원클릭 PC IP 자동 맞춤 버튼
            row1 = tk.Frame(frame, bg="#FFFFFF")
            row1.pack(fill="x", pady=(0, 3))

            tk.Label(row1, text="모드:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 3))
            self.var_conn_mode = tk.StringVar(value="AUTO")
            ttk.Combobox(
                row1,
                textvariable=self.var_conn_mode,
                values=["AUTO", "LAN", "USB", "GPIB", "SIMULATION"],
                width=10,
                state="readonly",
            ).pack(side="left", padx=(0, 8))

            tk.Label(row1, text="내 PC 랜카드:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 3))
            adapter_choices = [f"{a['idx']}: {a['name']}" for a in self.adapters]
            self.var_adapter_choice = tk.StringVar(value=adapter_choices[0])
            ttk.Combobox(
                row1,
                textvariable=self.var_adapter_choice,
                values=adapter_choices,
                width=18,
                state="readonly",
            ).pack(side="left", padx=(0, 8))

            tk.Label(row1, text="5560A IP:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 2))
            self.var_lan_ip = tk.StringVar(value="169.254.1.1")
            ttk.Entry(row1, textvariable=self.var_lan_ip, width=14, font=("Consolas", 10, "bold")).pack(side="left", padx=(0, 5))

            tk.Label(row1, text="내 PC 변경IP:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 2))
            self.var_custom_pc_ip = tk.StringVar(value="AUTO")
            ttk.Entry(row1, textvariable=self.var_custom_pc_ip, width=13, font=("Consolas", 9)).pack(side="left", padx=(0, 5))

            tk.Label(row1, text="포트:", bg="#FFFFFF").pack(side="left", padx=(0, 2))
            self.var_lan_port = tk.StringVar(value=str(DEFAULT_LAN_PORT))
            ttk.Entry(row1, textvariable=self.var_lan_port, width=5, font=("Consolas", 10)).pack(side="left", padx=(0, 6))

            tk.Label(row1, text="GPIB:", bg="#FFFFFF").pack(side="left", padx=(0, 2))
            self.var_gpib_addr = tk.StringVar(value="1")
            ttk.Entry(row1, textvariable=self.var_gpib_addr, width=3).pack(side="left", padx=(0, 8))

            tk.Button(
                row1,
                text="⚡ 5560A IP 자동탐지 + 내 PC IP 자동변경 연결",
                bg="#EA580C",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=11,
                pady=4,
                command=self._on_auto_align_pc_ip_and_connect,
            ).pack(side="left", padx=(0, 5))

            tk.Button(
                row1,
                text="🔍 일반 자동 연결",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=10,
                pady=4,
                command=self._on_connect_clicked,
            ).pack(side="left", padx=(0, 5))

            tk.Button(
                row1,
                text="🔄 PC IP 원래대로(DHCP) 복구",
                bg="#0D9488",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=9,
                pady=4,
                command=self._on_restore_pc_dhcp_clicked,
            ).pack(side="left", padx=(0, 5))

            tk.Button(
                row1,
                text="🧪 시뮬레이션",
                bg="#475569",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9),
                relief="flat",
                padx=8,
                pady=4,
                command=self._on_sim_connect_clicked,
            ).pack(side="left", padx=(0, 4))

            tk.Button(
                row1,
                text="해제",
                bg="#E2E8F0",
                fg="#1E293B",
                font=("Malgun Gothic", 9),
                relief="flat",
                padx=7,
                pady=4,
                command=self._on_disconnect_clicked,
            ).pack(side="left")

            local_ips = self.conn_mgr.get_local_pc_ipv4_list()
            pc_ip_txt = ", ".join(local_ips) if local_ips else "감지된 IP 없음"
            self.var_idn_display = tk.StringVar(
                value=f"현재 내 PC IP: [{pc_ip_txt}]  |  랜선 연결이 안 될 때 [⚡ 5560A IP 자동탐지 + 내 PC IP 자동변경 연결]을 누르면 알아서 PC IP를 맞춰 연결합니다!"
            )
            tk.Label(
                frame,
                textvariable=self.var_idn_display,
                bg="#FFFFFF",
                fg="#475569",
                font=("Consolas", 9),
            ).pack(anchor="w")

        def _get_selected_adapter_id(self) -> str:
            raw = self.var_adapter_choice.get().strip()
            if ":" in raw:
                return raw.split(":", 1)[0].strip()
            return raw

        def _on_auto_align_pc_ip_and_connect(self) -> None:
            """
            [사용자 요청 핵심 기능]
            1) 5560A의 IP를 자동 탐지하고,
            2) 내 PC의 랜카드 IP를 5560A와 통신 가능한 동일 대역 IP(예: .249)로 자동 변경한 뒤,
            3) 즉시 5560A에 연결합니다!
            """
            adapter_id = self._get_selected_adapter_id()
            hint_ip = self.var_lan_ip.get().strip()
            try:
                port = int(self.var_lan_port.get() or DEFAULT_LAN_PORT)
            except ValueError:
                port = DEFAULT_LAN_PORT

            self._log(
                f"[자동 IP 맞춤 시작] 내 PC 랜카드(Idx={adapter_id})에 다중 대역 IP를 부여하고 5560A의 IP를 자동 탐색합니다..."
            )
            self.update_idletasks()

            custom_pc = self.var_custom_pc_ip.get().strip()
            found_ip, found_port, new_pc_ip, msg = AutoNetworkAligner.align_pc_to_subnets_and_find_5560a(
                adapter_idx=adapter_id,
                user_hint_ip=hint_ip,
                target_port=port,
                custom_pc_ip=custom_pc,
            )
            self._log(f"[자동 IP 맞춤 결과] {msg}")

            if found_ip:
                self.var_lan_ip.set(found_ip)
                self.var_lan_port.set(str(found_port or port))
                self.var_conn_mode.set("LAN")
                self._on_connect_clicked()
                if self.conn_mgr.info.connected:
                    messagebox.showinfo(
                        "5560A 자동 탐지 및 PC IP 변경 연결 성공",
                        f"Fluke 5560A IP({found_ip}:{found_port})를 탐지하여\n"
                        f"내 PC 랜카드 IP를 '{new_pc_ip}'로 자동 변경하고 연결을 완료했습니다!",
                    )
            else:
                local_ips = self.conn_mgr.get_local_pc_ipv4_list()
                self.var_idn_display.set(
                    f"내 PC IP 변경됨: [{', '.join(local_ips) or new_pc_ip}]  |  5560A 본체: Remote Interface=Computer, Ethernet DHCP=OFF 확인!"
                )
                messagebox.showwarning("PC IP 변경 완료 (5560A 응답 대기)", msg)

        def _on_restore_pc_dhcp_clicked(self) -> None:
            adapter_id = self._get_selected_adapter_id()
            ok, msg = AutoNetworkAligner.restore_dhcp(adapter_id)
            local_ips = self.conn_mgr.get_local_pc_ipv4_list()
            self.var_idn_display.set(f"내 PC IP(DHCP 복원): [{', '.join(local_ips)}]  |  {msg}")
            self._log(f"[PC IP 자동(DHCP) 복원] {msg}")
            if ok:
                messagebox.showinfo("PC IP 원상복구 완료", "내 PC 랜카드 설정을 원래대로(자동 IP 받기 / DHCP) 복원했습니다.")
            else:
                messagebox.showwarning("PC IP 복원 안내", msg)

        def _build_safety_bar(self) -> None:
            bar = tk.Frame(self, bg="#FFFFFF", highlightbackground="#CBD5E1", highlightthickness=1, padx=12, pady=5)
            bar.pack(fill="x", padx=14, pady=2)

            self.lbl_output_state = tk.Label(
                bar,
                text="■ STANDBY (출력 대기 / 안전 상태)",
                bg="#DCFCE7",
                fg="#15803D",
                font=("Malgun Gothic", 10, "bold"),
                width=32,
                pady=4,
            )
            self.lbl_output_state.pack(side="left", padx=(0, 12))

            tk.Button(
                bar,
                text="▶ OPER (출력 활성화)",
                bg="#16A34A",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=14,
                pady=4,
                command=self._on_oper_clicked,
            ).pack(side="left", padx=3)

            tk.Button(
                bar,
                text="■ STBY (출력 즉시 차단)",
                bg="#DC2626",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=14,
                pady=4,
                command=self._on_stby_clicked,
            ).pack(side="left", padx=3)

            tk.Button(
                bar,
                text="⚠ 비상 초기화 (*RST; STBY)",
                bg="#F59E0B",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=10,
                pady=4,
                command=self._on_reset_clicked,
            ).pack(side="left", padx=6)

            self.var_active_cmd_summary = tk.StringVar(value="최근 전송 명령: 없음")
            tk.Label(
                bar,
                textvariable=self.var_active_cmd_summary,
                bg="#FFFFFF",
                fg="#0F172A",
                font=("Consolas", 10, "bold"),
            ).pack(side="right", padx=6)

        # -----------------------------------------------------------------
        # 탭 0: 엑셀 스타일 스프레드시트 그리드
        # -----------------------------------------------------------------
        def _build_sequence_tab(self) -> None:
            container = tk.Frame(self.tab_sequence, bg="#FFFFFF", padx=12, pady=8)
            container.pack(fill="both", expand=True)

            top_bar = tk.Frame(container, bg="#F8FAFC", highlightbackground="#CBD5E1", highlightthickness=1, padx=10, pady=6)
            top_bar.pack(fill="x", pady=(0, 5))

            tk.Button(
                top_bar,
                text="📋 선택한 행부터 아래로 붙여넣기 (Ctrl+V)",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=12,
                pady=4,
                command=lambda: self._on_paste_into_sheet_button(replace_all=False),
            ).pack(side="left", padx=(0, 6))

            tk.Button(
                top_bar,
                text="📋 시트 전체 새로 붙여넣기",
                bg="#0369A1",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=10,
                pady=4,
                command=lambda: self._on_paste_into_sheet_button(replace_all=True),
            ).pack(side="left", padx=(0, 6))

            tk.Button(
                top_bar,
                text="➕ 빈 행 아래에 추가",
                bg="#334155",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9),
                relief="flat",
                padx=8,
                pady=4,
                command=self._on_add_blank_row,
            ).pack(side="left", padx=(0, 6))

            tk.Button(
                top_bar,
                text="🗑 시트 비우기",
                bg="#E2E8F0",
                fg="#0F172A",
                font=("Malgun Gothic", 9),
                relief="flat",
                padx=8,
                pady=4,
                command=self._on_clear_sheet,
            ).pack(side="left", padx=(0, 12))

            self.var_seq_use_coil = tk.BooleanVar(value=False)
            tk.Checkbutton(
                top_bar,
                text="클램프 턴코일 배율 적용 (전류/전력 자동 환산):",
                variable=self.var_seq_use_coil,
                bg="#F8FAFC",
                font=("Malgun Gothic", 9, "bold"),
                command=self._recalculate_all_grid_steps,
            ).pack(side="left", padx=(2, 4))

            self.var_seq_coil_turns = tk.StringVar(value="50")
            ent_seq_turns = ttk.Entry(top_bar, textvariable=self.var_seq_coil_turns, width=5, font=("Consolas", 10, "bold"))
            ent_seq_turns.pack(side="left", padx=(0, 4))
            ent_seq_turns.bind("<KeyRelease>", lambda _e: self._recalculate_all_grid_steps())
            tk.Label(top_bar, text="배", bg="#F8FAFC", font=("Malgun Gothic", 9)).pack(side="left", padx=(0, 6))

            for t_v, t_l in (("1", "1x(직결)"), ("10", "10x"), ("50", "50x")):
                tk.Button(
                    top_bar,
                    text=t_l,
                    bg="#E0F2FE",
                    fg="#0369A1",
                    font=("Malgun Gothic", 8, "bold"),
                    relief="flat",
                    padx=6,
                    command=lambda v=t_v: (
                        self.var_seq_use_coil.set(v != "1"),
                        self.var_seq_coil_turns.set(v),
                        self._recalculate_all_grid_steps(),
                    ),
                ).pack(side="left", padx=2)

            preset_bar = tk.Frame(container, bg="#FFFFFF")
            preset_bar.pack(fill="x", pady=(0, 5))
            tk.Label(preset_bar, text="예시 성적서 시트 전환:", bg="#FFFFFF", fg="#475569", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 6))
            for lbl, sample_txt in (
                ("① AC 전력 시트 (63행, V/I·주파수·역률 1/-0.5/0.5)", SAMPLE_POWER_TABLE),
                ("② AC 전압 시트 (12행, 100Hz/1kHz)", SAMPLE_ACV_TABLE),
                ("③ AC 전류 시트 (12행, 100Hz/1kHz)", SAMPLE_ACI_TABLE),
                ("④ DC 전압 시트 (16행, Spec/Range 자동무시)", SAMPLE_DCV_TABLE),
            ):
                tk.Button(
                    preset_bar,
                    text=lbl,
                    bg="#F1F5F9",
                    fg="#0F172A",
                    font=("Malgun Gothic", 8, "bold"),
                    relief="solid",
                    bd=1,
                    padx=8,
                    pady=2,
                    command=lambda t=sample_txt: self._load_sample_into_sheet(t, silent=False),
                ).pack(side="left", padx=3)

            tk.Label(
                preset_bar,
                text="💡 팁: 엑셀처럼 아무 칸이나 클릭하고 Ctrl+V를 누르면 복사한 줄들이 그 칸부터 아래로 그대로 들어갑니다!",
                bg="#FFFFFF",
                fg="#0369A1",
                font=("Malgun Gothic", 9, "bold"),
            ).pack(side="right", padx=4)

            status_banner = tk.Frame(container, bg="#FEF9C3", highlightbackground="#EAB308", highlightthickness=2, padx=12, pady=6)
            status_banner.pack(fill="x", pady=(0, 6))

            self.var_active_row_banner = tk.StringVar(
                value="▶ 현재 대기 중 — 각 행 왼쪽의 [▶시작] 버튼을 누르거나 하단 [▶ 지정/선택 행부터 자동 시작]을 누르세요."
            )
            self.lbl_active_row_banner = tk.Label(
                status_banner,
                textvariable=self.var_active_row_banner,
                bg="#FEF9C3",
                fg="#854D0E",
                font=("Malgun Gothic", 11, "bold"),
                anchor="w",
            )
            self.lbl_active_row_banner.pack(side="left", fill="x", expand=True)

            self.progress_var = tk.DoubleVar(value=0.0)
            self.progress_bar = ttk.Progressbar(status_banner, variable=self.progress_var, maximum=100.0, length=180)
            self.progress_bar.pack(side="right", padx=(10, 0))

            self.excel_grid = ExcelSheetGrid(
                container,
                on_paste_text_callback=self._handle_excel_paste_at_cell,
                on_cell_edited_callback=self._handle_excel_cell_edited,
                on_row_start_callback=self._handle_row_direct_start,
                on_row_breakpoint_callback=self._on_seq_toggle_breakpoint,
                on_row_select_callback=self._handle_row_selected,
            )
            self.excel_grid.pack(fill="both", expand=True, pady=(0, 6))

            ctrl_bar = tk.Frame(container, bg="#EFF6FF", highlightbackground="#93C5FD", highlightthickness=1, padx=10, pady=7)
            ctrl_bar.pack(fill="x")

            tk.Label(ctrl_bar, text="시작 행(#):", bg="#EFF6FF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 3))
            self.var_seq_start_row = tk.StringVar(value="1")
            ttk.Entry(ctrl_bar, textvariable=self.var_seq_start_row, width=4, font=("Consolas", 10, "bold")).pack(side="left", padx=(0, 8))

            tk.Label(ctrl_bar, text="목표 중단 행(#):", bg="#EFF6FF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 3))
            self.var_seq_stop_row = tk.StringVar(value="63")
            ttk.Entry(ctrl_bar, textvariable=self.var_seq_stop_row, width=4, font=("Consolas", 10, "bold")).pack(side="left", padx=(0, 8))

            tk.Label(ctrl_bar, text="유지시간(초):", bg="#EFF6FF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 3))
            self.var_seq_dwell_sec = tk.StringVar(value="3.0")
            ttk.Entry(ctrl_bar, textvariable=self.var_seq_dwell_sec, width=4, font=("Consolas", 10, "bold")).pack(side="left", padx=(0, 8))

            self.var_seq_auto_oper = tk.BooleanVar(value=True)
            tk.Checkbutton(
                ctrl_bar,
                text="자동 OPER",
                variable=self.var_seq_auto_oper,
                bg="#EFF6FF",
                font=("Malgun Gothic", 9, "bold"),
            ).pack(side="left", padx=(0, 8))

            tk.Button(
                ctrl_bar,
                text="▶ 지정/선택 행부터 자동 시작",
                bg="#16A34A",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=11,
                pady=5,
                command=self._on_seq_start_from_selected_or_range,
            ).pack(side="left", padx=3)

            tk.Button(
                ctrl_bar,
                text="⏸ 현재 행 즉시 중단 (Pause)",
                bg="#DC2626",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=11,
                pady=5,
                command=lambda: self._on_seq_stop_auto(silent=False),
            ).pack(side="left", padx=3)

            tk.Button(
                ctrl_bar,
                text="⏯ 중단된 행부터 이어서 시작",
                bg="#EA580C",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=11,
                pady=5,
                command=self._on_seq_resume_from_paused,
            ).pack(side="left", padx=3)

            tk.Button(
                ctrl_bar,
                text="🛑 선택 행 중단점(F9)",
                bg="#991B1B",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=9,
                pady=5,
                command=lambda: self._on_seq_toggle_breakpoint(self.excel_grid.selected_row_idx),
            ).pack(side="left", padx=3)

            tk.Button(
                ctrl_bar,
                text="▼ 다음 행 출력(F8)",
                bg="#0284C7",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=9,
                pady=5,
                command=self._on_seq_step_next,
            ).pack(side="left", padx=(8, 3))

            tk.Button(
                ctrl_bar,
                text="▲ 이전 행",
                bg="#475569",
                fg="#FFFFFF",
                font=("Malgun Gothic", 9),
                relief="flat",
                padx=7,
                pady=5,
                command=self._on_seq_step_prev,
            ).pack(side="left", padx=2)

            tk.Button(
                ctrl_bar,
                text="⚡ 선택 행 단독 출력",
                bg="#0F172A",
                fg="#FACC15",
                font=("Malgun Gothic", 9, "bold"),
                relief="flat",
                padx=9,
                pady=5,
                command=self._on_seq_output_selected,
            ).pack(side="left", padx=3)

        def _get_coil_turns(self) -> float:
            try:
                t = float(self.var_seq_coil_turns.get())
                return t if t > 0 else 1.0
            except ValueError:
                return 1.0

        def _load_sample_into_sheet(self, sample_txt: str, silent: bool = False) -> None:
            self._on_seq_stop_auto(silent=True)
            turns = self._get_coil_turns()
            steps = SmartClipboardSequenceParser.build_sequence_steps(
                raw_text=sample_txt,
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
            self.var_active_row_banner.set(
                f"▶ 엑셀 시트에 총 {len(steps)}개 행 로드 완료 — 각 행의 [▶시작]을 누르거나 아무 셀에나 Ctrl+V로 붙여넣으세요."
            )
            self.lbl_active_row_banner.configure(bg="#FEF9C3", fg="#854D0E")
            if not silent:
                self._log(f"[엑셀 시트 로드] 총 {len(steps)}개 행이 엑셀 칸에 맞춰 등록되었습니다.")

        def _on_paste_into_sheet_button(self, replace_all: bool = False) -> None:
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
                            step_no=step_num,
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
                self._log(
                    f"[엑셀 붙여넣기 완료] #{start_row_idx + 1}번 행부터 아래로 총 {len(parsed_rows)}개 행이 칸별로 자동 입력되었습니다."
                )
                return

            for offset, ln in enumerate(lines):
                target_idx = start_row_idx + offset
                while target_idx >= len(self.seq_steps):
                    dummy = SmartClipboardSequenceParser.build_Single_step_from_fields(
                        step_no=len(self.seq_steps) + 1,
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
                        step_no=target_idx + 1,
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
            self._log(f"[열 붙여넣기 완료] #{start_row_idx + 1}번 행부터 아래로 {len(lines)}개 셀을 붙여넣었습니다.")

        def _handle_excel_cell_edited(self, r_idx: int, vi_c: str, fq_c: str, pf_c: str, nom_c: str) -> None:
            if not (0 <= r_idx < len(self.seq_steps)):
                return
            st = self.seq_steps[r_idx]
            if (vi_c, fq_c, pf_c, nom_c) == (st.vi_cond_raw, st.freq_cond_raw, st.pf_cond_raw, st.nominal_raw):
                return
            try:
                updated = SmartClipboardSequenceParser.build_Single_step_from_fields(
                    step_no=st.step_no,
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
            except Exception as e:
                self._log(f"[#{st.step_no}행 셀 입력 오류] {e}")

        def _on_add_blank_row(self) -> None:
            new_no = len(self.seq_steps) + 1
            step = SmartClipboardSequenceParser.build_Single_step_from_fields(
                step_no=new_no,
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
            self.excel_grid.scroll_to_row(len(self.seq_steps) - 1)

        def _on_clear_sheet(self) -> None:
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

        def _recalculate_all_grid_steps(self) -> None:
            if not self.seq_steps:
                return
            turns = self._get_coil_turns()
            new_steps: List[SequenceStep] = []
            try:
                for st in self.seq_steps:
                    rebuilt = SmartClipboardSequenceParser.build_Single_step_from_fields(
                        step_no=st.step_no,
                        vi_cond_raw=st.vi_cond_raw,
                        freq_cond_raw=st.freq_cond_raw,
                        pf_cond_raw=st.pf_cond_raw,
                        nominal_raw=st.nominal_raw,
                        use_coil=self.var_seq_use_coil.get(),
                        coil_turns=turns,
                        breakpoint=st.breakpoint,
                    )
                    rebuilt.status = st.status
                    new_steps.append(rebuilt)
                self.seq_steps = new_steps
                self.excel_grid.render_steps(self.seq_steps, active_idx=self.seq_current_idx)
            except Exception as e:
                self._log(f"[턴코일 재계산 경고] {e}")

        def _execute_sequence_step(self, index: int) -> bool:
            if not (0 <= index < len(self.seq_steps)):
                return False
            if not self._ensure_connected_or_sim():
                return False

            prev_idx = self.seq_current_idx
            for i, st in enumerate(self.seq_steps):
                if i < index and st.status == "▶ 측정 진행 중":
                    st.status = "✓ 완료"
                elif i == index:
                    st.status = "▶ 측정 진행 중"

            self.seq_current_idx = index
            self.seq_paused_idx = index
            step = self.seq_steps[index]
            auto_oper = self.var_seq_auto_oper.get()

            try:
                self._send_and_log(step.scpi_cmd, auto_oper=auto_oper)
                pct = ((index + 1) / len(self.seq_steps)) * 100.0
                self.progress_var.set(pct)
                self.var_active_row_banner.set(
                    f"⚡ [현재 측정 진행 중: #{step.step_no}번 행 / 총 {len(self.seq_steps)}행]  "
                    f"조건: {step.vi_cond_raw} | {step.freq_cond_raw} | 역률: {step.pf_cond_raw}  ➔  명목값: {step.nominal_raw}  ({step.scpi_cmd})"
                )
                self.lbl_active_row_banner.configure(bg="#FDE047", fg="#0F172A")
                self._log(
                    f"   └─ [진행 행 #{step.step_no}/{len(self.seq_steps)}] 조건=({step.vi_cond_raw}, {step.freq_cond_raw}, PF={step.pf_cond_raw}) "
                    f"| 명목값={step.nominal_raw} ➔ {step.actual_out_desc}"
                )
                if 0 <= prev_idx < len(self.seq_steps) and prev_idx != index:
                    self.excel_grid._update_single_row_visual(prev_idx, self.seq_steps[prev_idx], is_active=False)
                self.excel_grid._update_single_row_visual(index, step, is_active=True)
                self.excel_grid.scroll_to_row(index)
                return True
            except Exception as e:
                step.status = "⚠ 오류"
                self.excel_grid._update_single_row_visual(index, step, is_active=False)
                self._log(f"[#{step.step_no}행 실행 오류] {e}")
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

        def _on_seq_start_from_selected_or_range(self) -> None:
            if not self.seq_steps:
                messagebox.showwarning("시퀀스 없음", "먼저 엑셀 데이터를 붙여넣으세요.")
                return
            if not self._ensure_connected_or_sim():
                return

            try:
                dwell_sec = float(self.var_seq_dwell_sec.get())
                if dwell_sec < 0.2:
                    raise ValueError
            except ValueError:
                messagebox.showerror("시간 입력 오류", "유지시간(초)은 0.2초 이상의 숫자로 입력하세요.")
                return

            self._on_seq_stop_auto(silent=True)
            start_idx, stop_idx = self._get_start_and_stop_indices()
            for i in range(start_idx, stop_idx + 1):
                if not self.seq_steps[i].breakpoint:
                    self.seq_steps[i].status = "대기"

            self.seq_running = True
            self._log(
                f"[자동 순차 시작] #{start_idx + 1}번 행부터 #{stop_idx + 1}번 행까지 {dwell_sec:g}초 간격으로 순차 측정을 진행합니다."
            )
            self._run_next_auto_step(start_idx, stop_idx, int(dwell_sec * 1000))

        def _on_seq_resume_from_paused(self) -> None:
            if not self.seq_steps:
                return
            if self.seq_paused_idx < 0:
                self._on_seq_start_from_selected_or_range()
                return

            try:
                dwell_sec = float(self.var_seq_dwell_sec.get())
            except ValueError:
                dwell_sec = 3.0

            _, stop_idx = self._get_start_and_stop_indices()
            resume_idx = self.seq_paused_idx
            if resume_idx > stop_idx:
                stop_idx = len(self.seq_steps) - 1
                self.var_seq_stop_row.set(str(stop_idx + 1))

            self.seq_running = True
            self._log(f"[측정 재개] 중단되었던 #{resume_idx + 1}번 행부터 이어서 순차 실행을 시작합니다.")
            self._run_next_auto_step(resume_idx, stop_idx, int(dwell_sec * 1000))

        def _run_next_auto_step(self, idx: int, stop_idx: int, dwell_ms: int) -> None:
            if not self.seq_running:
                return

            if idx > stop_idx or idx >= len(self.seq_steps):
                for r_i, st in enumerate(self.seq_steps):
                    if st.status == "▶ 측정 진행 중":
                        st.status = "✓ 완료"
                        self.excel_grid._update_single_row_visual(r_i, st, is_active=False)
                self.seq_running = False
                self.seq_after_id = None
                self.var_active_row_banner.set(
                    f"✓ 지정된 목표 중단 행(#{stop_idx + 1}번 행)까지 측정 완료 — 안전을 위해 STBY로 전환했습니다."
                )
                self.lbl_active_row_banner.configure(bg="#DCFCE7", fg="#166534")
                self._on_stby_clicked()
                self._log(f"[목표 행 도달 완료] #{stop_idx + 1}번 행까지 출력을 완료하고 STBY로 전환했습니다.")
                return

            ok = self._execute_sequence_step(idx)
            if not ok:
                self.seq_running = False
                self.seq_after_id = None
                return

            if self.seq_steps[idx].breakpoint:
                self.seq_steps[idx].status = "🛑 중단점 도달"
                self.seq_running = False
                self.seq_after_id = None
                self.seq_paused_idx = min(len(self.seq_steps) - 1, idx + 1)
                self.var_active_row_banner.set(
                    f"🛑 [중단점(Breakpoint) 도달: #{idx + 1}번 행] 설정된 중단점에서 자동 일시중단되었습니다. ([⏯ 이어서 시작]으로 다음 행 진행)"
                )
                self.lbl_active_row_banner.configure(bg="#FECACA", fg="#991B1B")
                self.excel_grid._update_single_row_visual(idx, self.seq_steps[idx], is_active=False)
                self._log(f"[중단점 자동 멈춤] #{idx + 1}번 행(🛑 중단점) 출력을 마치고 일시중단했습니다.")
                return

            self.seq_after_id = self.after(dwell_ms, lambda: self._run_next_auto_step(idx + 1, stop_idx, dwell_ms))

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

        def _on_seq_step_next(self) -> None:
            self._on_seq_stop_auto(silent=True)
            if not self.seq_steps:
                return
            next_idx = self.seq_current_idx + 1
            if next_idx >= len(self.seq_steps):
                messagebox.showinfo("마지막 행 도달", "마지막 행까지 모두 출력했습니다. 안전을 위해 STBY로 전환합니다.")
                self._on_stby_clicked()
                return
            self._execute_sequence_step(next_idx)

        def _on_seq_step_prev(self) -> None:
            self._on_seq_stop_auto(silent=True)
            if not self.seq_steps:
                return
            prev_idx = max(0, self.seq_current_idx - 1)
            self._execute_sequence_step(prev_idx)

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
                text="턴코일(Turn Coil) 배율 환산 활성화 (체크 시: 5560A 실제 출력 전류 = 목표 클램프 전류 ÷ 코일 배율 N)",
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

            ttk.Label(pad, text="저항(Resistance) 출력 및 리드 보상(ZCOMP) 설정 (0 Ω ~ 1100 MΩ)", style="SubHeader.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))

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

        def _build_console_panel(self) -> None:
            frame = tk.LabelFrame(
                self,
                text=" 실시간 SCPI 원격 명령어 콘솔 및 통신 로그 ",
                bg="#FFFFFF",
                fg="#0F172A",
                font=("Malgun Gothic", 9, "bold"),
                padx=10,
                pady=4,
            )
            frame.pack(fill="both", expand=False, padx=14, pady=(0, 6))

            cmd_row = tk.Frame(frame, bg="#FFFFFF")
            cmd_row.pack(fill="x", pady=(0, 4))

            tk.Label(cmd_row, text="직접 명령어 입력:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 6))
            self.var_manual_cmd = tk.StringVar(value="*IDN?")
            ent_cmd = ttk.Entry(cmd_row, textvariable=self.var_manual_cmd, width=48, font=("Consolas", 10))
            ent_cmd.pack(side="left", padx=(0, 8))
            ent_cmd.bind("<Return>", lambda _e: self._on_manual_send(is_query=False))

            tk.Button(
                cmd_row,
                text="명령 전송 (Write)",
                bg="#334155",
                fg="#FFFFFF",
                relief="flat",
                padx=10,
                command=lambda: self._on_manual_send(is_query=False),
            ).pack(side="left", padx=3)

            tk.Button(
                cmd_row,
                text="응답 조회 (Query)",
                bg="#0284C7",
                fg="#FFFFFF",
                relief="flat",
                padx=10,
                command=lambda: self._on_manual_send(is_query=True),
            ).pack(side="left", padx=3)

            tk.Button(
                cmd_row,
                text="장비 에러 확인 (ERR?)",
                bg="#E2E8F0",
                fg="#0F172A",
                relief="flat",
                padx=10,
                command=self._on_check_err,
            ).pack(side="left", padx=6)

            self.txt_log = tk.Text(frame, height=4, bg="#0F172A", fg="#38BDF8", font=("Consolas", 9), wrap="word")
            self.txt_log.pack(fill="both", expand=True)

        def _update_voltage_preview(self) -> None:
            try:
                val = float(self.var_v_val.get())
                freq = float(self.var_v_freq.get()) if self.var_v_mode.get() == "ACV" else 0.0
                cmd, volts = Fluke5560ACommandBuilder.build_voltage_cmd(
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
                cmd, actual_a, effective_a = Fluke5560ACommandBuilder.build_current_cmd(
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
                    f"• [{coil_desc}] 클램프 미터 목표 전류: {effective_a:.6g} A  ➔  5560A 실제 출력 전류: {actual_a:.6g} A (최대 30A 한계 내 정상)\n"
                    f"• 생성될 SCPI 명령: {cmd}"
                )
            except Exception as e:
                self.var_i_preview.set(f"[설정 범위/입력 오류] {e}")

        def _update_resistance_preview(self) -> None:
            try:
                val = float(self.var_r_val.get())
                cmd, ohms = Fluke5560ACommandBuilder.build_resistance_cmd(
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

                res = Fluke5560ACommandBuilder.build_power_cmd(
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
                    f"• [전류 배분] 클램프 인가 유효 전류: {res['effective_current_a']:.6g} A  ➔  5560A 실제 출력 전류: {res['actual_current_a']:.6g} A (전압: {res['voltage_v']:g} V)\n"
                    f"• 생성될 SCPI 명령: {res['cmd']}"
                )
            except Exception as e:
                self.var_p_preview.set(f"[전력/턴코일 설정 오류] {e}")

        def _on_connect_clicked(self) -> None:
            try:
                info = self.conn_mgr.scan_and_auto_connect(
                    preferred_mode=self.var_conn_mode.get(),
                    lan_ip=self.var_lan_ip.get(),
                    lan_port=int(self.var_lan_port.get() or DEFAULT_LAN_PORT),
                    gpib_addr=int(self.var_gpib_addr.get() or 1),
                )
                if info.discovered_ip:
                    self.var_lan_ip.set(info.discovered_ip)
                    self.var_lan_port.set(str(info.discovered_port))
                self._update_connection_ui(info)
                self._log(f"[연결 성공] 인터페이스: {info.interface_type} | 리소스: {info.resource_name} | IDN: {info.idn_string}")
            except Exception as e:
                messagebox.showwarning("장비 연결 안내", str(e))
                self._log(f"[연결 실패] {e}")

        def _on_sim_connect_clicked(self) -> None:
            self.var_conn_mode.set("SIMULATION")
            self._on_connect_clicked()

        def _on_disconnect_clicked(self) -> None:
            self._on_seq_stop_auto(silent=True)
            self.conn_mgr.disconnect()
            self.is_operating = False
            self._refresh_oper_badge()
            self._update_connection_ui(self.conn_mgr.info)
            self._log("[연결 해제] 안전 대기(STBY) 전환 후 장비 연결을 종료했습니다.")

        def _update_connection_ui(self, info: ConnectionInfo) -> None:
            if info.connected:
                self.lbl_conn_badge.configure(
                    text=f"● CONNECTED [{info.interface_type}]",
                    bg="#16A34A",
                    fg="#FFFFFF",
                )
                self.var_idn_display.set(f"감지된 인터페이스: {info.interface_type}  |  리소스: {info.resource_name}  |  장비 식별(*IDN?): {info.idn_string}")
            else:
                self.lbl_conn_badge.configure(text="● DISCONNECTED", bg="#334155", fg="#E2E8F0")
                self.var_idn_display.set("연결된 장비 없음")

        def _ensure_connected_or_sim(self) -> bool:
            if not self.conn_mgr.info.connected:
                ans = messagebox.askyesno(
                    "장비 미연결 안내",
                    "현재 연결된 Fluke 5560A 장비가 없습니다.\n가상 시뮬레이션 모드(SIMULATION)로 자동 전환하여 명령 전송을 테스트하시겠습니까?",
                )
                if ans:
                    self._on_sim_connect_clicked()
                    return True
                return False
            return True

        def _send_and_log(self, cmd: str, auto_oper: bool = False) -> None:
            if not self._ensure_connected_or_sim():
                return
            full_cmd = f"{cmd}; OPER" if auto_oper else cmd
            self.conn_mgr.send_command(full_cmd)
            self.var_active_cmd_summary.set(f"최근 전송 명령: {full_cmd}")
            self._log(f"TX >> {full_cmd}")
            if auto_oper:
                self.is_operating = True
                self._refresh_oper_badge()

        def _apply_voltage(self, auto_oper: bool) -> None:
            try:
                val = float(self.var_v_val.get())
                freq = float(self.var_v_freq.get()) if self.var_v_mode.get() == "ACV" else 0.0
                cmd, _ = Fluke5560ACommandBuilder.build_voltage_cmd(
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
                cmd, actual_a, effective_a = Fluke5560ACommandBuilder.build_current_cmd(
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
                    self._log(f"   └─ [턴코일 {turns:g}x 환산] 클램프 목표 전류: {effective_a:g} A ➔ 5560A 실제 출력: {actual_a:g} A")
            except Exception as e:
                messagebox.showerror("전류/턴코일 설정 오류", str(e))

        def _apply_resistance(self, auto_oper: bool) -> None:
            try:
                val = float(self.var_r_val.get())
                cmd, _ = Fluke5560ACommandBuilder.build_resistance_cmd(
                    value=val,
                    unit=self.var_r_unit.get(),
                    zcomp=self.var_r_zcomp.get(),
                )
                self._send_and_log(cmd, auto_oper=auto_oper)
            except Exception as e:
                messagebox.showerror("저항 설정 오류", str(e))

        def _apply_power(self, auto_oper: bool) -> None:
            try:
                res = Fluke5560ACommandBuilder.build_power_cmd(
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
                    f"클램프 유효전류={res['effective_current_a']:.6g} A (5560A 실제 출력={res['actual_current_a']:.6g} A)"
                )
            except Exception as e:
                messagebox.showerror("전력 설정 오류", str(e))

        def _on_oper_clicked(self) -> None:
            if not self._ensure_connected_or_sim():
                return
            try:
                self.conn_mgr.send_command("OPER")
                self.is_operating = True
                self._refresh_oper_badge()
                self._log("TX >> OPER (출력 활성화)")
            except Exception as e:
                messagebox.showerror("통신 오류", str(e))

        def _on_stby_clicked(self) -> None:
            self._on_seq_stop_auto(silent=True)
            if not self._ensure_connected_or_sim():
                return
            try:
                self.conn_mgr.send_command("STBY")
                self.is_operating = False
                self._refresh_oper_badge()
                self._log("TX >> STBY (안전 대기 상태 전환 - 출력 차단)")
            except Exception as e:
                messagebox.showerror("통신 오류", str(e))

        def _on_reset_clicked(self) -> None:
            self._on_seq_stop_auto(silent=True)
            if not self._ensure_connected_or_sim():
                return
            try:
                self.conn_mgr.send_command("*RST; *CLS; STBY")
                self.is_operating = False
                self._refresh_oper_badge()
                self._log("TX >> *RST; *CLS; STBY (장비 초기화 및 출력 안전 차단 완료)")
            except Exception as e:
                messagebox.showerror("통신 오류", str(e))

        def _refresh_oper_badge(self) -> None:
            if self.is_operating:
                self.lbl_output_state.configure(
                    text="⚡ OPERATE (출력 인가 중 - 단자 접촉 주의!)",
                    bg="#FEE2E2",
                    fg="#DC2626",
                )
            else:
                self.lbl_output_state.configure(
                    text="■ STANDBY (출력 대기 / 안전 상태)",
                    bg="#DCFCE7",
                    fg="#15803D",
                )

        def _on_manual_send(self, is_query: bool) -> None:
            if not self._ensure_connected_or_sim():
                return
            cmd = self.var_manual_cmd.get().strip()
            if not cmd:
                return
            try:
                if is_query or cmd.endswith("?"):
                    self._log(f"TX >> {cmd}")
                    resp = self.conn_mgr.query(cmd)
                    self._log(f"RX << {resp}")
                else:
                    self.conn_mgr.send_command(cmd)
                    self._log(f"TX >> {cmd}")
            except Exception as e:
                self._log(f"[명령 오류] {e}")

        def _on_check_err(self) -> None:
            self.var_manual_cmd.set("ERR?")
            self._on_manual_send(is_query=True)

        def _log(self, msg: str) -> None:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.txt_log.insert("end", f"[{ts}] {msg}\n")
            self.txt_log.see("end")


def run_cli_interactive() -> None:
    mgr = Fluke5560AConnectionManager()
    print("=" * 88)
    print(" Fluke 5560A Multi-Product Calibrator Controller v5.2 (CLI Mode)")
    print("=" * 88)
    print(f"현재 PC 로컬 IPv4: {mgr.get_local_pc_ipv4_list()}")
    print(f"감지된 이더넷 어댑터: {AutoNetworkAligner.list_ethernet_interfaces()}")
    info = mgr.scan_and_auto_connect("SIMULATION")
    print(f"연결 상태: {info.interface_type} | {info.resource_name} | {info.idn_string}")


if __name__ == "__main__":
    if HAS_TKINTER and "--cli" not in sys.argv:
        app = Fluke5560AControllerApp()
        app.mainloop()
    else:
        run_cli_interactive()