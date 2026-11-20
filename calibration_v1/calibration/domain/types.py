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

TRUEVOLT_MODELS = ("34461A", "34465A")
DMM_MODELS = ("34401A", *TRUEVOLT_MODELS)


MAX_VOLTAGE_V = 1020.0          # 최대 AC/DC 전압 (1020 V)


MAX_CURRENT_A = 30.0            # 최대 연속 AC/DC 전류 (30 A, 무휴지 연속 출력)


MAX_RESISTANCE_OHM = 1.1e9      # 최대 저항 (1100 MΩ)


DEFAULT_LAN_PORT = 3490         # Fluke 5560A 공식 Ethernet Telnet 포트 (CRLF 종료)


HIGH_VOLTAGE_WARN_V = 30.0      # 고전압 주의 기준 (30 V 이상)


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
    dut_channel: int | None = None
    dut_channel_suffix: str = ""
    channel_hint: tuple[int, str] | None = None
    declared_kind: str | None = None

    @property
    def channel_key(self):
        return (self.dut_channel, self.dut_channel_suffix) if self.dut_channel is not None else None


CHANNEL_MEASUREMENT_NAMES = {
    "DCV": "DC 전압", "ACV": "AC 전압", "DCI": "DC 전류", "ACI": "AC 전류",
    "DCP": "DC 전력", "ACP": "AC 전력", "PF": "역률",
}


MEASUREMENT_NAMES = {**CHANNEL_MEASUREMENT_NAMES, "OHM": "저항", "FREQ": "주파수"}
