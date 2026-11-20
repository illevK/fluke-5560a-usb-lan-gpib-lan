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
from calibration.domain.types import MAX_CURRENT_A, MAX_RESISTANCE_OHM, MAX_VOLTAGE_V
from calibration.domain.units import SIUnits


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
        max_voltage: float = MAX_VOLTAGE_V,
    ) -> Tuple[str, float]:
        norm_unit = cls.normalize_unit(unit)
        volts = SIUnits.to_base(value, norm_unit, "V")
        if abs(volts) > max_voltage:
            raise OutputRangeError(
                f"설정 전압({volts:.4g} V)이 최대 허용 한계(±{max_voltage:g} V)를 초과합니다."
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
        max_current: float = MAX_CURRENT_A,
    ) -> Tuple[float, float]:
        if not use_coil:
            coil_turns = 1.0
        if coil_turns <= 0:
            raise ValueError("턴코일 배율(Turn Ratio)은 0보다 큰 값이어야 합니다.")

        actual_output_a = target_current_a / coil_turns
        effective_clamp_a = actual_output_a * coil_turns

        if abs(actual_output_a) > max_current:
            max_effective = max_current * coil_turns
            raise OutputRangeError(
                f"실제 출력 전류({actual_output_a:.4f} A)가 장비 최대 출력 한계({max_current:g} A)를 초과합니다.\n"
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
        max_current: float = MAX_CURRENT_A,
    ) -> Tuple[str, float, float]:
        norm_unit = cls.normalize_unit(unit)
        target_a = SIUnits.to_base(input_value, norm_unit, "A")
        actual_a, effective_a = cls.calculate_turn_coil_current(
            target_current_a=target_a,
            coil_turns=coil_turns,
            use_coil=use_coil,
            max_current=max_current,
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
            raise OutputRangeError(
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
        if abs(voltage_v) > MAX_VOLTAGE_V:
            raise OutputRangeError(f"기준 전압의 절댓값은 {MAX_VOLTAGE_V:g} V 이하여야 합니다.")
        if voltage_v == 0:
            raise ValueError("기준 전압은 0이 아닌 값이어야 합니다.")

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


class OutputRangeError(ValueError):
    """A numeric setpoint falls outside the configured output limits."""


DUAL_CURRENT_DC_DEFAULT = 0.01


DUAL_CURRENT_AC_MIN = 0.033


def serial_baudrates(model):
    slow = (300, 600, 1200, 2400, 4800)
    fast = (19200, 38400, 57600, 115200)
    if model in ('5520A', '5522A'):
        return (*slow, 9600)
    if model in ('5560A', '5730A'):
        return (9600, *fast)
    return (*slow, 9600, *fast)


@dataclass(frozen=True)
class CalibratorProfile:
    model: str
    interfaces: tuple
    max_current: float
    max_resistance: float = 1.1e9
    max_voltage: float = MAX_VOLTAGE_V
    usb_virtual_com = False
    supports_power = True

    @property
    def terminal_note(self):
        return ("VI AUX ≤3.1 A / 30A >3.1 A" if self.model == "5560A"
                else "AUX <3 A / 20A ≥3 A (고전류 출력 시간 제한은 본체 설명서 참조)")

    def high_current(self, amps):
        return abs(amps) > 3.1 if self.model == "5560A" else abs(amps) >= 3.0

    @staticmethod
    def validate_dual_current_voltages(dc_voltage, ac_voltage):
        if not 0 < dc_voltage <= MAX_VOLTAGE_V:
            raise ValueError(f"DC 듀얼 전압은 0 V 초과 {MAX_VOLTAGE_V:g} V 이하로 입력하세요.")
        if not DUAL_CURRENT_AC_MIN <= ac_voltage <= MAX_VOLTAGE_V:
            raise ValueError(f"AC 듀얼 전압은 {DUAL_CURRENT_AC_MIN:g}~{MAX_VOLTAGE_V:g} V로 입력하세요.")

    def dual_current_command(self, command, *, dc_voltage=DUAL_CURRENT_DC_DEFAULT, ac_voltage=DUAL_CURRENT_AC_MIN):
        """Keep the current setpoint and add the selected primary voltage."""
        if self.model != "5560A":
            return None
        self.validate_dual_current_voltages(dc_voltage, ac_voltage)
        values = self.output_values(command)
        if values.keys() != {"A", "HZ"}:
            raise ValueError("듀얼 전류로 바꿀 OUT에는 전류와 주파수만 지정하세요.")
        amps, hz = values["A"], values["HZ"]
        if hz == 0:
            return f"OUT {dc_voltage:.12g} V, " + command[4:]
        # 5560A Product Specifications, p. 21, Power and Dual Output Limits.
        # 33 mV is the NORMAL output minimum in this band, not the AUX V minimum.
        if 45 <= hz <= 65 and .0012001 <= amps <= 20.5:
            return f"OUT {ac_voltage:.12g} V, " + command[4:] + "; WAVE SINE,SINE"
        return None

    def verify_dual_current_readback(self, command, reply):
        expected = self.output_values(command.split(";", 1)[0])
        fields = [field.strip().upper() for field in reply.split(",")]
        try:
            if len(fields) == 5 and (fields[1], fields[3]) == ("V", "A"):
                actual = [float(fields[i]) for i in (0, 2, 4)]
                if actual[0] > 0 and all(math.isclose(value, expected[unit], rel_tol=1e-5, abs_tol=1e-8)
                                         for value, unit in zip(actual, ("V", "A", "HZ"))):
                    return
        except ValueError:
            pass
        raise ValueError(f"5560A 듀얼 전압·전류 설정 확인 실패: {reply[:120]}. 본체 STBY를 확인하세요.")

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
        if abs(values.get("V", 0)) > self.max_voltage:
            raise OutputRangeError(f"{self.model}: 최대 전압 {self.max_voltage:g} V를 초과합니다.")
        if abs(values.get("A", 0)) > self.max_current:
            raise OutputRangeError(f"{self.model}: 코일 환산 후 실제 전류가 최대 {self.max_current:g} A를 초과합니다.")
        if values.get("HZ", 0) < 0:
            raise ValueError("주파수는 0 이상이어야 합니다.")
        if values.get("HZ", 0) > 0 and any(values.get(u, 1) <= 0 for u in ("V", "A")):
            raise ValueError("AC 전압·전류는 양수여야 합니다. 위상은 PHASE/DPF로 설정하세요.")
        ohms = values.get("OHM")
        if ohms is not None:
            if not 0 <= ohms <= self.max_resistance:
                raise OutputRangeError(f"{self.model}: 저항 범위 0~{self.max_resistance / 1e6:g} MΩ를 벗어났습니다.")
            if self.model == "5080A" and not any(math.isclose(ohms, r, rel_tol=1e-10, abs_tol=1e-12) for r in RESISTORS_5080A):
                raise ValueError("5080A는 고정 저항만 출력합니다: 0, 1/1.9/10/19/100/190 Ω, "
                                 "1/1.9/10/19/100/190 kΩ, 1/1.9/10/19/100/190 MΩ. 자동 반올림하지 않습니다.")
        self.validate_output_options(parts, values)
        return command

    def validate_output_options(self, parts, values):
        ohms = values.get("OHM")
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

class Fluke5730AProfile(CalibratorProfile):
    """5730A mainframe. Optional external amplifiers and the wideband output are separate."""
    usb_virtual_com = True
    supports_power = False
    resistors = tuple(value for value in RESISTORS_5080A if value <= 100e6)

    @property
    def terminal_note(self):
        return "5730A 본체 전류 ≤2.2 A · NORMAL (OUTPUT HI/LO)"

    def high_current(self, amps):
        return False  # All supported currents use the same pair of terminals.

    def lcomp_allowed(self, amps, hz):
        return False

    def validate_output_options(self, parts, values):
        if "A" in values and "V" in values:
            raise OutputRangeError("5730A 본체는 전압·전류를 동시에 출력할 수 없어 전력·역률 행을 실행할 수 없습니다.")
        hz = values.get("HZ", 0)
        if hz:
            limit = 10_000 if "A" in values else 1_199_900
            if not 10 <= hz <= limit:
                raise OutputRangeError(f"5730A 주파수 범위는 10~{limit:g} Hz입니다.")
            if values.get("A", 1) < 9e-6 or values.get("V", 1) < 220e-6:
                raise OutputRangeError("5730A AC 출력은 전류 9 µA, 전압 220 µV부터 가능합니다.")
            if values.get("V", 0) * hz > 2.2e7:
                raise OutputRangeError("5730A 본체의 AC 전압·주파수 곱 한계(2.2 × 10⁷ V·Hz)를 초과합니다.")
        ohms = values.get("OHM")
        if ohms is not None and ohms not in self.resistors:
            raise OutputRangeError("5730A 저항은 0 Ω 또는 1·1.9 계열의 고정값(최대 100 MΩ)을 선택하세요.")
        for part in parts:
            op = part.split()[0]
            if op in ("ZCOMP", "LCOMP", "DPF", "WAVE"):
                raise ValueError(f"5730A 본체는 {op} 명령을 지원하지 않습니다.")
            if op == "CUR_POST" and part not in ("CUR_POST NORMAL", "CUR_POST AUX"):
                raise ValueError("5730A 본체 전류 단자는 NORMAL 또는 AUX입니다.")
            if ohms is not None and part == "RCOMP ON" and ohms > 19e3:
                raise OutputRangeError("5730A의 2선 보상은 19 kΩ까지 지원합니다.")
            if ohms == 100e6 and part == "EXTSENSE ON":
                raise OutputRangeError("5730A의 100 MΩ은 2선식으로 설정하세요.")

    def prepare_generated(self, command, use_coil=False):
        out, _, options = command.partition(";")
        values = self.output_values(out)
        if "OHM" in values:
            mode = options.strip().removeprefix("ZCOMP ")
            sense = "ON" if mode == "WIRE4" and values["OHM"] < 100e6 else "OFF"
            command = f"{out}; *WAI; EXTSENSE {sense}"
            if values["OHM"] <= 19e3:
                comp = "ON" if mode == "WIRE2" else "OFF"
                command += f"; *WAI; RCOMP {comp}"
        elif "A" in values and "V" not in values and not options:
            command = out + "; *WAI; CUR_POST NORMAL"
        return self.validate_command(command)


MODEL_PROFILES = {
    "5560A": CalibratorProfile("5560A", ("LAN", "USB", "RS232", "GPIB"), 30),
    "5730A": Fluke5730AProfile("5730A", ("LAN", "USB", "RS232", "GPIB"), 2.2, 100e6, 1100),
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
        return self._build("build_voltage_cmd", max_voltage=self.profile.max_voltage, **kwargs)

    def build_current_cmd(self, **kwargs):
        return self._build("build_current_cmd", max_current=self.profile.max_current, **kwargs)

    def build_resistance_cmd(self, **kwargs):
        return self._build("build_resistance_cmd", **kwargs)

    def build_power_cmd(self, **kwargs):
        if not self.profile.supports_power:
            raise OutputRangeError(f"{self.profile.model} 본체는 전압·전류를 동시에 출력할 수 없어 전력·역률 행을 실행할 수 없습니다.")
        return self._build("build_power_cmd", **kwargs)
