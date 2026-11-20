"""Extension points for device identity and LAN probes.

Register plugins at startup. Probing sends only identification commands; the
driver configures a device only after identification and explicit connection.
"""
IDENTIFIERS = []
LAN_PROBES = []

def register_identity(name, identify, lan_probes=()):
    if any(existing == name for existing, _ in IDENTIFIERS):
        raise ValueError('중복 장비 등록: ' + name)
    probes = tuple(lan_probes)
    for mode, port, eol in probes:
        if mode not in ('SCPI_TCP', 'VXI11') or not 1 <= port <= 65535 or eol not in ('LF', 'CRLF', 'CR'):
            raise ValueError('추가 장비 LAN 프로토콜 설정을 확인하세요.')
    IDENTIFIERS.append((name, identify))
    LAN_PROBES.extend(probes)

def identify_registered(identity):
    for _, identify in IDENTIFIERS:
        profile = identify(identity)
        if profile is not None:
            return profile
    return None
