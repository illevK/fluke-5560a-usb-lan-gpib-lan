"""Connection orchestration. All methods run on the same I/O worker as measurements."""
from __future__ import annotations
from dataclasses import asdict, dataclass, replace
import re
import threading

from .drivers import FlukeAdapter, DUTAdapter, SimulatedCalibrator, SimulatedDUT
from .storage import Settings
from .connection_history import CalibratorHistory
from ..domain.types import TRUEVOLT_MODELS
from ..domain.connection_guide import address_modes, supported_modes
from ..instruments.dut import DUTSession, DUTSettings, DUTTransport, _dut_gpib_key, _DUT_LAN_MODES
from ..instruments.discovery import discover_duts, align_dut_direct_lan, _dut_discovery_usb_key

@dataclass
class ConnectionSettings:
    mode: str = 'AUTO'
    model: str = 'AUTO'
    address: str = ''
    port: int = 3490
    mask: str = '255.255.255.0'
    direct: bool = True
    baudrate: int = 9600
    data_bits: int = 8
    parity: str = 'N'
    stop_bits: int = 1
    flow: str = 'NONE'
    eol: str = 'CRLF'
    timeout: float = 3.0
    operation_timeout: float = 60.0
    lan_protocol: str = 'AUTO'

class CandidateChoice(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__('여러 DUT가 응답했습니다. 연결할 장비를 선택하세요.')

class DeviceHub:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.fluke = FlukeAdapter()
        self.calibrator_history = CalibratorHistory(self.settings, self.fluke.manager.previous_routes())
        self.calibrator = self.fluke
        self.session = DUTSession()
        self.dut = None
        self.demo = False
        self.defaults = ''
        self.last_output = None
        self.connection_cancel = threading.Event()
        self.log = lambda message: self.fluke.manager.record_event(message, notify=True)

    def cancel(self):
        self.connection_cancel.set()
        self.calibrator.request_stop()
        self.session.cancel.set()

    def saved(self, role):
        value = self.settings.get(role + '_connection', {})
        try:
            return ConnectionSettings(**value)
        except (TypeError, ValueError):
            return ConnectionSettings()

    def connect_calibrator(self, config):
        self.connection_cancel.clear()
        m = self.fluke.manager
        source = ''
        address = config.address.strip()
        mode = config.mode
        if mode not in supported_modes(config.model):
            raise ValueError(f'{config.model}의 연결 방식을 확인하세요. 지원 방식은 {", ".join(supported_modes(config.model)[1:])}입니다.')
        if address:
            options = address_modes(address, config.model)
            if not options or mode != 'AUTO' and mode not in options:
                raise ValueError(f'{config.model}의 {mode} 설정에서 주소 형식을 확인하세요. '
                                 'LAN은 본체 IP, RS232는 COM 번호, GPIB는 0~30 또는 VISA 주소를 사용합니다. '
                                 'USB는 5730A의 COM 번호 또는 5560A의 USB VISA 주소를 사용합니다.')
            if mode == 'AUTO' and address.isdigit():
                mode = 'GPIB'
        if mode == 'AUTO' and '::' in address:
            prefix = address.partition('::')[0].upper()
            if prefix.startswith(('USB', 'GPIB')):
                mode = 'USB' if prefix.startswith('USB') else 'GPIB'
        com = bool(re.fullmatch(r'COM[1-9]\d*', address, re.I))
        lan_ip = address if mode in ('AUTO', 'LAN') and not com else ''
        lan_unavailable = ''
        if lan_ip and config.direct:
            try:
                source = align_dut_direct_lan(lan_ip, config.mask, self.connection_cancel)
            except (OSError, ValueError) as exc:
                if mode != 'AUTO' or self.connection_cancel.is_set():
                    raise
                lan_unavailable = str(exc)
                self.log(f'유선 LAN 경로 확인 실패 · USB, RS232, GPIB 검색을 계속합니다. {exc}')
        other = self.session.settings
        excluded = other.address if other else ''
        try:
            self.calibrator_history.preserve_previous_routes()
        except OSError as exc:
            self.log(f'이전 기준기 주소 기록을 디스크에 저장하지 못했습니다. {exc}')
        info = m.scan_and_auto_connect(mode, model=config.model, lan_ip=lan_ip, lan_port=config.port,
            lan_unavailable=lan_unavailable,
            serial_port=address if mode == 'RS232' or (com and mode in ('AUTO', 'USB')) else '',
            visa_resource=address if mode in ('USB', 'GPIB') and '::' in address else '',
            gpib_addr=int(address.lstrip('0') or '0') if mode == 'GPIB' and address.isdigit() else None,
            baudrate=config.baudrate, data_bits=config.data_bits, parity=config.parity, stop_bits=config.stop_bits,
            flow_control=config.flow, eol=config.eol, timeout_sec=config.timeout,
            operation_timeout_sec=config.operation_timeout, source_ip=source,
            excluded_com=excluded.upper() if other and other.mode == 'SERIAL' else '',
            excluded_gpib=_dut_gpib_key(excluded), excluded_usb=_dut_discovery_usb_key(excluded),
            excluded_ip=excluded if other and other.mode.endswith(('LAN', 'TCP', '11')) else '')
        self.calibrator = self.fluke
        self.last_output = self.calibrator.output_state()
        try:
            self.settings.set('calibrator_connection', asdict(config))
            if m.active_endpoint is not None:
                self.calibrator_history.remember(m.active_endpoint, config)
        except OSError as exc:
            self.log(f'기준기 연결 설정을 디스크에 저장하지 못했습니다. {exc}')
        self.log(f'기준기 연결 · {info.idn_string} · {info.resource_name}')
        return info

    def connect_dut(self, config, candidate=None):
        self.connection_cancel.clear()
        self.session.cancel.clear()
        mode = config.mode
        address = config.address.strip()
        if mode in ('AUTO', 'USB') and re.fullmatch(r'COM[1-9]\d*', address, re.I):
            mode = 'RS232'
        elif mode == 'AUTO' and '::' in address:
            prefix = address.upper().partition('::')[0]
            if prefix.startswith('USB'):
                mode = 'USB'
            elif prefix.startswith('GPIB'):
                mode = 'GPIB'
        if mode == 'RS232' and config.model in TRUEVOLT_MODELS:
            raise ValueError(f'{config.model}는 USB, LAN 또는 옵션 GPIB로 연결하세요.')
        if mode in ('LAN', 'USB') and config.model == '34401A':
            raise ValueError('34401A는 RS232 또는 GPIB를 선택하세요. USB 변환기도 해당 방식을 사용합니다.')
        search = (mode == 'AUTO' or (mode == 'LAN' and config.lan_protocol == 'AUTO')
                  or (mode in ('USB', 'RS232', 'GPIB') and not address))
        if candidate is not None:
            target, expected = candidate.settings, candidate.identity
        elif search:
            info = self.fluke.manager.info
            choices, messages = discover_duts(self.connection_cancel, excluded_resource=info.resource_name,
                excluded_ip=info.discovered_ip, include_usb=mode in ('AUTO', 'USB') and config.model != '34401A',
                include_lan=mode in ('AUTO', 'LAN') and config.model != '34401A',
                include_serial=mode in ('AUTO', 'RS232') and config.model not in TRUEVOLT_MODELS,
                include_gpib=mode in ('AUTO', 'GPIB'), model_filter=config.model,
                include_usb_com=mode == 'USB' and config.model in ('AUTO', 'PPA'),
                lan_ip=address if mode in ('AUTO', 'LAN') else '',
                direct_mask=config.mask if config.direct else '')
            for message in messages:
                self.log(message)
            if not choices:
                details = '\n'.join(messages[-3:]) or 'LAN이면 본체 IP를 입력하세요.'
                raise ConnectionError(f'{config.model} DUT를 찾지 못했습니다.\n{details}')
            preferred = self.settings.get('dut_identity')
            matches = [item for item in choices if item.identity == preferred]
            if len(choices) > 1 and (config.model != 'AUTO' or len(matches) != 1):
                raise CandidateChoice(choices)
            candidate = matches[0] if matches else choices[0]
            target, expected = candidate.settings, candidate.identity
        else:
            mode = {'USB': 'VISA', 'RS232': 'SERIAL', 'LAN': config.lan_protocol}.get(mode, mode)
            if mode == 'GPIB' and address.isdigit():
                address = f'GPIB0::{int(address)}::INSTR'
            source = align_dut_direct_lan(address, config.mask, self.connection_cancel) if config.mode == 'LAN' and config.direct else ''
            target = DUTSettings(mode=mode, address=address, port=config.port, timeout=max(1, config.timeout),
                eol=config.eol, baudrate=config.baudrate, data_bits=config.data_bits, parity=config.parity,
                stop_bits=config.stop_bits, flow=config.flow, source_ip=source)
            expected = ''
        if config.model != 'AUTO':
            target = replace(target, model_hint=config.model)
        self.check_dut_endpoint(target)
        try:
            identity = self.session.connect(replace(target, timeout=max(10, config.timeout)), expected_identity=expected)
            self.dut = DUTAdapter(self.session)
            self.defaults = self.dut.initialize()
        except Exception:
            self.session.disconnect()
            self.dut = None
            raise
        self.settings.set('dut_connection', asdict(config))
        self.settings.set('dut_identity', identity)
        self.settings.set('dut_endpoint', asdict(target))
        self.log(f'DUT 연결 · {identity} · {target.mode} {target.address} · {self.defaults}')
        return identity

    def check_dut_endpoint(self, target):
        info = self.fluke.manager.info
        if not info.connected:
            return
        address, resource = target.address.upper(), info.resource_name.upper()
        same = address == resource
        if target.mode == 'SERIAL':
            same |= address == resource.partition(' / ')[0]
        if target.mode in _DUT_LAN_MODES:
            same |= target.address == info.discovered_ip
        for key in (_dut_gpib_key, _dut_discovery_usb_key):
            actual, other = key(target.address), key(info.resource_name)
            same |= actual is not None and actual == other
        if same:
            raise ValueError('기준기가 사용하는 연결입니다. DUT 주소를 확인하세요.')

    def set_demo(self, enabled, model='5560A', dut_model='34465A'):
        if self.fluke.connected or self.session.connected:
            raise RuntimeError('장비 연결을 해제한 뒤 미리보기를 전환하세요.')
        self.demo = enabled
        self.calibrator = SimulatedCalibrator(model) if enabled else self.fluke
        self.dut = SimulatedDUT(dut_model) if enabled and dut_model else None

    def disconnect(self, role):
        # Releasing the DUT while a source is on must also leave the source safe.
        if self.calibrator.connected:
            self.calibrator.standby()
        if role in ('calibrator', 'all'):
            self.calibrator.disconnect()
        if role in ('dut', 'all'):
            self.session.disconnect()
            self.dut = None

    def recover(self):
        self.connection_cancel.clear()
        self.calibrator.clear_cancel()
        self.session.cancel.clear()
        self.calibrator.standby()
        if not self.session.connected and self.session.recovery_target:
            self.session.recover_connection()
            self.dut = DUTAdapter(self.session)
            self.defaults = self.dut.initialize()
        return '같은 장비와 STBY를 확인했습니다. 시작을 누르면 선택한 행부터 재개합니다.'

    def live_read(self, channel=1):
        if self.demo:
            raise ValueError('미리보기에서는 측정표를 실행해 흐름을 확인하세요.')
        self.session.cancel.clear()
        return self.session.read(channel, include_dc=True)

    def health_check(self):
        if self.demo:
            return self.calibrator.output_state()
        self.last_output = self.calibrator.output_state() if self.calibrator.connected else None
        if self.session.connected and self.last_output is not True:
            self.session.cancel.clear()
            self.session.check_connection()
        return self.last_output

    def manual_command(self, command):
        if self.demo or not self.fluke.connected:
            raise ValueError('실제 기준기를 연결하세요.')
        command = command.strip()
        manager = self.fluke.manager
        manager.cancel_event.clear()
        if '?' in command:
            return manager.query(command)
        if command.upper() != 'OPER':
            self.fluke.standby()
        manager.send_command(command, check_before=True)
        self.last_output = self.fluke.output_state()
        return '명령 완료 · ' + ('OPER' if self.last_output else 'STBY')
