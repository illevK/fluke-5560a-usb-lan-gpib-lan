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
from calibration.domain.commands import MODEL_PROFILES, RESISTORS_5080A, serial_baudrates
from calibration.domain.types import ConnectionInfo
from calibration.services.storage import data_dir
from calibration.instruments.discovery import _dut_discovery_usb_key, _dut_wired_source
from calibration.instruments.dut import _dut_gpib_key
from calibration.instruments.transport import AutoNetworkAligner, CalibratorRecoveryTarget, HAS_PYSERIAL, HAS_PYVISA, InstrumentCommandError, InstrumentProtocolError, InstrumentReplyError, InstrumentResponseTimeout, OperationCancelled, PYSERIAL_IMPORT_ERROR, PYVISA_IMPORT_ERROR, SerialEndpoint, SerialLineTransport, SharedVisaManager, TelnetLineTransport, _serial_key_for_address, _serial_usb_key, _wait_for_serial_address, close_lan_socket, dependency_import_message, pyvisa, serial


class Fluke5560AConnectionManager:
    def __init__(self):
        self.info = ConnectionInfo()
        self._visa_rm = None
        self._visa_inst = None
        self._lan_sock = None
        self._first_command_check = False
        self.zero_adjustment_due = False
        self._transport = None
        self._serial_port = None
        self._active_endpoint = None
        self.recovery_target = None
        self.recovery_pending = False
        self.output_unconfirmed = False
        self.expected_model = "5560A"
        self.profile = MODEL_PROFILES["5560A"]
        self._lock = threading.RLock()
        self._sim_oper_state = False
        self._sim_last_out = "0 V, 0 HZ"
        self.last_error = ""
        self.source_ip = ""
        self._last_lan_source = None
        self._connection_history = self._load_connection_history()
        self._lan_tcp_failures = []
        self.last_diagnostic = ""
        self.operation_timeout = 60.0
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self._log_lock = threading.Lock()
        application_path = sys.executable if getattr(sys, "frozen", False) else __file__
        self._log_filename = f"calibration_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}_{os.getpid()}.log"
        self._default_log_folder = str(data_dir() / "logs")
        self.log_path = os.path.join(self._default_log_folder, self._log_filename)
        self.log_folder_warning = ""
        self._load_log_folder()

    @staticmethod
    def _log_settings_path():
        return data_dir() / "log-settings.json"

    @staticmethod
    def _connection_history_path():
        return Fluke5560AConnectionManager._log_settings_path().with_name("connections.json")

    @property
    def active_endpoint(self):
        return self._active_endpoint

    def previous_routes(self):
        return self._connection_history.copy()

    def _load_connection_history(self):
        try:
            history = json.loads(self._connection_history_path().read_text(encoding="utf-8"))
            return history if isinstance(history, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember_connection(self, interface, *, history_key=None, **details):
        model = history_key or self.profile.model
        settings = self._connection_history_path()
        history = self._load_connection_history()
        previous = history.get(model)
        previous = previous if isinstance(previous, dict) else {}
        updated = {**previous, **details, "interface": interface}
        self._connection_history[model] = updated
        if updated == previous:
            return
        history[model] = updated
        try:
            settings.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=settings.parent) as folder:
                staged = Path(folder) / settings.name
                staged.write_text(json.dumps(history), encoding="utf-8")
                staged.replace(settings)
        except OSError as exc:
            self.record_event(f"[연결 기록 저장 실패] {exc}", notify=True)

    def _load_log_folder(self):
        try:
            with self._log_settings_path().open(encoding="utf-8") as settings_file:
                settings = json.load(settings_file)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.log_folder_warning = "로그 폴더 설정을 읽지 못해 기본 폴더에 저장합니다."
            return
        folder = settings.get("log_folder") if isinstance(settings, dict) else None
        if not isinstance(folder, str) or not os.path.isabs(folder):
            self.log_folder_warning = "저장된 로그 폴더 경로가 올바르지 않아 기본 폴더에 저장합니다."
            return
        target = Path(folder) / self._log_filename
        try:
            # Do not recreate a missing custom folder, which may be on a disconnected drive.
            with target.open("a", encoding="utf-8"):
                pass
        except (OSError, ValueError):
            self.log_folder_warning = f"저장된 로그 폴더를 사용할 수 없어 기본 폴더에 저장합니다. ({folder})"
            return
        self.log_path = str(target)

    def set_log_folder(self, folder):
        target_folder = Path(folder)
        if not target_folder.is_absolute():
            raise ValueError("로그를 저장할 폴더의 전체 경로를 선택하세요.")
        settings = self._log_settings_path()
        # Verify write access before saving the preference or changing the active log.
        with tempfile.TemporaryFile(dir=target_folder):
            pass
        settings.parent.mkdir(parents=True, exist_ok=True)
        with self._log_lock:
            with tempfile.TemporaryDirectory(dir=settings.parent, prefix="log-settings-") as staging_dir:
                staged = Path(staging_dir) / settings.name
                staged.write_text(json.dumps({"log_folder": str(target_folder)}, ensure_ascii=False), encoding="utf-8")
                staged.replace(settings)
            self.log_path = str(target_folder / self._log_filename)
            self.log_folder_warning = ""

    def record_event(self, message, notify=False):
        with self._log_lock:
            try:
                folder = os.path.dirname(self.log_path)
                if folder == self._default_log_folder:
                    os.makedirs(folder, exist_ok=True)
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
                and fields[1].upper() in MODEL_PROFILES
                and self.expected_model in ("AUTO", fields[1].upper()))

    def _identify(self, value, interface, expected_identity=""):
        if expected_identity and value != expected_identity:
            raise InstrumentProtocolError("재연결한 기준기의 식별 정보가 기존 장비와 다릅니다.")
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
            query = f"COMM_MODE? {port}"
            skip_interruption = interface == "RS232"
            mode = transport.query(query, skip_terminal_interruption=skip_interruption).upper()
            if mode == "TERM":
                transport.write(f"COMM_MODE {port}, COMP")
                mode = transport.query(query, skip_terminal_interruption=skip_interruption).upper()
            if mode != "COMP":
                raise InstrumentProtocolError(f"Computer 모드 전환을 확인하지 못했습니다. 장비 응답: {mode[:120]!r}")
        transport.write("REMOTE")
        if self.profile.model == "5730A":
            port = {"LAN": "ENET", "USB": "USB", "RS232": "SERIAL"}.get(interface)
            if port:
                transport.write(f"REM_MODE {port}, COMP")
        if transport.query("*OPC?").strip() != "1":
            raise InstrumentProtocolError("REMOTE 처리 완료 응답이 올바르지 않습니다.")
        if self.profile.model == "5730A" and not self._status_5730a(transport.query("ISR?")) & (1 << 11):
            raise InstrumentProtocolError("5730A의 Active Remote Port와 Computer 설정을 확인하세요.")

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
        if previous and previous[:2] == (ip, port):
            host = previous[2]
        else:
            saved = self._connection_history.get(self.expected_model)
            host = saved.get("source_ip") if isinstance(saved, dict) and (saved.get("ip"), saved.get("port")) == (ip, port) else None
        if host and host in candidates:
            candidates.remove(host)
            candidates.insert(0, host)
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
                              baudrate=9600, data_bits=8, parity="N", stop_bits=1, flow_control="XON/XOFF",
                              excluded_com="", excluded_gpib=None, excluded_ip="", excluded_usb=None,
                              lan_unavailable=""):
        # Legacy fallback argument is intentionally ignored: IDN verification is mandatory.
        mode = preferred_mode.strip().upper()
        if mode not in ("AUTO", "USB", "GPIB", "RS232", "LAN", "LAN (SOCKET)", "LAN (VISA)", "SIMULATION"):
            raise ValueError(f"지원하지 않는 연결 모드: {mode}")
        model = model.strip().upper()
        automatic_model = model == "AUTO" and mode != "SIMULATION"
        if model not in MODEL_PROFILES and not automatic_model:
            raise ValueError(f"지원하지 않는 모델: {model}")
        profile = MODEL_PROFILES.get(model, MODEL_PROFILES["5560A"])
        interface = "LAN" if mode.startswith("LAN") else mode
        if mode == "USB" and profile.usb_virtual_com and visa_resource:
            raise ValueError("5730A 후면 USB는 가상 COM을 사용합니다. 주소를 비워 검색하거나 COM 번호를 입력하세요.")
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
        errors = [f"[유선 LAN 경로] {lan_unavailable}"] if lan_unavailable else []
        saved = self._connection_history.get(model)
        saved = saved if isinstance(saved, dict) else {}
        histories = [saved]
        if automatic_model:
            histories = [self._connection_history[key] for key in reversed(self._connection_history)
                         if key in MODEL_PROFILES and isinstance(self._connection_history[key], dict)]
        lan_candidates = []
        if not lan_unavailable and "LAN" in profile.interfaces and (mode == "AUTO" or mode.startswith("LAN")):
            inputs = [(lan_ip, lan_port)]
            if mode == "AUTO" or not lan_ip.strip():
                inputs.extend((item.get("ip"), item.get("port")) for item in histories)
            if automatic_model and not lan_ip.strip() and _dut_wired_source(
                    "169.254.1.1", AutoNetworkAligner.list_ethernet_interfaces()):
                inputs.append(("169.254.1.1", 3490))
            for candidate_ip, candidate_port in inputs:
                if not isinstance(candidate_ip, str) or not candidate_ip.strip():
                    continue
                try:
                    address = AutoNetworkAligner.normalize_ipv4(candidate_ip), self._validate_port(candidate_port)
                except (TypeError, ValueError) as exc:
                    if mode != "AUTO":
                        raise
                    errors.append(f"[LAN 설정] {exc}")
                    continue
                if address[0] != excluded_ip and address not in lan_candidates:
                    lan_candidates.append(address)
            if mode.startswith("LAN") and not lan_candidates:
                raise ValueError(f"{model} 본체 Ethernet 화면의 현재 IP 주소를 입력하세요.")

        serial_first = mode == "AUTO" and (
            bool(serial_port.strip()) or any(item.get("interface") in ("RS232", "USB-COM") for item in histories)
        )
        if automatic_model and lan_ip.strip():
            serial_first = False
        order = ("RS232", "LAN") if serial_first else ("LAN", "RS232")
        if automatic_model and not serial_first:
            order = ("LAN", "VISA", "RS232")
        else:
            order += ("VISA",)
        for interface in order:
            if self.cancel_event.is_set():
                raise OperationCancelled("장비 검색을 중지했습니다.")
            if interface == "LAN":
                for ip, port in lan_candidates:
                    self.record_event(f"[LAN 연결 시도] {ip}:{port}", notify=True)
                    try:
                        source_ips = self._lan_source_ips(ip, port, source_ip, adapter_idx)
                    except (ConnectionError, ValueError) as exc:
                        if mode != "AUTO":
                            raise
                        errors.append(f"[LAN 경로] {ip}:{port} · {exc}")
                        continue
                    result = self._try_lan_socket(ip, port, timeout_sec, eol=eol, source_ips=source_ips)
                    if result.connected:
                        self.expected_model = self.profile.model
                        self._remember_connection("LAN", ip=ip, port=port, source_ip=self.source_ip)
                        return result
                    errors.append(self.last_error)
                    if mode != "AUTO":
                        raise ConnectionError(self.build_lan_diagnostic_report(ip, port))
            elif interface == "VISA":
                if mode in ("AUTO", "USB", "GPIB") and set(profile.interfaces) & {"USB", "GPIB"}:
                    if mode == "USB" and (profile.usb_virtual_com or serial_port.strip()):
                        continue
                    result = self._try_visa_auto_scan(mode, gpib_addr, timeout_sec, visa_resource,
                                                      excluded_gpib=excluded_gpib, excluded_usb=excluded_usb)
                    if result.connected:
                        self.expected_model = self.profile.model
                        return result
                    errors.append(self.last_error)
            elif (mode in ("AUTO", "RS232") and "RS232" in profile.interfaces
                  or mode == "USB" and (profile.usb_virtual_com or automatic_model) and not visa_resource):
                if serial_port.strip() and mode in ("RS232", "USB"):
                    candidates = [serial_port.strip()]
                else:
                    recent_serial = next((item for item in histories if item.get("com")), {})
                    candidates = self._serial_candidates(serial_port, recent_serial.get("com"), recent_serial.get("serial_usb_key", ""))
                candidates = [port for port in candidates if port.upper() != excluded_com]
                if not candidates:
                    errors.append("검색할 COM 포트가 없습니다. USB–RS-232 컨버터를 확인하세요." if HAS_PYSERIAL else
                                  dependency_import_message("pyserial", "pyserial", PYSERIAL_IMPORT_ERROR))
                configured = (baudrate, data_bits, parity, stop_bits, flow_control, eol)
                presets = [configured]
                search_framing = automatic_model and mode == "AUTO"
                if search_framing:
                    if eol == "CRLF":
                        presets.insert(0, (*configured[:-1], "LF"))
                    presets.extend((rate, 8, "N", 1, flow, ending) for rate, flow, ending in
                                   ((9600, "XON/XOFF", "CRLF"), (9600, "NONE", "LF"), (9600, "RTS/CTS", "CRLF"),
                                    (19200, "NONE", "LF"), (38400, "NONE", "LF"), (115200, "NONE", "LF")))
                    for item in reversed(histories):
                        recent = item.get("serial_settings")
                        if isinstance(recent, (tuple, list)) and len(recent) == 6 and all(isinstance(v, (str, int)) for v in recent):
                            preset = tuple(recent)
                            if item is self._connection_history.get("5560A") and preset[-1] == "CRLF":
                                preset = (*preset[:-1], "LF")
                            presets.insert(0, preset)
                probe_timeout = min(timeout_sec, 1.0) if search_framing else timeout_sec
                serial_deadline = time.monotonic() + 12
                for speed, bits, check, stop, flow, ending in dict.fromkeys(presets):
                    for port in candidates:
                        if self.cancel_event.is_set():
                            raise OperationCancelled("장비 검색을 중지했습니다.")
                        if search_framing and time.monotonic() >= serial_deadline:
                            break
                        self.record_event(f"[RS232 연결 시도] {port} / {speed} / {bits}{check}{stop} / {flow} / {ending}", notify=True)
                        result = self._try_serial(port, probe_timeout, ending, speed, bits, check, stop, flow,
                                                  stream_mode=mode)
                        if result.connected:
                            self.expected_model = self.profile.model
                            ending = "LF" if self.profile.model == "5560A" and ending == "CRLF" else ending
                            saved_interface = self._active_endpoint.interface if self._active_endpoint else "RS232"
                            self._remember_connection(saved_interface, com=port, serial_settings=[speed, bits, check, stop, flow, ending],
                                                      serial_usb_key=self._active_endpoint.usb_serial_key if self._active_endpoint else "")
                            if self._transport is not None:
                                self._transport.timeout = timeout_sec
                            if self._active_endpoint is not None:
                                self._active_endpoint = replace(self._active_endpoint, timeout=timeout_sec)
                            return result
                        errors.append(self.last_error)
        self.last_error = "\n".join(errors)
        raise ConnectionError(self.last_error or "연결할 인터페이스와 장비 IP/리소스를 지정하세요.")

    def _try_lan_socket(self, ip, port=3490, timeout_sec=3.0, allow_open_port_fallback=False,
                        source_ip="", eol="CRLF", *, source_ips=None, expected_identity=""):
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
            self._identify(idn, "LAN", expected_identity)
            # Identification is complete before sending any state-changing commands.
            stage = "Computer 통신 모드 확인"
            self._prepare_remote_stream(transport, "LAN")
            self._lan_sock, self._transport = sock, transport
            self._first_command_check = True
            self.info = ConnectionInfo(True, "LAN (Ethernet Socket)", f"TCPIP::{ip}::{port}::SOCKET", idn, ip, port)
            self._active_endpoint = CalibratorRecoveryTarget("LAN", ip, idn, self.profile.model,
                                                              timeout_sec, eol, port, self.source_ip)
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

    @staticmethod
    def _serial_candidates(explicit, previous, usb_key=""):
        if not HAS_PYSERIAL:
            return [explicit.strip().upper()] if explicit.strip() else []
        ports = [port for port in serial.tools.list_ports.comports()
                 if not (port.hwid or "").upper().startswith(("BTHENUM", "BTHLE", "BTHMODEM"))
                 and "bluetooth" not in (port.description or "").casefold()]
        ports.sort(key=lambda port: (port.device.upper() != explicit.strip().upper(),
                                     not usb_key or _serial_usb_key(port) != usb_key,
                                     port.device.upper() != previous, port.vid is None, port.device.upper()))
        return list(dict.fromkeys(port.device.upper() for port in ports))

    def _try_serial(self, port, timeout, eol, baudrate, data_bits, parity, stop_bits, flow_control, *,
                    expected_identity="", stream_mode="RS232"):
        if not HAS_PYSERIAL:
            self.last_error = dependency_import_message("pyserial", "pyserial", PYSERIAL_IMPORT_ERROR)
            return ConnectionInfo()
        handle = None
        stage = "COM 포트 열기"
        # The 5560A can interpret CRLF as two input messages.
        input_eol = "LF" if self.expected_model == "5560A" and eol == "CRLF" else eol
        try:
            if not port.strip():
                raise ValueError("장치 관리자 또는 COM 목록에서 컨버터의 COM 번호를 선택하세요.")
            if int(baudrate) not in serial_baudrates(self.expected_model) or int(data_bits) not in (7, 8) or parity not in ("N", "E", "O") or float(stop_bits) not in (1, 2):
                raise ValueError(f"{self.profile.model}의 속도/데이터/패리티/정지 비트 설정을 확인하세요.")
            if flow_control not in ("NONE", "XON/XOFF", "RTS/CTS"):
                raise ValueError("흐름 제어는 NONE, XON/XOFF, RTS/CTS 중 선택하세요.")
            handle = serial.Serial(port=port.strip(), baudrate=int(baudrate), bytesize=int(data_bits),
                                   parity=parity, stopbits=float(stop_bits), timeout=.2, write_timeout=timeout,
                                   xonxoff=flow_control == "XON/XOFF", rtscts=flow_control == "RTS/CTS", dsrdtr=False)
            transport = SerialLineTransport(SerialEndpoint(handle), timeout, input_eol)
            # Allow the 5560A to finish its RS232 response before sending another
            # command. Bench checks at 9600 baud produced -410 without this gap.
            if self.expected_model in ("5560A", "AUTO"):
                transport.reply_gap = .12
            stage = "장비 식별 (*IDN?)"
            idn = transport.query("*IDN?", identity=True)
            interface = "USB" if stream_mode == "USB" else "RS232"
            self._identify(idn, interface, expected_identity)
            if interface == "USB" and not self.profile.usb_virtual_com:
                raise InstrumentProtocolError(f"{self.profile.model}의 USB는 가상 COM 방식이 아닙니다.")
            if stream_mode == "AUTO" and self.profile.usb_virtual_com:
                device = next((p for p in serial.tools.list_ports.comports()
                               if p.device.upper() == port.upper()), None)
                description = " ".join(str(getattr(device, field, "") or "")
                                       for field in ("manufacturer", "product", "description")).upper()
                interface = "USB" if "5730" in description or "FLUKE" in description else "COM"
            if self.profile.model == "5560A":
                if input_eol == "CRLF":
                    transport.terminator = b"\n"
                    input_eol = "LF"
            else:
                transport.reply_gap = 0.0
            stage = "원격 제어 준비"
            self._prepare_remote_stream(transport, interface)
            self._serial_port, self._transport = handle, transport
            self._first_command_check = True
            description = {"USB": "USB (가상 COM)", "COM": "COM (USB / RS232)", "RS232": "RS232 (COM / USB–RS-232)"}
            self.info = ConnectionInfo(True, description[interface],
                                       f"{port} / {baudrate} / {data_bits}{parity}{stop_bits} / {flow_control}", idn)
            self._active_endpoint = CalibratorRecoveryTarget(
                "USB-COM" if interface == "USB" else interface, port, idn, self.profile.model, timeout,
                input_eol,
                serial_settings=(baudrate, data_bits, parity, stop_bits, flow_control),
                usb_serial_key=_serial_key_for_address(port))
            self.last_error = ""
            return self.info
        except Exception as exc:
            if handle is not None:
                handle.close()
            self._serial_port = self._transport = None
            self.info = ConnectionInfo()
            if stage == "원격 제어 준비":
                hint = ("장비 식별은 성공했습니다. 본체 RS-232 메뉴의 Remote Interface를 Computer로 설정한 뒤 다시 연결하세요."
                        if self.profile.model == "5560A" else "장비 식별은 성공했습니다. 원격 제어 모드와 본체 화면을 확인하세요.")
            else:
                hint = ("USB–RS-232 컨버터의 COM 번호와 본체의 속도, 데이터 비트, 패리티, 정지 비트, 흐름 제어, EOL을 확인하세요.")
                if self.expected_model in ("5520A", "5522A"):
                    hint += " 장비의 SERIAL 1 FROM HOST에 null-modem 결선으로 연결하세요."
            if self.expected_model == "5730A" or stream_mode == "USB":
                hint += " 5730A는 본체 Active Remote Port를 연결한 USB 또는 RS-232로 선택하고 Computer 모드를 확인하세요."
            framing = f"{baudrate} baud · {data_bits}{parity}{stop_bits} · {flow_control} · PC 송신 {input_eol}"
            self.last_error = f"[{stage}] {port} · {framing}\n{type(exc).__name__}: {exc}\n{hint}"
            return self.info

    def _try_visa_auto_scan(self, filter_mode, gpib_addr, timeout_sec, visa_resource="",
                            *, excluded_gpib=None, excluded_usb=None, expected_identity=""):
        if not HAS_PYVISA:
            self.last_error = dependency_import_message("pyvisa", "PyVISA", PYVISA_IMPORT_ERROR)
            return ConnectionInfo()
        errors = []
        try:
            self._visa_rm = SharedVisaManager()
            if visa_resource.strip():
                resources = [visa_resource.strip()]
            elif filter_mode == "GPIB" and gpib_addr is not None:
                if not 0 <= int(gpib_addr) <= 30:
                    raise ValueError("GPIB 주소는 0~30 사이여야 합니다.")
                # Honor the requested address, not whichever device answers first.
                resources = [f"GPIB0::{int(gpib_addr)}::INSTR"]
            else:
                resources = list(self._visa_rm.list_resources())
            for resource in resources:
                kind = resource.upper().split("::", 1)[0]
                if not kind.startswith(("USB", "GPIB")):
                    continue
                interface = "USB" if kind.startswith("USB") else "GPIB"
                if self.expected_model != "AUTO" and (interface not in self.profile.interfaces
                        or interface == "USB" and self.profile.usb_virtual_com):
                    continue
                if (excluded_usb is not None and _dut_discovery_usb_key(resource) == excluded_usb
                        or excluded_gpib is not None and _dut_gpib_key(resource) == excluded_gpib):
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
                    self._identify(idn, interface, expected_identity)
                    if interface == "USB" and self.profile.usb_virtual_com:
                        raise InstrumentProtocolError("5730A USB 장치는 Windows COM 포트로 연결하세요.")
                    if interface == "GPIB":
                        inst.control_ren(pyvisa.constants.RENLineOperation.asrt_address)
                        if self.profile.model == "5730A" and not self._status_5730a(inst.query("ISR?")) & (1 << 11):
                            raise InstrumentProtocolError("5730A Active Remote Port에서 GPIB를 선택하세요.")
                    self._visa_inst = inst
                    self._first_command_check = True
                    # REMOTE/LOCAL strings are Ethernet/serial commands, not GPIB/USBTMC.
                    self.info = ConnectionInfo(True, "USB (USBTMC)" if kind.startswith("USB") else "GPIB (IEEE-488.2)", resource, idn)
                    self._active_endpoint = CalibratorRecoveryTarget(interface, resource, idn, self.profile.model, timeout_sec)
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
        if self.expected_model == "5730A":
            return (f"5730A {target_ip}:{target_port}\n{self.last_error}\n"
                    "본체 Setup Menu → Instrument Setup → Remote Port에서 Active Remote Port를 Ethernet으로 "
                    "선택하고 IP, 서브넷, 포트(기본 3490)를 확인하세요. 다른 LAN 세션이 있으면 먼저 해제하세요.")
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
        if self.expected_model != "AUTO" and "LAN" not in MODEL_PROFILES[self.expected_model].interfaces:
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
            if not self._validate_idn(idn) or "LAN" not in MODEL_PROFILES[idn.split(",")[1].strip().upper()].interfaces:
                raise InstrumentProtocolError(f"다른 장비 응답: {idn}")
            self._last_lan_source = (ip, port, self.source_ip)
            self.last_error = f"[진단 성공] TCP 및 장비 식별 확인: {idn}"
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
        self._first_command_check = False
        self.zero_adjustment_due = False
        self._serial_port = None
        self.info = ConnectionInfo()
        self._active_endpoint = None

    @staticmethod
    def _status_5730a(reply):
        if not re.fullmatch(r"\+?\d{1,5}", reply.strip()) or int(reply) > 65535:
            raise InstrumentReplyError(f"5730A 상태 레지스터 응답을 확인할 수 없습니다: {reply[:80]}")
        return int(reply)

    def _query(self, command, timeout=None, cancellable=True):
        """Caller holds the transaction lock and owns failure cleanup."""
        if self.profile.model == "5730A":
            if command.upper() == "OPER?":
                status = self._query("ISR?", timeout, cancellable)
                return str(self._status_5730a(status) & 1)
            if command.upper() == "ERR?":
                reply = self._query("FAULT?", timeout, cancellable)
                if not re.fullmatch(r"[+-]?\d{1,6}", reply):
                    raise InstrumentReplyError(f"5730A 오류 번호를 확인할 수 없습니다: {reply[:80]}")
                code = int(reply)
                if not code:
                    return '0,"No error"'
                description = self._query(f"EXPLAIN? {code}", timeout, cancellable)
                return f"{code},{description}"
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
        if command.upper() == "ERR?":
            if not re.match(r"^(?:[+-]?\d+\s*,|\+?0$)", response):
                raise InstrumentReplyError(f"장비 오류 응답 형식이 올바르지 않습니다: {response[:120]}")
            if self.profile.model == "5560A" and re.match(r"^\+?8012\s*,", response):
                self.zero_adjustment_due = True
                self.record_event(f"[영점 조정 필요] {response} · 5560A 본체에서 Zero Adjustment를 실행하세요.", notify=True)
        return response

    def _write(self, command):
        self.record_event(f"TX COMMAND >> {command}")
        if self._visa_inst is not None:
            self._visa_inst.write(command)
        elif self._transport is not None:
            self._transport.write(command)
        else:
            raise ConnectionError("유효한 통신 핸들이 없습니다.")

    def _confirm_standby_after_interrupted_wait(self):
        # An earlier OPC/ERR reply can still be in flight. Only trust new
        # status queries after seeing this session's exact identification.
        deadline = time.monotonic() + 3.0
        self.record_event("TX QUERY >> *IDN? | STBY 복구")
        self._transport.query("*IDN?", timeout=3.0, expected_reply=self.info.idn_string)
        self.record_event(f"RX << {self.info.idn_string} | STBY 복구")
        if self._query("OPER?", timeout=max(.001, deadline - time.monotonic()), cancellable=False) != "0":
            raise InstrumentProtocolError("본체의 STBY 상태를 확인하지 못했습니다.")
        return self._query("ERR?", timeout=max(.001, deadline - time.monotonic()), cancellable=False)

    def _can_recover_standby(self, exc):
        return (self._transport is not None
                and (isinstance(exc, (OperationCancelled, InstrumentResponseTimeout))
                     or (self.profile.model != "5560A" and isinstance(exc, InstrumentReplyError))))

    def _fail_control_session(self, context, exc, *, recover_standby=False):
        message = f"{context}: {type(exc).__name__}: {exc}"
        target = self._active_endpoint
        self.output_unconfirmed = True
        self.record_event("[출력 상태 미확인] 기준기 응답이 끊겼습니다. 본체에서 STBY를 확인하세요.", notify=True)
        try:
            self._write("STBY")
        except Exception as standby_error:
            message += f"\nSTBY 전송 실패: {standby_error}. 본체의 STBY 상태를 확인하세요."
        else:
            if recover_standby:
                try:
                    error = self._confirm_standby_after_interrupted_wait()
                except Exception as recovery_error:
                    message += f"\nSTBY 확인 실패: {recovery_error}"
                else:
                    self.cancel_event.clear()
                    self.output_unconfirmed = False
                    message = f"{message}\n{self.profile.model} 응답 {error}. STBY 확인 완료, 연결 유지. 다음 행은 실행하지 않습니다."
                    self.last_error = message
                    self.record_event(message, notify=True)
                    raise InstrumentCommandError(message) from exc
            message += "\nSTBY 전송 시도 후 연결 종료. 출력 차단 확인은 되지 않았으므로 본체의 STBY 상태를 확인하세요."
        self._drop_connection()
        self.recovery_target = target
        self.recovery_pending = target is not None
        self.last_error = message
        self.record_event(message, notify=True)

    def check_output_state(self):
        with self._lock:
            state = self.query("OPER?").strip()
            if state not in ("0", "1"):
                error = InstrumentProtocolError(f"출력 상태 응답이 올바르지 않습니다: {state}")
                self._fail_control_session("OPER? 응답 확인", error)
                raise error
            return state

    def set_uncompensated_resistance_in_standby(self, command):
        expected = self.profile.output_values(command.partition(";")[0])["OHM"]
        self.send_command(command, check_before=True)
        reply = self.query("OUT?")
        fields = [field.strip().upper() for field in reply.split(",")]
        if self.profile.model == "5730A":
            try:
                # Fixed resistors can report their characterized rather than nominal value.
                confirmed = (len(fields) == 3 and fields[1] == "OHM" and float(fields[2]) == 0
                             and math.isclose(float(fields[0]), expected, rel_tol=.01, abs_tol=.05 if expected == 0 else 0))
            except ValueError:
                confirmed = False
            if not confirmed:
                raise InstrumentReplyError(f"5730A {expected:g} Ω 설정 확인 실패 · 응답 {reply[:120]}")
            if self._status_5730a(self.query("ISR?")) & 0x14:  # RCOMP or EXSENS
                raise InstrumentReplyError("5730A 2선식 저항의 내부 센스와 보상 OFF를 확인하지 못했습니다.")
            return
        confirmed = False
        if len(fields) == 5 and (fields[1], fields[3]) == ("OHM", "0"):
            try:
                actual = float(fields[0])
                if math.isfinite(actual) and float(fields[2]) == float(fields[4]) == 0:
                    if self.profile.model == "5080A":
                        # OUT? can report the characterized value of the selected fixed resistor.
                        selected = min(RESISTORS_5080A, key=lambda nominal: abs(nominal - abs(actual)))
                        confirmed = selected == expected and (actual >= 0 or expected == 0)
                    else:
                        confirmed = math.isclose(actual, expected, rel_tol=5e-6)
            except ValueError:
                pass
        if not confirmed:
            raise InstrumentReplyError(f"기준기 {expected:g} Ω 설정 확인 실패 · 응답 {reply[:120]}")
        compensation = self.query("ZCOMP?").strip().upper()
        if compensation not in ("NONE", "OFF"):
            raise InstrumentReplyError(f"기준기 저항 보상 OFF 확인 실패 · 응답 {compensation[:80]}")

    def recover_standby(self):
        with self._lock:
            saved = self.recovery_target
            self.recovery_pending = False
            if saved is None or self.info.connected:
                raise ConnectionError("복구할 기준기 연결이 없습니다.")
            self.expected_model = saved.model
            self.profile = MODEL_PROFILES[saved.model]
            timeout = min(2.0, saved.timeout)
            try:
                if saved.interface in ("RS232", "USB-COM", "COM"):
                    port = _wait_for_serial_address(saved.address, saved.usb_serial_key, self.cancel_event)
                    connection = self._try_serial(port, timeout, saved.eol, *saved.serial_settings,
                                                  expected_identity=saved.identity,
                                                  stream_mode={"USB-COM": "USB", "COM": "AUTO"}.get(saved.interface, "RS232"))
                elif saved.interface == "LAN":
                    connection = self._try_lan_socket(saved.address, saved.port, timeout, eol=saved.eol,
                                                      source_ips=(saved.source_ip,), expected_identity=saved.identity)
                else:
                    connection = self._try_visa_auto_scan(saved.interface, 0, timeout, saved.address,
                                                          expected_identity=saved.identity)
                if not connection.connected:
                    raise ConnectionError(self.last_error)
                self._write("STBY")
                if self._query("*OPC?", timeout=8.0, cancellable=False) != "1" or self._query(
                        "OPER?", timeout=2.0, cancellable=False) != "0":
                    raise InstrumentProtocolError("복구한 기준기의 STBY 응답을 확인하지 못했습니다.")
                if self._transport is not None:
                    self._transport.timeout = saved.timeout
                elif self._visa_inst is not None:
                    self._visa_inst.timeout = int(saved.timeout * 1000)
                self._active_endpoint = replace(self._active_endpoint, timeout=saved.timeout)
            except Exception:
                self._drop_connection()
                raise
            self.recovery_target = None
            self.output_unconfirmed = False
            self.record_event(f"[기준기 복구] {connection.resource_name} · 동일 장비 · STBY 확인", notify=True)
            return connection

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
                recover_standby = (command.upper() in ("OPER?", "*OPC?", "CUR_POST?", "ERR?")
                                   and self._can_recover_standby(exc))
                self._fail_control_session(f"조회 {command}", exc, recover_standby=recover_standby)
                raise

    def _is_protection_error(self, error):
        if self.profile.model == "5730A":
            return False  # Confirm STBY after every 5730A fault.
        codes = {4008, 4100, 4102, 4202} if self.profile.model == "5560A" else {1500, 1501, 1502, 1503, 1504}
        return int(error.partition(",")[0]) in codes

    def _stop_after_command_error(self, error, phase):
        # A valid error reply means the stream is still synchronized.
        if not self._is_protection_error(error) or self._query("OPER?", cancellable=False) != "0":
            self._write("STBY")
            if self._query("*OPC?", timeout=self.operation_timeout) != "1" or self._query("OPER?") != "0":
                raise InstrumentProtocolError(f"{self.profile.model} 오류 {error}; STBY 확인 실패")
        hint = (" COM 포트의 속도·패리티·흐름 제어와 케이블을 확인하세요."
                if self._serial_port is not None and int(error.partition(",")[0]) in (-361, 801) else "")
        raise InstrumentCommandError(
            f"{self.profile.model} 오류 ({phase}): {error}. STBY 확인 완료, 연결 유지. 다음 행은 실행하지 않습니다."
            + hint
        )

    def _is_552x_serial_framing(self, code):
        return (code == 801 and self._serial_port is not None
                and self.profile.model in ("5520A", "5522A"))

    def _confirm_serial_standby_after_queue(self, error):
        if int(error.partition(",")[0]) == 0:
            if self._query("*IDN?") != self.info.idn_string or self._query("OPER?") != "0":
                raise InstrumentProtocolError("RS232 재확인 중 장비 식별 또는 STBY 상태가 일치하지 않습니다.")
            error = self._query("ERR?")
            if int(error.partition(",")[0]) == 0:
                self.record_event("[RS232 복구 확인] 동일 장비 · STBY · 오류 없음", notify=True)
        return error

    def _check_initial_error(self, error):
        if self.profile.model == "5730A":
            return error
        allowed = {-410, 8012} if self.profile.model == "5560A" else {1308}
        serial_5560 = self.profile.model == "5560A" and self._serial_port is not None
        if serial_5560:
            allowed.add(-361)
        if self._is_552x_serial_framing(801):
            allowed.add(801)
        serial_recovery = False
        # Inspect the initial backlog up to the documented queue capacity. Stop
        # on any other fault, including overflow, or if the output is active.
        for _ in range(15):
            code = int(error.partition(",")[0])
            if code not in allowed or self._query("OPER?") != "0":
                break
            if (serial_5560 and code in (-410, -361)) or self._is_552x_serial_framing(code):
                serial_recovery = True
            else:
                allowed.remove(code)
            self.record_event(f"[첫 명령 확인] STBY에서 장비 오류 목록을 다시 확인합니다: {error}")
            error = self._query("ERR?")
        return self._confirm_serial_standby_after_queue(error) if serial_recovery else error

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
                first_output = self._first_command_check and any(
                    re.match(r"^(?:OUT\s|OPER$)", part.strip(), re.I) for part in command.split(";"))
                if check_before or (first_output and (self.profile.model != "5560A" or self._serial_port is not None)):
                    pending = self._query("ERR?")
                    if first_output:
                        pending = self._check_initial_error(pending)
                    if not re.match(r"^\+?0\s*(?:,|$)", pending):
                        self._stop_after_command_error(pending, "출력 전 대기 중이던 오류")
                if first_output:
                    self._first_command_check = False
                self._write(command)
                completion = self._query("*OPC?", timeout=self.operation_timeout)
                if completion != "1":
                    raise InstrumentReplyError(f"명령 처리 완료를 확인하지 못했습니다. 장비 응답: {completion[:120]}")
                error = self._query("ERR?")
                if self._first_command_check and command.upper() == "STBY":
                    self._first_command_check = False
                    error = self._check_initial_error(error)
                elif (command.upper() == "STBY"
                      and self._is_552x_serial_framing(int(error.partition(",")[0]))
                      and self._query("OPER?") == "0"):
                    self.record_event(f"[STBY 확인] RS232 오류 재확인 · {error}")
                    error = self._confirm_serial_standby_after_queue(self._query("ERR?"))
                if not re.match(r"^\+?0\s*(?:,|$)", error):
                    self._stop_after_command_error(error, command)
                self.record_event(f"[명령 완료] {command} | {time.monotonic() - started:.3f}s", notify=True)
            except InstrumentCommandError as exc:
                self.last_error = str(exc)
                self.record_event(str(exc), notify=True)
                raise
            except Exception as exc:
                context = f"명령 [{command}] / {time.monotonic() - started:.3f}초 경과 / 완료 대기 한도 {self.operation_timeout:g}초"
                self._fail_control_session(context, exc, recover_standby=self._can_recover_standby(exc))
                raise

    def _record_shutdown_error(self, phase):
        error = self.query("ERR?")
        self.record_event(f"[연결 해제 · {phase}] {error}",
                          notify=not re.match(r"^\+?0\s*(?:,|$)", error))
        if self.profile.model == "5560A" and self._is_protection_error(error):
            return f"{phase}: {error}"
        return None

    def _leave_5560a_power_in_standby(self):
        if self.query("FUNC?").upper() not in ("AC_POWER", "DC_POWER"):
            return
        # STBY was confirmed by disconnect(); keep output disabled while leaving dual output.
        self.send_command("OUT 0 V, 0 HZ")
        if self.query("OPER?") != "0":
            self.send_command("STBY")
            raise InstrumentProtocolError("전력 설정 해제 후 본체에서 STBY를 다시 확인하세요.")
        readback = self.query("OUT?")
        if not re.fullmatch(r"[+-]?0+(?:\.0+)?(?:E[+-]?\d+)?\s*,\s*V\s*,\s*"
                            r"[+-]?0+(?:\.0+)?(?:E[+-]?\d+)?\s*,\s*0\s*,\s*"
                            r"[+-]?0+(?:\.0+)?(?:E[+-]?\d+)?", readback, re.I):
            raise InstrumentCommandError(f"5560A 0 V DC 설정 확인 실패: {readback[:120]}. STBY 확인 완료.")
        self.record_event("[연결 해제] 전력 설정 해제 · 0 V DC / STBY 확인", notify=True)

    def disconnect(self, standby=True):
        with self._lock:
            try:
                self.cancel_event.clear()
                if self.info.connected and standby:
                    shutdown_issues = []
                    checked_shutdown = not self.info.interface_type.startswith("SIMULATION")
                    if not checked_shutdown or self.query("OPER?") != "0":
                        try:
                            self.send_command("STBY")
                        except InstrumentCommandError as exc:
                            # STBY was confirmed, so remote control can still be released.
                            if self.profile.model == "5560A":
                                shutdown_issues.append(str(exc))
                        if checked_shutdown and self.query("OPER?") != "0":
                            raise InstrumentProtocolError("연결 해제 전 STBY를 확인하지 못했습니다.")
                    if checked_shutdown:
                        self.record_event("[연결 해제] STBY 확인")
                        error = self._record_shutdown_error("LOCAL 전")
                        if error:
                            shutdown_issues.append(error)
                        if self.profile.model == "5560A":
                            try:
                                self._leave_5560a_power_in_standby()
                            except InstrumentCommandError as exc:
                                shutdown_issues.append(str(exc))
                                self.record_event(f"[연결 해제 · 전력 설정] {exc}", notify=True)
                    if self._transport is not None:
                        self._write("LOCAL")
                        if self._query("*OPC?", timeout=self.operation_timeout) != "1":
                            raise InstrumentProtocolError("LOCAL 완료 응답을 확인하지 못했습니다.")
                        if checked_shutdown:
                            if self.query("OPER?") != "0":
                                raise InstrumentProtocolError("LOCAL 이후 STBY를 확인하지 못했습니다.")
                            error = self._record_shutdown_error("LOCAL 후")
                            if error:
                                shutdown_issues.append(error)
                    elif self._visa_inst is not None:
                        try:
                            self._visa_inst.control_ren(pyvisa.constants.RENLineOperation.address_gtl)
                        except Exception:
                            pass
                    if shutdown_issues:
                        raise InstrumentProtocolError("연결은 해제했습니다. 장비 응답을 확인하세요.\n" + "\n".join(shutdown_issues))
            finally:
                self._drop_connection()
