import datetime
import json
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'outputs'))
import fluke_5560a_controller as f

report = {'time': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(),
          'test': 'After operator-confirmed instrument restart; no OPER or OUT commands', 'cycles': []}
manager = f.Fluke5560AConnectionManager()
try:
    source = f.AutoNetworkAligner.select_source_ip('17', '169.254.1.1')
    for cycle in range(1, 4):
        item = {'cycle': cycle, 'source_ip': source}
        report['cycles'].append(item)
        print(f'Cycle {cycle}: connecting...', flush=True)
        info = manager.scan_and_auto_connect('LAN', '169.254.1.1', 3490,
                                              timeout_sec=5, source_ip=source)
        item['idn'] = info.idn_string
        item['operate'] = manager.query('OPER?')
        item['idn_repeat'] = manager.query('*IDN?')
        item['errors_read'] = []
        for _ in range(5):
            error = manager.query('ERR?')
            item['errors_read'].append(error)
            if error.startswith('0,'):
                break
        if item['operate'] != '0':
            raise RuntimeError('Instrument is operating; test stopped without changing output.')
        manager.disconnect()
        item['disconnect'] = 'STBY acknowledged, LOCAL acknowledged, TCP closed'
        item['success'] = True
        print(json.dumps(item, ensure_ascii=False), flush=True)
        time.sleep(.7)
except Exception as exc:
    report['failure'] = f'{type(exc).__name__}: {exc}'
    print(report['failure'], flush=True)
finally:
    manager.disconnect(standby=False)
    (root / 'work/live_reconnect_result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
sys.exit(1 if 'failure' in report else 0)
