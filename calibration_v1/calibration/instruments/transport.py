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
from calibration.domain.types import DEFAULT_LAN_PORT


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


class InstrumentReplyError(InstrumentProtocolError):
    """A complete line was received, but it was not the expected reply."""


class InstrumentResponseTimeout(TimeoutError):
    """The command was sent, but its complete response did not arrive."""


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

    PROMPT = re.compile(r"^(?:[0-9]+>\s*|(?:5[0-9]{3}A)?\s*[=!?>]>?\s*)", re.I)

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
            raise InstrumentReplyError(f"장비 오류 응답: {text}")
        text = cls.PROMPT.sub("", text).strip()
        return "" if text.upper() == command.upper() else text

    def query(self, command, identity=False, timeout=None, cancel_event=None, progress=None, *,
              expected_reply=None, skip_terminal_interruption=False):
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
                try:
                    text = self.clean_line(line, command)
                except InstrumentReplyError:
                    if expected_reply is None:
                        raise
                    continue
                if not text or text.upper() in self._recent_writes:
                    continue
                if re.match(r"^Fault\s+-?\d+\s*:", text, re.I):
                    if expected_reply is not None or (skip_terminal_interruption
                            and re.fullmatch(r"Fault\s+-410\s*:\s*488\.2 interrupted query", text, re.I)):
                        continue
                    raise InstrumentReplyError(f"장비 오류 응답: {text}")
                self.last_received = text
                # Initial banners/prompts are not identification responses.
                if identity and len(text.split(",")) < 4:
                    continue
                if expected_reply is not None and text != expected_reply:
                    continue
                return text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                partial = bytes(self.buffer[-120:]).decode("ascii", errors="replace")
                raise InstrumentResponseTimeout(f"{command}: {limit:g}초 동안 완전한 응답 없음 (부분 수신: {partial or '없음'}).")
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


@dataclass(frozen=True)
class CalibratorRecoveryTarget:
    interface: str
    address: str
    identity: str
    model: str
    timeout: float
    eol: str = "CRLF"
    port: int = DEFAULT_LAN_PORT
    source_ip: str = ""
    serial_settings: tuple = ()
    usb_serial_key: str = ""


PYSERIAL_IMPORT_ERROR = ""


try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError as exc:
    serial = None
    HAS_PYSERIAL = False
    PYSERIAL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _serial_usb_key(port):
    serial_number = getattr(port, "serial_number", None)
    vid, pid = getattr(port, "vid", None), getattr(port, "pid", None)
    if vid is None or pid is None:
        return ""
    location = getattr(port, "location", None)
    if serial_number:
        return f"{vid:04x}:{pid:04x}:serial:{serial_number}".casefold()
    return f"{vid:04x}:{pid:04x}:location:{location}".casefold() if location else ""


def _serial_key_for_address(address):
    if HAS_PYSERIAL:
        try:
            for port in serial.tools.list_ports.comports():
                if port.device.upper() == address.upper():
                    return _serial_usb_key(port)
        except OSError:
            pass
    return ""


def _wait_for_serial_address(address, usb_key, cancel, wait_seconds=3.0):
    deadline = time.monotonic() + wait_seconds
    while HAS_PYSERIAL and not cancel.is_set():
        ports = serial.tools.list_ports.comports()
        matches = [port.device.upper() for port in ports if
                   (_serial_usb_key(port) == usb_key if usb_key else port.device.upper() == address.upper())]
        if len(matches) == 1:
            return matches[0]
        if time.monotonic() >= deadline:
            break
        cancel.wait(min(.25, max(0, deadline - time.monotonic())))
    if cancel.is_set():
        raise InterruptedError("COM 복구가 중지되었습니다.")
    raise ConnectionError(f"{address} USB–RS232 변환기가 Windows에 다시 표시되지 않았습니다.")


class SerialEndpoint:
    """Adapt pyserial to the cancellable line reader; an empty read is a timeout."""
    def __init__(self, port):
        self.port = port

    def settimeout(self, _timeout):
        # The line reader handles the overall deadline. The fixed port timeouts
        # are configured at open so a read never reconfigures the Windows driver.
        pass

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
    reply_gap = 0.0

    def query(self, *args, **kwargs):
        reply = super().query(*args, **kwargs)
        if self.reply_gap:
            cancel = kwargs.get("cancel_event")
            if cancel is None:
                time.sleep(self.reply_gap)
            elif cancel.wait(self.reply_gap):
                raise OperationCancelled("사용자가 현재 응답 대기를 중단했습니다.")
        return reply

    def _feed(self, raw):
        # RS-232 is not Telnet. Never send IAC negotiation bytes to a serial port.
        self.buffer.extend(raw)
        if len(self.buffer) > 65536:
            raise InstrumentProtocolError("RS-232 응답이 64 KiB를 초과했습니다.")
