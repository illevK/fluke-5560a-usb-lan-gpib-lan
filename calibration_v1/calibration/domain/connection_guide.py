"""Front-panel and PC setup for the supported Fluke remote interfaces."""
import re
from .commands import MODEL_PROFILES


def supported_modes(model):
    modes = MODEL_PROFILES[model].interfaces if model in MODEL_PROFILES else ('LAN', 'USB', 'RS232', 'GPIB')
    return ('AUTO', *modes)


def address_modes(address, model):
    value = address.strip().upper()
    modes = set(supported_modes(model)) - {'AUTO'}
    if not value:
        return modes
    if re.fullmatch(r'COM[1-9][0-9]*', value):
        return modes & ({'USB', 'RS232'} if model in ('AUTO', '5730A') else {'RS232'})
    if re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{1,3}){3}', value):
        return modes & {'LAN'}
    if value.isascii() and value.isdigit():
        number = value.lstrip('0') or '0'
        return modes & {'GPIB'} if len(number) <= 2 and int(number) <= 30 else set()
    if value.startswith('GPIB') and '::' in value:
        return modes & {'GPIB'}
    if value.startswith('USB') and '::' in value and model != '5730A':
        return modes & {'USB'}
    return set()


_HOST = 'SETUP → INSTMT SETUP → REMOTE SETUP'
_REMOTE = {
    '5560A': 'Setup → System Settings → Remote Port Setup',
    '5730A': 'Setup Menu → Instrument Setup → Remote Port',
    '5522A': _HOST, '5520A': _HOST, '5080A': _HOST,
}
_REMOTE_PORT = {'LAN': 'Ethernet', 'USB': 'USB', 'RS232': 'RS-232', 'GPIB': 'GPIB'}
_PC = {
    'LAN': '본체의 현재 IP와 TCP 포트를 입력하세요. 기본 포트는 3490입니다. PC와 직결했다면 직결을 켜고 본체 서브넷 마스크를 입력하세요.',
    'RS232': '장치 관리자의 COM 번호를 입력하거나 비워 두고 검색하세요. 속도, 데이터·정지 비트, 패리티, 흐름 제어(Stall), 줄 끝 문자를 본체와 대조하세요.',
    'GPIB': 'GPIB 어댑터 드라이버와 VISA를 설치하세요. GPIB0은 본체 주소(0~30), 다른 보드는 전체 VISA 주소를 입력하세요. 비워 두면 검색합니다.',
    'USB': '후면 USB Device 포트를 PC에 연결하세요. 장비 모델을 고르면 Windows에서 확인할 장치가 표시됩니다.',
}


def connection_guide(model, mode):
    """Return instructions for the front panel and this application's connection form."""
    menu = _REMOTE.get(model)
    if mode == 'AUTO':
        if not menu:
            return ('본체 전면의 모델명을 확인하세요. 모델과 케이블을 선택하면 해당 메뉴와 단자가 표시됩니다.',
                    '처음에는 AUTO로 연결하세요. LAN 장비를 찾지 못하면 본체 화면의 현재 IP를 입력하세요.')
        modes = ' · '.join(supported_modes(model)[1:])
        panel = f'{menu}에서 사용할 통신 포트를 확인하세요.'
        if model in ('5520A', '5522A', '5080A'):
            panel += ' USB–RS232 변환 케이블은 RS232를 선택하세요.'
        return panel, f'지원 연결은 {modes}입니다. AUTO로 찾지 못하면 연결 방식을 선택하세요.'
    if not menu:
        return '장비 모델을 선택하면 해당 기준기의 메뉴 경로와 단자를 볼 수 있습니다.', _PC[mode]
    if mode not in supported_modes(model):
        return f'{model}에서 사용할 수 있는 연결은 {" · ".join(supported_modes(model)[1:])}입니다.', 'USB–RS232 변환기를 사용할 때는 RS232를 선택하세요.'
    if model == '5560A':
        port = 'USBTMC' if mode == 'USB' else _REMOTE_PORT[mode]
        panel = f'{menu}에서 {port}를 켜세요.'
    elif model == '5730A':
        panel = f'{menu} → Active Remote Port에서 {_REMOTE_PORT[mode]}를 선택하세요.'
    else:
        port = {'LAN': 'Ethernet', 'RS232': 'serial', 'GPIB': 'gpib'}[mode]
        panel = f'{menu}\nHOST 항목을 선택해 HOST {port}로 맞추세요.'
    pc = _PC[mode]
    if mode == 'LAN':
        if model == '5560A':
            panel += ' Ethernet에서 Remote Interface를 Computer로 맞추고 Allowed Host IP Addresses에 PC 유선 IP가 포함되는지 확인하세요.'
        else:
            settings = 'LAN SETUP' if model == '5080A' else 'Ethernet Setup'
            panel += f' {settings}에서 IP, 서브넷 마스크와 포트를 확인하세요.'
    elif mode == 'RS232':
        if model in ('5520A', '5522A'):
            panel += '\n후면 SERIAL 1 FROM HOST에 널모뎀 케이블을 연결하세요. HOST SETUP에서 REMOTE I/F를 comp로 맞추고 BAUD·STALL·EOL을 확인하세요.'
        elif model == '5080A':
            panel += ' 후면 RS-232를 연결하고 SERIAL SETUP에서 REMOTE I/F를 comp로 맞추세요.'
        else:
            panel += ' RS-232 설정에서 Remote Interface를 Computer로 맞추세요.'
    elif mode == 'USB':
        if model == '5730A':
            pc = '후면 USB Device 포트를 연결하고 장치 관리자에서 가상 COM 번호를 확인하세요. 주소는 비워 두거나 COM 번호를 입력하세요.'
        else:
            pc = '후면 USB/Control Device(B형)를 연결하세요. NI-VISA 설치 후 장치 관리자의 USB Test and Measurement Devices를 확인하세요. 주소는 비워 두거나 USB VISA 주소를 입력하세요.'
    else:
        panel += ' 본체에 표시된 GPIB 주소를 확인하세요.'
    if model in ('5520A', '5522A', '5080A'):
        panel += ' 변경 후 PREV MENU로 나가 STORE CHANGES로 저장하세요.'
    return panel, pc
