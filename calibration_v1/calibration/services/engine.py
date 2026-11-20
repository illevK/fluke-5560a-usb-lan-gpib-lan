"""One synchronous state machine, driven by a worker. No Qt, Tk or timers."""
from __future__ import annotations
from dataclasses import dataclass, replace
from enum import Enum
from threading import Event, Thread, Lock
from uuid import uuid4
import math
import time

from ..domain.document import RunOptions
from ..domain.types import TRUEVOLT_MODELS
from ..domain.units import SIUnits
from ..domain.planning import Point, QUANTITIES, wiring_change, advance_wiring
from ..domain.commands import CalibratorCommandBuilder
from ..instruments.transport import OperationCancelled

class State(str, Enum):
    IDLE = '대기'
    PREPARING = '장비 설정'
    ZEROING = '0 Ω 널 측정'
    SETTLING = '출력 안정화'
    READING = 'DUT 값 읽기'
    MANUAL = '측정값 직접 입력 · 출력 유지'
    HOLDING = '진행 보류 · 출력 유지'
    WIRING = '결선 변경 대기'
    STOPPING = 'STBY 확인 중'
    STOPPED = '정지'
    COMPLETE = '완료'
    ERROR = '중단'

@dataclass(frozen=True)
class EngineEvent:
    run_id: str
    kind: str
    state: State
    row_id: str = ''
    message: str = ''
    measured: str = ''
    completed: int = 0
    total: int = 0
    output: bool | None = None
    simulated: bool = False

class MeasurementEngine:
    def __init__(self, calibrator, dut=None, publish=lambda event: None):
        self.calibrator, self.dut, self.publish = calibrator, dut, publish
        self.stop_requested = Event()
        self.hold_requested = Event()
        self.resume_requested = Event()
        self.state = State.IDLE
        self.run_id = uuid4().hex
        self.output = None
        self.completed = self.total = 0
        self.current = None
        self._stop_lock = Lock()
        self._emergency_thread = None
        self._emergency_error = None
        self._entry_lock = Lock()
        self._entry_ready = Event()
        self._entry_row = None
        self._entry_text = ''

    def emit(self, kind='state', message='', measured=''):
        self.publish(EngineEvent(self.run_id, kind, self.state, self.current.uid if self.current else '',
            message, measured, self.completed, self.total, self.output, self.calibrator.simulated))

    def transition(self, state, message=''):
        self.state = state
        self.emit(message=message)

    def request_hold(self):
        self.hold_requested.set()
        self.resume_requested.clear()

    def resume(self):
        self.hold_requested.clear()
        self.resume_requested.set()

    def request_stop(self):
        with self._stop_lock:
            self.stop_requested.set()
            self._entry_ready.set()
            self.resume_requested.set()
            self.calibrator.request_stop()
            if self.dut:
                self.dut.request_stop()
            if self._emergency_thread is None:
                # A blocked DUT USB/VISA read must not delay the other device's
                # STBY. The source lock still allows only one source transaction.
                self._emergency_thread = Thread(target=self._emergency_standby, name='calibration-standby', daemon=True)
                self._emergency_thread.start()

    def _emergency_standby(self):
        row_id = self.current.uid if self.current else ''
        try:
            self.calibrator.standby()
            self.output = False
            message = 'STBY 확인 완료 · 측정을 정지합니다.'
        except Exception as error:
            self._emergency_error = error
            self.output = None
            message = f'출력 상태 미확인 · 본체에서 STBY를 확인하세요. {error}'
        self.publish(EngineEvent(self.run_id, 'emergency', State.STOPPING, row_id, message,
                                completed=self.completed, total=self.total, output=self.output,
                                simulated=self.calibrator.simulated))

    def check_stop(self):
        if self.stop_requested.is_set():
            raise OperationCancelled('사용자 정지 요청')

    def checkpoint(self):
        self.check_stop()
        if self.hold_requested.is_set():
            previous = self.state
            self.transition(State.HOLDING, '출력을 유지하고 진행을 보류합니다. 결선 변경에는 정지를 누르세요.')
            while self.hold_requested.is_set():
                self.check_stop()
                if self.output is True and not self.calibrator.output_state():
                    raise RuntimeError('진행 보류 중 기준기 출력이 STBY로 바뀌었습니다.')
                self.resume_requested.wait(.5)
            self.check_stop()
            self.transition(previous)

    def wait_dwell(self, seconds):
        # Hold time is excluded; a running command always finishes before hold.
        remaining = seconds
        while remaining > 0:
            self.checkpoint()
            start = time.monotonic()
            if self.stop_requested.wait(min(.05, remaining)):
                self.check_stop()
            remaining -= time.monotonic() - start

    def standby(self):
        self.calibrator.standby()
        self.output = False
        self.emit(kind='output')

    def submit_entry(self, run_id, row_id, text):
        text = text.strip()
        with self._entry_lock:
            if (not text or run_id != self.run_id or row_id != self._entry_row
                    or self.stop_requested.is_set() or self.hold_requested.is_set()):
                return False
            self._entry_text = text
            self._entry_row = None  # Enter can be accepted only once for this point.
            self._entry_ready.set()
            return True

    def wait_for_manual_value(self, point):
        with self._entry_lock:
            self._entry_row = point.uid
            self._entry_text = ''
            self._entry_ready.clear()
        try:
            self.transition(State.MANUAL, f'{point.step.step_no}행 측정값을 입력하고 Enter를 누르세요. 출력 유지 중입니다.')
            while True:
                self.checkpoint()
                submitted = self._entry_ready.wait(.5)
                self.checkpoint()
                if not self.calibrator.output_state():
                    raise RuntimeError('직접 입력 중 기준기 출력이 중단되었습니다. 이 행을 다시 확인하세요.')
                if submitted:
                    with self._entry_lock:
                        return SIUnits.with_nominal_unit(self._entry_text, point.step.nominal_raw)
        finally:
            with self._entry_lock:
                self._entry_row = None

    def zero(self, point, options):
        self.transition(State.ZEROING, '2선식 저항: 0 Ω 출력 → DUT 널값 확인')
        self.dut.prepare_zero()
        command, _ = CalibratorCommandBuilder(self.calibrator.model).build_resistance_cmd(value=0, unit='OHM', zcomp='NONE')
        zero = replace(point, command=command)
        self.calibrator.apply(zero)
        self.checkpoint()
        self.calibrator.operate()
        self.output = True
        self.emit(kind='output')
        self.wait_dwell(options.dwell)
        reading = self.dut.read_zero()
        self.check_stop()
        if not self.calibrator.output_state():
            raise RuntimeError('0 Ω 널 측정 중 기준기 출력이 중단되었습니다.')
        self.standby()
        self.dut.apply_zero(reading)
        self.emit(kind='log', message=f'0 Ω 널값 {reading[1]} Ω 적용 확인')

    def run(self, plan: tuple[Point, ...], options: RunOptions):
        self.total = len(plan)
        wiring, null_ready = None, False
        try:
            options.validate()
            if not plan:
                raise ValueError('실행할 측정 행이 없습니다.')
            # A previously attached DUT that lost its link must be explicitly
            # recovered or disconnected. Never switch to manual halfway through a run.
            if options.capture and self.dut is not None and not self.dut.connected:
                raise ConnectionError('DUT 연결이 끊겼습니다. 복구하거나 DUT 연결을 해제한 뒤 시작하세요.')
            record_mode = options.record_mode(self.dut is not None)
            if self.dut and self.calibrator.simulated != self.dut.simulated:
                raise ValueError('실제 장비와 미리보기 장비를 함께 실행할 수 없습니다.')
            self.check_stop()
            self.calibrator.clear_cancel()
            if self.dut:
                self.dut.clear_cancel()
            for index, point in enumerate(plan):
                self.current = point
                self.checkpoint()
                boundary = wiring_change(wiring, point.wiring)
                if index and (boundary or point.step.breakpoint):
                    self.standby()
                    self.transition(State.WIRING, boundary or '중단점에서 멈췄습니다. 시작을 누르면 이 행부터 진행합니다.')
                    return
                if point.error:
                    raise ValueError(point.error)
                self.transition(State.PREPARING, f'{point.step.nominal_raw} · CH{point.channel}')
                self.standby()
                self.checkpoint()
                resistance = bool(record_mode == 'dut' and self.dut.model in TRUEVOLT_MODELS
                                  and point.step.func_type == 'OHM' and options.zero_resistance)
                if record_mode == 'dut':
                    self.dut.prepare(point, self.standby)
                if resistance and not null_ready:
                    self.zero(point, options)
                    null_ready = True
                elif point.step.func_type != 'OHM':
                    null_ready = False
                self.checkpoint()
                self.calibrator.apply(point)
                self.checkpoint()
                if not options.auto_oper:
                    self.transition(State.STOPPED, '설정 완료 · STBY. 출력을 실행하려면 자동 출력을 켜세요.')
                    return
                self.calibrator.operate()
                self.output = True
                self.transition(State.SETTLING, f'{options.dwell:g}초 안정화 대기')
                self.wait_dwell(options.dwell)
                if not self.calibrator.output_state():
                    raise RuntimeError('출력 안정화 중 기준기가 STBY로 전환됐습니다.')
                measured = ''
                result_message = '출력 완료'
                if record_mode == 'dut':
                    result_message = '완료 · DUT'
                    identity = self.dut.identity
                    self.transition(State.READING)
                    self.checkpoint()
                    sample = self.dut.capture(point, resistance_null=resistance)
                    self.check_stop()
                    if (sample.identity != identity or sample.channel != point.channel
                            or sample.simulated != self.calibrator.simulated):
                        raise RuntimeError('DUT 식별·채널·측정값 출처가 바뀌었습니다.')
                    metric, unit = QUANTITIES[point.step.func_type]
                    value = sample.values.get(metric)
                    if value is None or not math.isfinite(value):
                        raise ValueError('유효한 DUT 측정값이 없습니다. 측정 기능·입력 범위·결선을 확인하세요.')
                    if sample.vi_modes and metric in ('voltage', 'current'):
                        if sample.vi_modes[0 if metric == 'voltage' else 1] != 'RMS':
                            raise ValueError('DUT의 RMS 측정 설정을 확인하세요.')
                    if not self.calibrator.output_state():
                        raise RuntimeError('DUT 값을 읽는 동안 기준기 출력이 중단됐습니다. 이 값은 기록하지 않습니다.')
                    raw = (sample.raw_values or {}).get(metric, f'{value:.16g}')
                    measured = f'{raw} {unit}'.strip()
                elif record_mode == 'entry':
                    result_message = '완료 · 직접 입력'
                    measured = self.wait_for_manual_value(point)
                self.check_stop()
                self.completed += 1
                self.emit(kind='result', measured=measured, message=result_message)
                wiring = advance_wiring(wiring, point.wiring)
                self.checkpoint()
            self.standby()
            self.transition(State.COMPLETE, '선택한 채널·그룹의 측정을 마쳤습니다. STBY 확인 완료.')
        except Exception as error:
            stopping = self.stop_requested.is_set() or isinstance(error, (OperationCancelled, InterruptedError))
            self.transition(State.STOPPING, '출력을 끄고 응답을 확인합니다.')
            try:
                if self._emergency_thread is not None:
                    self._emergency_thread.join()
                    if self._emergency_error is not None:
                        raise self._emergency_error
                else:
                    self.standby()
                suffix = 'STBY 확인 완료'
            except Exception as standby_error:
                self.output = None
                suffix = f'출력 상태 미확인. 본체에서 STBY를 확인하세요. {standby_error}'
            self.transition(State.STOPPED if stopping else State.ERROR, f'{error} · {suffix}')
        finally:
            self.emit(kind='finished')
