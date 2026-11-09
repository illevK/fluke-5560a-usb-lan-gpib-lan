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
        with patch.object(f.Fluke5560AControllerApp, "_load_sample_into_sheet"):
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

    def sample(self, channel, **quantities):
        values = {key: 0.0 for key, _, _ in f.DUT_METRICS}
        values.update(quantities)
        return f.DUTSample(self.app.dut_session.identity, channel, datetime.datetime.now().isoformat(),
                           values, "")

    def test_auto_capture_after_dwell_and_excludes_other_current_group(self):
        rows = [
            f.SequenceStep(1, "120 V / 1 A", "DC", "-", "120 W", "DCP", "OUT 120 V, 1 A, 0 HZ", "120 W",
                           resolution_raw="0.0001 kW"),
            f.SequenceStep(2, "120 V / 5 A", "DC", "-", "600 W", "DCP", "OUT 120 V, 5 A, 0 HZ", "600 W"),
            f.SequenceStep(3, "-", "DC", "-", "-5 V", "DCV", "OUT -5 V, 0 HZ", "-5 V"),
        ]
        self.set_rows(rows)
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            def read(channel, *, include_dc, fresh):
                self.read_calls.append((channel, include_dc, fresh, list(peer.commands), time.monotonic()))
                return self.sample(channel, power=1234.56, voltage=5.0, dc_voltage=-4.99)
            self.app.dut_session.read = read
            start = time.monotonic()
            self.app._start_filtered_sequence(0, 2)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual([row.measured_raw for row in rows], ["1234.56 W", "", "-4.99 V"])
            self.assertEqual(self.app.excel_grid._measurement_display(0), "1.2346 kW")
            self.app.excel_grid._set_measurement_selection(0, 0)
            self.assertEqual(self.app.excel_grid.measurement_selection_text(), "1.2346 kW\r\n")
            self.assertEqual([call[:3] for call in self.read_calls], [(1, True, True), (1, True, True)])
            self.assertGreaterEqual(self.read_calls[0][-1] - start, .2)
            self.assertEqual(self.read_calls[0][-2][-1], "OPER?")
            self.assertNotIn("OUT -5 V, 0 HZ; OPER", self.read_calls[0][-2])
            self.assertNotIn("OUT 120 V, 5 A, 0 HZ; OPER", peer.commands)
            self.assertFalse(self.app.is_operating)

    def test_selected_row_links_and_records_pf_without_a_unit(self):
        row = f.SequenceStep(1, "120 V / 1 A", "60 Hz", "LEAD", "0.5 LEAD", "PF",
                             "OUT 120 V, 1 A, 60 HZ", "0.5", resolution_raw="0.001")
        self.set_rows([row])
        self.app.excel_grid.selected_row_idx = 0
        with local.SlowOutputTests().peer() as peer:
            self.connect(peer)
            self.app.dut_session.read = lambda channel, **_: self.sample(channel, pf=.49851)
            self.app._on_seq_output_selected()
            self.pump_until(lambda: row.status == "✓ 완료 · DUT")
            self.assertEqual(row.measured_raw, "0.49851")
            self.assertEqual(self.app.excel_grid._measurement_display(0), "0.499")

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
