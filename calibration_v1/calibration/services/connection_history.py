"""Reusable PC settings from identified calibrators, separate from search preferences."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from ..domain.commands import MODEL_PROFILES, serial_baudrates
from ..domain.connection_guide import address_modes


@dataclass(frozen=True)
class CalibratorPreset:
    model: str
    serial: str
    interface: str
    address: str
    connected_at: str = ''
    port: int = 0
    source_ip: str = ''
    mask: str = ''
    direct: bool | None = None
    framing: tuple = ()
    eol: str = ''
    timeout: float | None = None
    operation_timeout: float | None = None

    @property
    def mode(self):
        return {'USB-COM': 'USB', 'COM': 'AUTO'}.get(self.interface, self.interface)

    def matches(self, model, mode):
        return (model == 'AUTO' or model == self.model) and (
            mode == 'AUTO' or mode == self.mode or self.interface == 'COM' and mode in ('RS232', 'USB'))

    def usable(self):
        if self.model not in MODEL_PROFILES or not isinstance(self.address, str) or not self.address:
            return False
        mode = 'RS232' if self.interface == 'COM' else self.mode
        if mode not in address_modes(self.address, self.model) or self.eol not in ('', 'LF', 'CR', 'CRLF'):
            return False
        if self.timeout is not None and not .2 <= self.timeout <= 60:
            return False
        if self.operation_timeout is not None and not 1 <= self.operation_timeout <= 300:
            return False
        if self.interface in ('COM', 'USB-COM', 'RS232'):
            if not isinstance(self.framing, (tuple, list)) or len(self.framing) != 5:
                return False
            speed, bits, parity, stop, flow = self.framing
            return (speed in serial_baudrates(self.model) and bits in (7, 8) and parity in ('N', 'O', 'E')
                    and stop in (1, 2) and flow in ('NONE', 'RTS/CTS', 'XON/XOFF'))
        if self.interface == 'LAN':
            return isinstance(self.port, int) and 1 <= self.port <= 65535 and isinstance(self.mask, str)
        return self.interface in ('USB', 'GPIB') and not self.framing

    def title(self):
        route = {'USB-COM': 'USB 가상 COM', 'COM': 'COM · 케이블 미확인'}.get(self.interface, self.mode)
        unit = f' · S/N {self.serial}' if self.serial else ''
        return f'{self.model}{unit} · {route}'

    def details(self):
        lines = [f'마지막 성공  {self.connected_at}' if self.connected_at else '이전 버전 주소 기록 · 일련번호 및 시각 미기록']
        if self.interface == 'LAN':
            lines.append(f'장비 {self.address}' + (f'  ·  TCP {self.port}' if self.port else ''))
            if self.source_ip:
                lines.append(f'PC {self.source_ip}')
            if self.direct is not None:
                lines.append(f'유선 직결  ·  {self.mask}' if self.direct else 'Windows 네트워크 경로 사용')
            else:
                lines.append('직결 여부와 마스크 미기록 · 현재 설정을 확인하세요.')
        else:
            lines.append(self.address)
        if self.framing:
            speed, bits, parity, stop, flow = self.framing
            lines.append(f'{speed} baud  ·  {bits}{parity}{stop}  ·  흐름 제어 {flow}')
        if self.eol:
            lines.append(f'PC 송신 줄 끝 {self.eol}')
        if self.timeout is not None and self.operation_timeout is not None:
            lines.append(f'응답 {self.timeout:g}초  ·  출력 완료 {self.operation_timeout:g}초')
        return '\n'.join(lines)

    def apply(self, config):
        mode = self.mode
        if self.interface == 'COM' and config.mode in ('RS232', 'USB'):
            mode = config.mode
        values = dict(model=self.model, mode=mode, address=self.address)
        if self.interface == 'LAN':
            values['port'] = self.port or config.port
            if self.direct is not None:
                values['direct'] = self.direct
            if self.mask:
                values['mask'] = self.mask
        if self.framing:
            values.update(zip(('baudrate', 'data_bits', 'parity', 'stop_bits', 'flow'), self.framing))
        if self.eol:
            values['eol'] = self.eol
        if self.timeout is not None:
            values['timeout'] = self.timeout
        if self.operation_timeout is not None:
            values['operation_timeout'] = self.operation_timeout
        return replace(config, **values)

    @classmethod
    def from_endpoint(cls, target, config):
        serial = target.identity.split(',')[2].strip()
        at = datetime.now().astimezone().isoformat(sep=' ', timespec='seconds')
        values = {}
        if target.interface == 'LAN':
            # The mask is an operator-entered PC setting, never inferred from an ID reply.
            entered = config.address.strip().split('.')
            same_ip = len(entered) == 4 and all(p.isascii() and p.isdigit() and len(p) <= 3 for p in entered)
            same_ip = same_ip and '.'.join(str(int(p)) for p in entered) == target.address
            direct = config.direct if same_ip or not config.direct else None
            values.update(port=target.port, source_ip=target.source_ip, direct=direct,
                          mask=config.mask if direct else '', eol=target.eol)
        elif target.serial_settings:
            values.update(framing=tuple(target.serial_settings), eol=target.eol)
        return cls(target.model, serial, target.interface, target.address, at, timeout=target.timeout,
                   operation_timeout=config.operation_timeout, **values)


class CalibratorHistory:
    def __init__(self, settings, previous_routes):
        self.settings = settings
        saved = settings.get('calibrator_previous_routes')
        self.previous_routes = saved if isinstance(saved, dict) else previous_routes
        self.older = []
        for model, route in self.previous_routes.items():
            if model not in MODEL_PROFILES or not isinstance(route, dict):
                continue
            kind = route.get('interface')
            if 'LAN' in MODEL_PROFILES[model].interfaces and route.get('ip') and isinstance(route.get('port'), int):
                self.older.append(CalibratorPreset(model, '', 'LAN', route['ip'], port=route['port'],
                                                   source_ip=route.get('source_ip', '')))
            if route.get('com'):
                framing = route.get('serial_settings')
                if isinstance(framing, (list, tuple)) and len(framing) == 6:
                    if kind not in ('RS232', 'USB-COM', 'COM'):
                        kind = 'COM' if MODEL_PROFILES[model].usb_virtual_com else 'RS232'
                    self.older.append(CalibratorPreset(model, '', kind, route['com'], framing=tuple(framing[:5]),
                                                       eol=framing[5]))

    def preserve_previous_routes(self):
        # Later ID-only scans update the discovery cache, even if the status query fails.
        if not isinstance(self.settings.get('calibrator_previous_routes'), dict):
            self.settings.set('calibrator_previous_routes', self.previous_routes)

    def confirmed(self):
        records = []
        saved = self.settings.get('calibrator_presets', [])
        for row in saved if isinstance(saved, list) else ():
            try:
                preset = CalibratorPreset(**row)
                if preset.connected_at and preset.usable():
                    records.append(preset)
            except (TypeError, ValueError):
                continue
        return records

    def choices(self):
        records = self.confirmed()
        known = {(r.model, r.interface) for r in records}
        return records + [old for old in self.older if old.usable() and (old.model, old.interface) not in known]

    def remember(self, target, config):
        current = CalibratorPreset.from_endpoint(target, config)
        def key(r):
            return r.model, r.serial or r.address, r.interface
        records = [current, *(r for r in self.confirmed() if key(r) != key(current))]
        self.settings.set('calibrator_presets', [asdict(r) for r in records])
