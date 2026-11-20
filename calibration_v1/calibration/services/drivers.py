"""Device contracts: new DUTs implement prepare and capture, never UI callbacks."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Protocol, Callable
import math
import threading
import time

from ..domain.commands import MODEL_PROFILES, CalibratorCommandBuilder
from ..domain.planning import Point, QUANTITIES
from ..domain.types import DMM_MODELS
from ..instruments.fluke import Fluke5560AConnectionManager
from ..instruments.dut import DUTSession, DUTProfile, DUTSample, DUTSettings, DUTTransport, DUTConfigurationError
from ..instruments.transport import InstrumentProtocolError, InstrumentCommandError, OperationCancelled

class Calibrator(Protocol):
    model: str
    simulated: bool
    def standby(self) -> None: ...
    def apply(self, point: Point) -> None: ...
    def operate(self) -> None: ...
    def output_state(self) -> bool: ...
    def request_stop(self) -> None: ...
    def clear_cancel(self) -> None: ...

class DUTDriver(Protocol):
    def initialize(self) -> str: ...
    def prepare(self, point: Point, standby: Callable[[], None]) -> None: ...
    def capture(self, point: Point, resistance_null=False) -> DUTSample: ...

class FlukeAdapter:
    simulated = False
    def __init__(self, manager=None):
        self.manager = manager or Fluke5560AConnectionManager()
        self._stop_latched = threading.Event()

    @property
    def model(self):
        return self.manager.profile.model

    @property
    def connected(self):
        return self.manager.info.connected

    def output_state(self):
        return self.manager.check_output_state() == '1'

    def standby(self):
        m = self.manager
        # Clearing cancellation must wait until the interrupted transaction releases
        # its lock. Otherwise an emergency STBY could uncancel a pending *OPC?.
        with m._lock:
            self._standby_locked()

    def _standby_locked(self):
        m = self.manager
        m.cancel_event.clear()
        if not m.info.connected:
            if m.recovery_target is not None:
                m.recover_standby()
            else:
                raise ConnectionError('기준기 연결이 없어 STBY를 확인할 수 없습니다. 본체에서 출력을 확인하세요.')
        if m._first_command_check or self.output_state():
            m.send_command('STBY')
        if self.output_state():
            m.output_unconfirmed = True
            raise InstrumentProtocolError('STBY 응답을 확인하지 못했습니다. 본체에서 출력을 끄세요.')
        m.output_unconfirmed = False

    def apply(self, point):
        m = self.manager
        if self._stop_latched.is_set():
            raise OperationCancelled('정지 요청 이후 출력 설정을 차단했습니다.')
        if m.output_unconfirmed:
            raise ConnectionError('기준기 출력 상태를 먼저 확인하세요.')
        # The engine brings output to STBY before configuring either instrument.
        if point.step.func_type == 'OHM' and ('ZCOMP NONE' in point.command
                or ('EXTSENSE OFF' in point.command and 'RCOMP ON' not in point.command)):
            m.set_uncompensated_resistance_in_standby(point.command)
        else:
            m.send_command(point.command, check_before=True)
        if point.dual:
            m.profile.verify_dual_current_readback(point.command, m.query('OUT?'))
        post = next((part.strip().split()[-1] for part in point.command.split(';')
                     if part.strip().startswith('CUR_POST ')), None)
        if post and m.query('CUR_POST?').upper() != post:
            raise InstrumentCommandError(f'{m.profile.model} 전류 단자 설정 확인 실패')
        if m.profile.model == '5730A':
            status = m._status_5730a(m.query('ISR?'))
            if status & ((1 << 10) | (1 << 3)):
                raise InstrumentCommandError('5730A에서 Wideband 또는 외부 증폭기가 선택되어 있습니다. 본체 출력을 선택하세요.')

    def operate(self):
        with self.manager._lock:
            if self._stop_latched.is_set():
                raise OperationCancelled('정지 요청 이후 OPER를 차단했습니다.')
            self.manager.send_command('OPER', check_before=True)
            if not self.output_state():
                raise InstrumentCommandError('기준기의 OPER 상태를 확인하지 못했습니다.')

    def request_stop(self):
        self._stop_latched.set()
        self.manager.request_abort()

    def clear_cancel(self):
        self._stop_latched.clear()
        self.manager.cancel_event.clear()

    def disconnect(self):
        self.manager.disconnect()

class PowerAnalyzerDriver:
    """Validated family-level implementation shared by WT, PPA and Hioki."""
    def __init__(self, session):
        self.session = session

    def initialize(self):
        return self.session.apply_measurement_defaults()

    def prepare(self, point, standby):
        if point.step.func_type not in ('DCV', 'ACV', 'DCI', 'ACI', 'DCP', 'ACP', 'PF', 'FREQ'):
            raise DUTConfigurationError('이 전력 분석기는 저항 측정을 지원하지 않습니다.')
        self.session.check_connection()
        if point.channel not in self.session.available_channels:
            raise DUTConfigurationError(f'DUT에 CH{point.channel} 입력이 없습니다.')

    def capture(self, point, resistance_null=False):
        return self.session.read(point.channel, include_dc=point.step.func_type in ('DCV', 'DCI', 'DCP'), fresh=True)

class WTDriver(PowerAnalyzerDriver):
    def prepare(self, point, standby):
        super().prepare(point, standby)
        if self.session.profile.wt3000_numeric:
            mode = 'DC' if point.step.func_type in ('DCV', 'DCI', 'DCP') else 'RMS'
            for quantity in ('VOLTAGE', 'CURRENT'):
                self.session._set_wt_default(f':INPUT:{quantity}:MODE:ELEMENT{point.channel}', mode)

class PPADriver(PowerAnalyzerDriver):
    def prepare(self, point, standby):
        if self.session.profile.ppa_common_only:
            raise DUTConfigurationError('이 PPA 모델은 공통 값 조회만 지원합니다. 자동 기록에는 모델별 새 데이터 확인 규칙이 필요합니다.')
        super().prepare(point, standby)
        if point.step.func_type in ('DCV', 'DCI', 'DCP'):
            command = f'COUPLI,PHASE{point.channel}'
            self.session.transport.write(command + ',AC+DC')
            if self.session.transport.query(command + '?').strip() != '0':
                raise DUTConfigurationError(f'PPA CH{point.channel} AC+DC 커플링 설정 확인 실패')

class DMMDriver(PowerAnalyzerDriver):
    def prepare(self, point, standby):
        s = self.session
        s.check_connection()
        if point.channel != 1:
            raise DUTConfigurationError('이 멀티미터는 CH1만 지원합니다. 재사용 구간은 CH1@로 지정하세요.')
        targets = {'DCV': 'VOLT:DC', 'ACV': 'VOLT:AC', 'DCI': 'CURR:DC', 'ACI': 'CURR:AC',
                   'OHM': 'RES', 'FREQ': 'FREQ'}
        target = targets.get(point.step.func_type)
        if target is None:
            raise DUTConfigurationError('멀티미터로 전력·역률을 직접 측정할 수 없습니다.')
        if s._dmm_function() != target:
            standby()
            s.transport.write(f'FUNC "{target}"')
        if s._dmm_function() != target:
            raise DUTConfigurationError(f'DUT {target} 설정 확인 실패')
        if target != 'FREQ':
            command = f'SENS:{target}:RANG:AUTO'
            s.transport.write(command + ' ON')
            if s.transport.query(command + '?').strip() not in ('1', '+1', 'ON'):
                raise DUTConfigurationError(f'DUT {target} 자동 범위 설정 확인 실패')
        if point.terminal is not None:
            s.verify_truevolt_current_terminal(point.step.func_type, point.terminal, ensure_standby=standby)

    def capture(self, point, resistance_null=False):
        return self.session.read(1, include_dc=point.step.func_type in ('DCV', 'DCI'), fresh=True,
                    dmm_terminal=point.terminal, expected_dmm_function=point.step.func_type,
                    resistance_null=resistance_null)

@dataclass(frozen=True)
class DriverRegistration:
    name: str
    matches: Callable[[DUTProfile], bool]
    create: Callable[[DUTSession], DUTDriver]
    choices: tuple

class DriverRegistry:
    def __init__(self):
        self.registrations = []

    def register(self, name, matches, create, *, identify=None, lan_probes=(), choices=()):
        if any(item.name == name for item in self.registrations):
            raise ValueError('중복 드라이버 이름: ' + name)
        if identify is not None:
            from ..instruments.catalog import register_identity
            register_identity(name, identify, lan_probes)
        self.registrations.append(DriverRegistration(name, matches, create, tuple(choices)))

    def connection_choices(self):
        return [('AUTO · 모델 자동 인식', 'AUTO'),
                *(choice for entry in self.registrations for choice in entry.choices)]

    def create(self, session):
        for entry in self.registrations:
            if entry.matches(session.profile):
                return entry.create(session)
        raise ValueError('이 DUT의 자동 측정 드라이버가 등록되지 않았습니다.')

REGISTRY = DriverRegistry()
REGISTRY.register('Yokogawa WT', lambda p: p.family == 'WT', WTDriver,
                  choices=[('Yokogawa WT 시리즈 · WT310(E), WT500, WT1800, WT3000, WT5000 등', 'WT')])
REGISTRY.register('N4L PPA', lambda p: p.family == 'PPA', PPADriver,
                  choices=[('N4L PPA 시리즈 · PPA510, PPA550, PPA2530, PPA5511 등', 'PPA')])
REGISTRY.register('Hioki PW', lambda p: p.family == 'HIOKI', PowerAnalyzerDriver,
                  choices=[('Hioki PW 시리즈 · PW3390, PW6001', 'HIOKI')])
REGISTRY.register('Keysight 344xx', lambda p: p.family == 'DMM', DMMDriver,
                  choices=[(f'Keysight {model}', model) for model in DMM_MODELS])

class DUTAdapter:
    simulated = False
    def __init__(self, session):
        self.session = session
        self.driver = REGISTRY.create(session)

    @property
    def model(self):
        return self.session.profile.model if self.session.profile else ''

    @property
    def connected(self):
        return self.session.connected

    @property
    def identity(self):
        return self.session.identity

    @property
    def channels(self):
        return self.session.available_channels

    def prepare(self, point, standby):
        return self.driver.prepare(point, standby)

    def initialize(self):
        return self.driver.initialize()

    def capture(self, point, resistance_null=False):
        return self.driver.capture(point, resistance_null)

    def prepare_zero(self):
        self.session.prepare_truevolt_resistance_null()

    def read_zero(self):
        return self.session.read_truevolt_resistance_zero()

    def apply_zero(self, reading):
        self.session.set_truevolt_resistance_null(reading)

    def request_stop(self):
        self.session.cancel.set()

    def clear_cancel(self):
        self.session.cancel.clear()

class SimulatedCalibrator:
    """Separate from every physical transport. All events carry simulated=True."""
    simulated = True
    connected = True
    def __init__(self, model='5560A'):
        self.model = model
        self.active = False
        self.commands = []
        self.point = None

    def standby(self):
        self.commands.append('STBY')
        self.active = False

    def apply(self, point):
        MODEL_PROFILES[self.model].validate_command(point.command)
        self.commands.append(point.command)
        self.point = point

    def operate(self):
        self.commands.append('OPER')
        self.active = True

    def output_state(self):
        return self.active

    def request_stop(self):
        pass

    def clear_cancel(self):
        pass

    def disconnect(self):
        self.standby()
        self.connected = False

class SimulatedDUT:
    simulated = True
    connected = True
    identity = '미리보기 DUT · 실제 측정값 아님'
    model = '34465A'
    channels = (1, 2, 3, 4)
    def __init__(self, model='34465A'):
        self.model = model
        self.prepared = []

    def prepare(self, point, standby):
        self.prepared.append(point.step.func_type)

    def capture(self, point, resistance_null=False):
        from ..domain.units import SIUnits
        from ..domain.clipboard import SmartClipboardSequenceParser
        metric, unit = QUANTITIES[point.step.func_type]
        if metric == 'pf':
            value = abs(SmartClipboardSequenceParser.parse_power_factor(point.step.nominal_raw)[0])
        else:
            value = SIUnits.input_value(point.step.nominal_raw, unit)
        return DUTSample(self.identity, point.channel, '', {metric: value}, '미리보기', True,
                         raw_values={metric: f'{value:.12g}'})

    def prepare_zero(self):
        self.prepared.append('ZERO')

    def read_zero(self):
        return .001, '0.001'

    def apply_zero(self, reading):
        self.zero = reading

    def request_stop(self):
        pass

    def clear_cancel(self):
        pass
