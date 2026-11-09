"""GUI integration tests with local TCP peers only; never connect real hardware."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f
import test_communications as peers


class AsyncGuiTests(unittest.TestCase):
    def setUp(self):
        adapters=[dict(idx='17',name='Test Ethernet',state='Up',wired=True,hardware=True,
                       addresses=[dict(ip='127.0.0.1',prefix=8)])]
        self.adapter_patch=patch.object(f.AutoNetworkAligner,'list_ethernet_interfaces',return_value=adapters)
        self.adapter_patch.start()
        self.app=f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.reports=[]
        self.app._show_report=lambda *args: self.reports.append(args)
        self.app.var_adapter_choice.set('자동: Windows 경로 사용')
        self.app.var_lan_ip.set('127.0.0.1')
        self.app.var_timeout.set('0.2')
        self.app.var_operation_timeout.set('2')
        self.app.var_seq_auto_oper.set(True)
        self.app.seq_steps=[f.SequenceStep(i,'-','DC','-',f'{i} V','DCV',f'OUT {i} V, 0 HZ',f'{i} V') for i in (1,2)]
        self.app.excel_grid.render_steps(self.app.seq_steps)

    def tearDown(self):
        self.app._on_seq_stop_auto(silent=True)
        self.app.conn_mgr.disconnect(standby=False)
        self.app._on_close()
        self.adapter_patch.stop()

    def pump_until(self,predicate,timeout=5):
        deadline=time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            self.app.update()
            time.sleep(.005)
        self.app.update()
        self.assertTrue(predicate(),'GUI condition timed out')

    def connect(self,server):
        self.app.var_lan_port.set(str(server.port))
        self.app._on_connect_clicked()
        self.pump_until(lambda:not self.app._io_busy)
        self.assertTrue(self.app.conn_mgr.info.connected)

    def test_slow_sequence_keeps_gui_responsive_and_waits_before_next_row(self):
        with peers.SlowOutputTests().peer(delay=.4) as server:
            self.connect(server)
            heartbeat=[]
            self.app.seq_running=True
            self.app._run_next_auto_step(0,1,200)
            self.app.after(30,lambda:heartbeat.append(True))
            self.pump_until(lambda:bool(heartbeat))
            self.assertTrue(self.app._io_busy)
            self.assertNotIn('OUT 2 V, 0 HZ; OPER',server.commands)
            self.pump_until(lambda:not self.app.seq_running and not self.app._io_busy)
            self.assertEqual(server.commands.count('OUT 1 V, 0 HZ; OPER'),1)
            self.assertEqual(server.commands.count('OUT 2 V, 0 HZ; OPER'),1)
            self.assertFalse(self.app.is_operating)
            self.assertTrue(self.app.conn_mgr.info.connected)
            self.assertIn('STBY 확인 완료',self.app.var_active_row_banner.get())
            self.assertEqual(self.reports,[])

    def test_timeout_marks_failed_row_and_does_not_execute_next(self):
        with peers.SlowOutputTests().peer(missing=True) as server:
            self.connect(server)
            self.app.var_operation_timeout.set('1')
            self.app.seq_running=True
            self.app._run_next_auto_step(0,1,200)
            self.pump_until(lambda:not self.app._io_busy)
            self.assertFalse(self.app.seq_running)
            self.assertFalse(self.app.conn_mgr.info.connected)
            self.assertIsNone(self.app.is_operating)
            self.assertEqual(self.app.seq_steps[0].status,'⚠ 오류')
            self.assertNotIn('OUT 2 V, 0 HZ; OPER',server.commands)
            self.assertEqual(server.commands[-1],'STBY')
            self.assertIn('OUT 1 V',self.app._last_report)

    def test_standby_button_interrupts_pending_output_without_replay(self):
        with peers.SlowOutputTests().peer(missing=True) as server:
            self.connect(server)
            self.app.var_operation_timeout.set('60')
            self.app._execute_sequence_step(0)
            self.pump_until(lambda:server.commands.count('*OPC?')>=2)
            started=time.monotonic()
            self.app._on_stby_clicked()
            self.pump_until(lambda:not self.app._io_busy,timeout=2)
            self.assertLess(time.monotonic()-started,2)
            self.assertIsNone(self.app.is_operating)
            self.assertFalse(self.app.conn_mgr.info.connected)
            self.assertEqual(server.commands[-1],'STBY')
            self.assertEqual(server.commands.count('OUT 1 V, 0 HZ; OPER'),1)

    def test_device_error_stops_rows_but_keeps_confirmed_standby(self):
        with peers.SlowOutputTests().peer(error='4103,"Output settling timed out"') as server:
            self.connect(server)
            self.app.seq_running=True
            self.app._run_next_auto_step(0,1,200)
            self.pump_until(lambda:not self.app._io_busy)
            self.assertFalse(self.app.seq_running)
            self.assertTrue(self.app.conn_mgr.info.connected)
            self.assertFalse(self.app.is_operating)
            self.assertNotIn('OUT 2 V, 0 HZ; OPER',server.commands)
            self.assertIn('4103',self.app._last_report)

    def test_pause_prevents_scheduling_next_row_after_late_reply(self):
        with peers.SlowOutputTests().peer(delay=.3) as server:
            self.connect(server)
            self.app.seq_running=True
            self.app._run_next_auto_step(0,1,200)
            self.app._on_seq_stop_auto(silent=True)
            self.pump_until(lambda:not self.app._io_busy)
            self.assertIsNone(self.app.seq_after_id)
            self.assertNotIn('OUT 2 V, 0 HZ; OPER',server.commands)
            self.assertTrue(self.app.conn_mgr.info.connected)


if __name__=='__main__':
    unittest.main(verbosity=2)
