"""Exercise linked capture with a local calibrator peer and a controlled DUT."""
import datetime
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs"))
import fluke_5560a_controller as f
import test_communications as local


class LinkedDutTests(unittest.TestCase):
    def setUp(self):
        self.release_read = threading.Event()
        self.started_read = threading.Event()
        self.read_calls = []
        self.adapters = patch.object(f.AutoNetworkAligner, "list_ethernet_interfaces", return_value=[])
        self.adapters.start()
        self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.app._show_report = lambda *_: None
        self.app.var_adapter_choice.set("자동: Windows 경로 사용")
        self.app.var_lan_ip.set("127.0.0.1")
        self.app.var_timeout.set("0.2")
        self.app.var_operation_timeout.set("2")
        self.app.var_seq_dwell_sec.set("0.2")
        self.app.dut_session.profile = f.identify_dut("YOKOGAWA,WT310E,DUT-1234,1.01")
        self.app.dut_session.identity = "YOKOGAWA,WT310E,DUT-1234,1.01"

    def tearDown(self):
        self.release_read.set()
        if self.app is not None:
            self.pump_until(lambda: not self.app._dut_busy and not self.app._io_busy)
            self.app.conn_mgr.disconnect(standby=False)
            self.app._on_close()
            self.pump_until(lambda: self.app._dut_poll_id is None)
        self.adapters.stop()

    def pump_until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.005)
        self.assertTrue(condition(), "GUI condition timed out")

    def connect(self, peer):
        self.app.var_lan_port.set(str(peer.port))
        self.app._on_connect_clicked()
        self.pump_until(lambda: not self.app._io_busy)
        self.assertTrue(self.app.conn_mgr.info.connected)

    def set_rows(self, steps):
        self.app.seq_steps = steps
        self.app.excel_grid.render_steps(steps)

    def sample(self, channel, *, raw_values=None, **quantities):
        values = {key: 0.0 for key, _, _ in f.DUT_METRICS}
        values.update(quantities)
        return f.DUTSample(self.app.dut_session.identity, channel, datetime.datetime.now().isoformat(),
                           values, "", raw_values=raw_values)

    def acv_peer(self, pending='0,"No error"', setting_error='0,"No error"', output=None):
        error, operating = pending, False

        def reply(conn, command):
            nonlocal error, operating
            if command.startswith("OUT "):
                error = setting_error
            elif command == "OPER":
                operating = True
            elif command == "STBY":
                operating = False
            response = {"*IDN?": local.IDN, "COMM_MODE? TELNET": "COMP", "*OPC?": "1",
                        "OPER?": "1" if operating else "0", "OUT?": output}.get(command)
            if command == "ERR?":
                response, error = error, '0,"No error"'
            if response is not None:
                conn.sendall((response + "\r\n").encode())

        return local.FakeInstrument(handler=reply)

    def test_dual_current_records_current_instead_of_companion_voltage(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("60 Hz\t1 A"))
        self.app.var_dual_current.set(True)
        self.app._on_current_group_changed()
        self.app.var_dual_ac_voltage.set("500 mV")
        self.app._on_dual_current_voltages_applied()
        with self.acv_peer(output="0.5,V,1.0,A,60") as peer:
            self.connect(peer)
            self.app.dut_session.read = lambda channel, **kwargs: self.sample(channel, current=.999, voltage=.5)
            self.app._start_filtered_sequence(0, 0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual(float(self.app.seq_steps[0].measured_raw.split()[0]), .999)
            self.assertTrue(self.app.seq_steps[0].measured_raw.endswith(" A"))
            self.assertIn("OUT 0.5 V, 1 A, 60 HZ; WAVE SINE,SINE", peer.commands)

    def test_acv_waits_for_setpoint_ack_then_records_fresh_rms(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("50 Hz\t1 V"))
        with self.acv_peer() as peer:
            self.connect(peer)
            self.app.dut_session.read = lambda channel, **kwargs: self.sample(
                channel, voltage=.9998, raw_values={"voltage": "0.999800E+00"})
            self.app._start_filtered_sequence(0, 0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual(self.app.seq_steps[0].measured_raw, "0.999800E+00 V")
            index = peer.commands.index("OUT 1 V, 50 HZ")
            self.assertEqual(peer.commands[index - 1:index + 6],
                             ["ERR?", "OUT 1 V, 50 HZ", "*OPC?", "ERR?", "OPER", "*OPC?", "ERR?"])

    def test_appended_batches_run_in_order_within_active_current_group(self):
        batches = (('50 Hz\t1 mV', True), ('1 A\n5 A', False),
                   ('120 V / 1 A', False), ('60 Hz\t120 V\t1 A\t0.5', False))
        for text, first in batches:
            with patch.object(self.app, 'clipboard_get', return_value=text):
                self.app._on_paste_into_sheet_button(replace_all=first, append=not first)
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            self.app.dut_session.read = lambda channel, **_: self.sample(
                channel, voltage=.0009998, dc_current=.999, power=119.9, pf=.4999,
                raw_values={"voltage": "0.0009998"})
            self.app.var_seq_start_row.set('3')
            self.app._on_seq_start_from_selected_or_range(all_rows=True)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual([row.measured_raw for row in self.app.seq_steps],
                             ['0.0009998 V', '0.999 A', '', '119.9 W', '0.4999'])
            self.assertEqual(self.app.excel_grid._row_widgets[0]["measurement_var"].get(), "0.9998 mV")
            self.assertEqual([cmd for cmd in peer.commands if cmd.startswith('OUT ')], [
                'OUT 0.001 V, 50 HZ', 'OUT 1 A, 0 HZ; OPER',
                'OUT 120 V, 1 A, 0 HZ; OPER', 'OUT 120 V, 1 A, 60 HZ; DPF 0.5, LAG; OPER',
            ])
            self.assertFalse(self.app.is_operating)

    def test_acv_pending_error_and_setpoint_error_both_stop_before_oper(self):
        for pending, setting_error, expected in (
            ('4100,"Compliance voltage exceeded"', '0,"No error"', "출력 전 대기 중이던 오류"),
            ('0,"No error"', '-102,"Syntax error"', "OUT 1 V, 50 HZ"),
        ):
            with self.subTest(expected=expected), self.acv_peer(pending, setting_error) as peer:
                self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("50 Hz\t1 V"))
                self.connect(peer)
                with patch.object(self.app.dut_session, "read") as read:
                    self.app._start_filtered_sequence(0, 0)
                    self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
                    read.assert_not_called()
                self.assertIn(expected, self.app.var_active_row_banner.get())
                self.assertEqual("STBY" in peer.commands, pending.startswith("0,"))
                self.assertNotIn("OPER", peer.commands)
                self.assertEqual("OUT 1 V, 50 HZ" in peer.commands, pending.startswith("0,"))
                self.app.conn_mgr.disconnect(standby=False)

    def test_auto_capture_after_dwell_and_excludes_other_current_group(self):
        rows = [
            f.SequenceStep(1, "120 V / 1 A", "DC", "-", "0.12 kW", "DCP", "OUT 120 V, 1 A, 0 HZ", "120 W"),
            f.SequenceStep(2, "120 V / 5 A", "DC", "-", "600 W", "DCP", "OUT 120 V, 5 A, 0 HZ", "600 W"),
            f.SequenceStep(3, "-", "DC", "-", "-5 V", "DCV", "OUT -5 V, 0 HZ", "-5 V"),
        ]
        self.set_rows(rows)
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            def read(channel, *, include_dc, fresh):
                self.read_calls.append((channel, include_dc, fresh, list(peer.commands), time.monotonic()))
                return self.sample(channel, power=1234.56, voltage=5.0, dc_voltage=-4.99,
                                   raw_values={"power": "+1.234560000000000000E+03"})
            self.app.dut_session.read = read
            start = time.monotonic()
            self.app._start_filtered_sequence(0, 2)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual([row.measured_raw for row in rows], ["+1.234560000000000000E+03 W", "", "-4.99 V"])
            self.assertEqual(self.app.excel_grid._row_widgets[0]["measurement_var"].get(), "1.234560000000000000 kW")
            self.app.excel_grid._set_measurement_selection(0, 0)
            self.assertEqual(self.app.excel_grid.measurement_selection_text(), "1.234560000000000000 kW\r\n")
            self.assertEqual([call[:3] for call in self.read_calls], [(1, True, True), (1, True, True)])
            self.assertGreaterEqual(self.read_calls[0][-1] - start, .2)
            self.assertEqual(self.read_calls[0][-2][-1], "OPER?")
            self.assertNotIn("OUT -5 V, 0 HZ; OPER", self.read_calls[0][-2])
            self.assertNotIn("OUT 120 V, 5 A, 0 HZ; OPER", peer.commands)
            self.assertFalse(self.app.is_operating)

    def test_selected_row_links_and_records_pf_without_a_unit(self):
        row = f.SequenceStep(1, "120 V / 1 A", "60 Hz", "LEAD", "0.5 LEAD", "PF",
                             "OUT 120 V, 1 A, 60 HZ", "0.5")
        self.set_rows([row])
        self.app.excel_grid.selected_row_idx = 0
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            self.app.dut_session.read = lambda channel, **_: self.sample(channel, pf=.49851)
            self.app._on_seq_output_selected()
            self.pump_until(lambda: row.status == "✓ 완료 · DUT")
            self.assertEqual(row.measured_raw, "0.49851")
            self.assertEqual(self.app.excel_grid._row_widgets[0]["measurement_var"].get(), "0.49851")

    def test_invalid_sample_stops_run_and_keeps_previous_cell(self):
        rows = [
            f.SequenceStep(i, "-", "DC", "-", f"{i} V", "DCV", f"OUT {i} V, 0 HZ", f"{i} V",
                           measured_raw="0.8 V")
            for i in (1, 2)
        ]
        self.set_rows(rows)
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            self.app.dut_session.read = lambda channel, **_: self.sample(channel, voltage=1.0)
            self.app._start_filtered_sequence(0, 1)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual(rows[0].status, "⚠ DUT 기록 실패")
            self.assertEqual(rows[0].measured_raw, "0.8 V")
            self.assertIn("STBY", peer.commands)
            self.assertFalse(self.app.is_operating)
            self.assertNotIn("OUT 2 V, 0 HZ; OPER", peer.commands)

    def test_standby_does_not_wait_for_dut_and_rejects_late_reply(self):
        row = f.SequenceStep(1, "-", "DC", "-", "1 V", "DCV", "OUT 1 V, 0 HZ", "1 V",
                             measured_raw="0.9 V")
        self.set_rows([row])
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            def read(channel, **_):
                self.started_read.set()
                self.release_read.wait(3)
                return self.sample(channel, dc_voltage=1.0)
            self.app.dut_session.read = read
            self.app._on_seq_step_next()
            self.pump_until(self.started_read.is_set)
            self.app.var_operation_timeout.set("")
            self.app._on_stby_clicked()
            self.pump_until(lambda: "STBY" in peer.commands and not self.app._io_busy, timeout=2)
            self.assertTrue(self.app._dut_busy)
            self.assertFalse(self.app.is_operating)
            self.release_read.set()
            self.pump_until(lambda: not self.app._dut_busy)
            self.assertEqual(row.measured_raw, "0.9 V")
            self.assertIsNone(self.app._seq_dut_pending)

    def test_close_requests_standby_while_dut_is_waiting(self):
        row = f.SequenceStep(1, "-", "DC", "-", "1 V", "DCV", "OUT 1 V, 0 HZ", "1 V")
        self.set_rows([row])
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            def read(channel, **_):
                self.started_read.set()
                self.release_read.wait(3)
                return self.sample(channel, dc_voltage=1.0)
            self.app.dut_session.read = read
            self.app._on_seq_step_next()
            self.pump_until(self.started_read.is_set)
            self.app._on_close()
            self.pump_until(lambda: "STBY" in peer.commands and not self.app._io_busy, timeout=2)
            self.assertTrue(self.app._dut_busy)
            self.assertFalse(self.app.is_operating)
            self.release_read.set()
            self.pump_until(lambda: self.app._dut_poll_id is None)
            self.assertEqual(row.measured_raw, "")
            self.app = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
