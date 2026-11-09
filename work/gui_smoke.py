import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f

app = f.Fluke5560AControllerApp()
app.withdraw()
app.var_conn_mode.set('SIMULATION')
app._on_connect_clicked()
deadline = time.monotonic() + 5
while app._io_busy and time.monotonic() < deadline:
    app.update()
    time.sleep(.01)
assert app.conn_mgr.info.connected, 'Simulation connect failed'
assert app.is_operating is False
assert app._get_selected_adapter_id() == str(app.adapters[0]['idx']), app.var_adapter_choice.get()
assert app.seq_steps, 'Existing spreadsheet data failed to initialize'
app._on_oper_clicked()
deadline = time.monotonic() + 5
while app._io_busy and time.monotonic() < deadline:
    app.update()
    time.sleep(.01)
assert app.conn_mgr.query('OPER?') == '1'
app._on_stby_clicked()
deadline = time.monotonic() + 5
while app._io_busy and time.monotonic() < deadline:
    app.update()
    time.sleep(.01)
assert app.conn_mgr.query('OPER?') == '0'
app._on_disconnect_clicked()
while app._io_busy and time.monotonic() < deadline:
    app.update()
    time.sleep(.01)
assert not app.conn_mgr.info.connected
app._on_close()
print('GUI smoke passed: physical adapter inventory, async connect, simulation, spreadsheet, disconnect.')
