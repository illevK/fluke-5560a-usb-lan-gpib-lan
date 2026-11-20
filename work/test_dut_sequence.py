"""Exercise linked capture with a local calibrator peer and a controlled DUT."""
import datetime
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs"))
import fluke_5560a_controller as f
import test_communications as local


class LinkedDutTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(f.Fluke5560AConnectionManager, "_connection_history_path",
                                       return_value=folder / "connections.json"))
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
        self.app.dut_session.check_connection = lambda: None

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

    def acv_peer(self, pending='0,"No error"', setting_error='0,"No error"', output=None, compensation="NONE"):
        error, operating, readback = pending, False, output

        def reply(conn, command):
            nonlocal error, operating, readback
            if command.startswith("OUT "):
                error = setting_error
                if output is None and " OHM" in command:
                    readback = f"{command.split()[1]},OHM,0E+00,0,0E+00"
                if command.endswith("; OPER"):
                    operating = True
            elif command == "OPER":
                operating = True
            elif command == "STBY":
                operating = False
            response = {"*IDN?": local.IDN, "COMM_MODE? TELNET": "COMP", "*OPC?": "1",
                        "OPER?": "1" if operating else "0", "OUT?": readback}.get(command)
            if command == "ZCOMP?":
                response = compensation(readback) if callable(compensation) else compensation
            if command == "ERR?":
                response, error = error, '0,"No error"'
            if response is not None:
                conn.sendall((response + "\r\n").encode())

        return local.FakeInstrument(handler=reply)

    def test_channel_preview_is_opt_in_and_assigns_three_repeating_blocks(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("\n".join(("1 V", "3 V", "5 V") * 3)))
        self.app.dut_session.profile = f.identify_dut("HIOKI,PW3390-02,TEST,V1.01")
        self.assertEqual(f.repeated_channel_blocks(self.app.seq_steps), [[0, 1, 2], [3, 4, 5], [6, 7, 8]])
        self.assertFalse(self.app.excel_grid.show_channels)
        with (patch.object(f.tk.Toplevel, "wait_visibility"), patch.object(f.tk.Toplevel, "grab_set")):
            self.app.btn_channel_assignment.invoke()
        dialog = next(child for child in self.app.winfo_children() if isinstance(child, f.tk.Toplevel))
        body, = dialog.winfo_children()
        buttons = {widget.cget("text"): widget for widget in body.winfo_children()[-1].winfo_children()}
        self.assertIsNone(self.app.seq_steps[3].dut_channel)
        self.assertNotIn("disabled", buttons["적용"].state())
        buttons["적용"].invoke()
        self.assertEqual([step.dut_channel for step in self.app.seq_steps], [1]*3 + [2]*3 + [3]*3)
        self.assertEqual(self.app.excel_grid._row_widgets[3]["labels"][10].cget("text"), "CH2")
        self.app._handle_excel_cell_edited(3, "-", "DC", "-", "2 V")
        self.app._recalculate_all_grid_steps()
        self.assertEqual(self.app.seq_steps[3].dut_channel, 2)
        self.assertEqual(f.repeated_channel_blocks(self.app.seq_steps), [])
        self.app._apply_channel_assignment(dict.fromkeys(range(9)))
        self.assertFalse(self.app.excel_grid.show_channels)

    def test_auto_assigns_each_of_seven_measurement_types_and_frequencies(self):
        steps = []
        expected = []
        for kind, unit in (("DCV", "V"), ("ACV", "V"), ("DCI", "A"), ("ACI", "A"),
                           ("DCP", "W"), ("ACP", "W"), ("PF", "")):
            frequencies = ("50 Hz", "60 Hz") if kind == "ACV" else (("60 Hz",) if kind.startswith("AC") or kind == "PF" else ("DC",))
            for frequency in frequencies:
                first = len(steps)
                values = ("1", "0.5") if kind == "PF" else ("1", "3")
                for target in values * 2:
                    steps.append(f.SequenceStep(len(steps) + 1, "-", frequency, "-", f"{target} {unit}".strip(),
                                                kind, "", ""))
                label = f.CHANNEL_MEASUREMENT_NAMES[kind]
                if kind == "ACV":
                    label += f" · {frequency}"
                expected.append((label, [[first, first + 1], [first + 2, first + 3]]))
        # An unmatched section should not prevent the other six from being assigned.
        self.assertEqual(f.detect_channel_sections(steps), expected)
        steps[-1].nominal_raw = "0.8"
        self.assertEqual(f.detect_channel_sections(steps), expected[:-1])

    def test_out_of_range_rows_keep_measurement_type_for_pattern_matching(self):
        self.app._handle_excel_paste_at_cell("1 A\n40 A\n1 A\n40 A", 0, 2, replace_all=True)
        steps = self.app.seq_steps
        self.assertEqual([step.func_type for step in steps], ["DCI", "OUT_OF_RANGE", "DCI", "OUT_OF_RANGE"])
        self.assertEqual(f.detect_channel_sections(steps), [("DC 전류", [[0, 1], [2, 3]])])

    def test_explicit_current_channels_include_extra_point_and_can_remap_extra_input(self):
        text = "\n".join(
            f"{label if index == 0 else ''}\t{freq}\t{amps} A"
            for freq in ("DC", "60 Hz")
            for label in ("CH-1", "CH-2", "CH-3", "CH-4", "CH-4*")
            for index, amps in enumerate((5, 10, 20, 50, 100, 200, 500) if label.endswith("*") else (5, 10, 20, 50, 100, 200))
        )
        self.app._handle_excel_paste_at_cell(text, 0, 2, replace_all=True)
        self.assertEqual([(name, list(map(len, blocks))) for name, blocks in f.detect_channel_sections(self.app.seq_steps)],
                         [("DC 전류", [6, 6, 6, 6, 7]), ("AC 전류", [6, 6, 6, 6, 7])])
        self.app.dut_session.profile = f.identify_dut("HIOKI,PW3390-02,TEST,V1.01")
        with (patch.object(f.tk.Toplevel, "wait_visibility"), patch.object(f.tk.Toplevel, "grab_set")):
            self.app.btn_channel_assignment.invoke()
        dialog = next(child for child in self.app.winfo_children() if isinstance(child, f.tk.Toplevel))
        body, = dialog.winfo_children()
        extra_row = body.winfo_children()[1]
        extra_row.winfo_children()[1].set("3@")
        buttons = {widget.cget("text"): widget for widget in body.winfo_children()[-1].winfo_children()}
        buttons["미리보기"].invoke()
        buttons["적용"].invoke()
        self.assertEqual(self.app.seq_steps[30].channel_key, (3, "@"))
        self.assertEqual(self.app.seq_steps[61].channel_key, (3, "@"))
        self.assertEqual(self.app.seq_steps[12].channel_key, (3, ""))
        self.assertEqual(self.app.seq_steps[30].func_type, "OUT_OF_RANGE")

    def test_unlabeled_fifth_group_can_use_a_four_channel_dut_input(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 A\n2 A\n" * 5))
        self.app.dut_session.profile = f.identify_dut("HIOKI,PW3390-02,TEST,V1.01")
        with (patch.object(f.tk.Toplevel, "wait_visibility"), patch.object(f.tk.Toplevel, "grab_set")):
            self.app.btn_channel_assignment.invoke()
        dialog = next(child for child in self.app.winfo_children() if isinstance(child, f.tk.Toplevel))
        body, = dialog.winfo_children()
        body.winfo_children()[1].winfo_children()[1].set("2@")
        buttons = {widget.cget("text"): widget for widget in body.winfo_children()[-1].winfo_children()}
        buttons["미리보기"].invoke()
        buttons["적용"].invoke()
        self.assertEqual([step.channel_key for step in self.app.seq_steps],
                         [key for key in ((1, ""), (2, ""), (3, ""), (4, ""), (2, "@")) for _ in range(2)])

    def test_channel_assignment_records_from_each_row_channel(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n2 V\n3 V\n4 V"))
        self.app.dut_session.profile = f.identify_dut("HIOKI,PW3390-02,TEST,V1.01")
        self.app.dut_session.input_channels = (1, 3)
        self.app._apply_channel_assignment({0: 1, 1: 3, 2: 1, 3: 3})

        def read(channel, **_):
            self.read_calls.append(channel)
            return self.sample(channel, dc_voltage=float(channel))

        self.app.dut_session.read = read
        with self.acv_peer() as peer:
            self.connect(peer)
            self.app._start_filtered_sequence(0, 3)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy, timeout=8)
            self.assertIn("CH1 완료", self.app.var_active_row_banner.get())
            self.assertEqual(self.read_calls, [1, 1])
            self.assertFalse(self.app.is_operating)
            self.app.var_seq_stop_row.set("4")
            self.app._handle_row_selected(1)
            self.app._on_seq_start_from_selected_or_range()
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy, timeout=8)
            self.assertEqual([cmd for cmd in peer.commands if cmd.startswith("OUT ")],
                             [f"OUT {i} V, 0 HZ; OPER" for i in (1, 3, 2, 4)])
        self.assertEqual(self.read_calls, [1, 1, 3, 3])
        self.assertEqual([step.measured_raw for step in self.app.seq_steps], ["1 V", "3 V", "1 V", "3 V"])
        self.assertEqual([step.nominal_raw for step in self.app.seq_steps], ["1 V", "2 V", "3 V", "4 V"])

    def test_channel_breakpoint_resume_uses_remaining_plan_including_earlier_rows(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n2 V\n3 V\n4 V"))
        self.app.dut_session.profile = f.identify_dut("HIOKI,PW3390-02,TEST,V1.01")
        self.app._apply_channel_assignment({0: 1, 1: 2, 2: 1, 3: 2})
        self.app.seq_steps[0].breakpoint = True
        self.app.dut_session.read = lambda channel, **_: self.sample(channel, dc_voltage=float(channel))
        with self.acv_peer() as peer:
            self.connect(peer)
            self.app._start_filtered_sequence(0, 3)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual(self.app.seq_paused_idx, 2)
            self.assertFalse(self.app.is_operating)
            self.app._on_seq_resume_from_paused()
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy, timeout=8)
            self.assertEqual([cmd for cmd in peer.commands if cmd.startswith("OUT ")],
                             [f"OUT {i} V, 0 HZ; OPER" for i in (1, 3)])
            self.assertEqual(self.app.seq_paused_idx, -1)

    def test_selecting_another_channel_starts_that_channel_and_extra_group_is_separate(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n2 V\n3 V\n4 V\n5 V"))
        self.app.dut_session.profile = f.identify_dut("HIOKI,PW3390-02,TEST,V1.01")
        self.app._apply_channel_assignment({0: 1, 1: 2, 2: 1, 3: 2, 4: (2, "@")})
        self.app.seq_steps[0].breakpoint = True
        self.app.var_seq_stop_row.set("5")
        self.app.dut_session.read = lambda channel, **_: self.sample(channel, dc_voltage=float(channel))
        with self.acv_peer() as peer:
            self.connect(peer)
            for index, expected in ((0, (1,)), (1, (1, 2, 4)), (4, (1, 2, 4, 5))):
                self.app._handle_row_selected(index)
                self.app._on_seq_start_from_selected_or_range()
                self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy, timeout=8)
                self.assertEqual([cmd for cmd in peer.commands if cmd.startswith("OUT ")],
                                 [f"OUT {i} V, 0 HZ; OPER" for i in expected])
                self.assertFalse(self.app.is_operating)
            self.assertIn("CH2@ 완료", self.app.var_active_row_banner.get())
            self.assertEqual(self.app.seq_steps[4].measured_raw, "2 V")

    def test_invalid_assigned_channel_rejects_sequence_before_output(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n3 V"))
        self.app.dut_session.input_channels = (1,)
        self.app._apply_channel_assignment({0: 1, 1: 2})
        with self.acv_peer() as peer:
            self.connect(peer)
            with patch.object(self.app, "_show_report") as report:
                self.app._start_filtered_sequence(1, 1)
            self.assertIn("CH2", report.call_args.args[1])
            self.assertFalse(any(cmd.startswith("OUT ") for cmd in peer.commands))
            self.assertFalse(self.app.seq_running)

    def test_dmm_function_mismatch_blocks_calibrator_output(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V"))
        session = self.app.dut_session
        session.identity = "Keysight Technologies,34465A,TEST,A.03"
        session.profile = f.identify_dut(session.identity)
        session.transport = Mock()
        session.transport.query.return_value = '"CURR"'
        with self.acv_peer() as peer:
            self.connect(peer)
            self.app._handle_row_direct_start(0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            session.transport.write.assert_called_once_with('FUNC "VOLT:DC"')
            self.assertEqual(session.transport.query.call_count, 2)
            self.assertIn("측정 기능 불일치", self.app.var_active_row_banner.get())
            self.assertFalse(any(cmd.startswith("OUT ") or cmd == "OPER" for cmd in peer.commands))

    def test_34465_start_selects_functions_and_waits_at_wiring_change(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n50 Hz\t2 V\n.5 A\n60 Hz\t.7 A"))
        session = self.app.dut_session
        session.identity = "Keysight Technologies,34465A,TEST,A.03"
        session.profile = f.identify_dut(session.identity)
        function = "CONT"
        switches = []
        terminals = {"DC": 10, "AC": 10}

        def query(command):
            return {"FUNC?": f'"{function}"', "READ?": "0.9998",
                    "SENS:CURR:DC:TERM?": f"+{terminals['DC']}",
                    "SENS:CURR:AC:TERM?": f"+{terminals['AC']}"}[command]

        with self.acv_peer() as peer:
            def write(command):
                nonlocal function
                if command.startswith("FUNC "):
                    function = command.split('"')[1]
                    switches.append((function, list(peer.commands)))
                elif command.startswith("SENS:CURR:"):
                    path, terminal = command.split()
                    terminals[path.split(":")[2]] = int(terminal)

            session.transport = Mock()
            session.transport.query.side_effect = query
            session.transport.write.side_effect = write
            self.connect(peer)
            self.app.var_seq_stop_row.set("4")
            self.app._handle_row_direct_start(0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual([mode for mode, _ in switches], ["VOLT:DC", "VOLT:AC"])
            before_ac = switches[1][1]
            self.assertIn("STBY", before_ac[before_ac.index("OUT 1 V, 0 HZ; OPER") + 1:])
            self.assertIn("결선 대기 · STBY 확인 완료", self.app.var_active_row_banner.get())
            self.assertEqual(self.app.seq_paused_idx, 2)
            self.assertEqual([step.measured_raw for step in self.app.seq_steps], ["0.9998 V", "0.9998 V", "", ""])

            self.app._on_seq_resume_from_paused()
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertEqual([mode for mode, _ in switches], ["VOLT:DC", "VOLT:AC", "CURR:DC", "CURR:AC"])
            self.assertEqual(terminals, {"DC": 3, "AC": 3})
            self.assertEqual([step.measured_raw for step in self.app.seq_steps], ["0.9998 V", "0.9998 V", "0.9998 A", "0.9998 A"])
            self.assertFalse(self.app.is_operating)

    def test_34465_terminal_mismatch_blocks_output_even_without_automatic_capture(self):
        session = self.app.dut_session
        session.identity = "Keysight Technologies,34465A,TEST,A.03"
        session.profile = f.identify_dut(session.identity)
        session.transport = Mock()
        replies = {"FUNC?": '"CURR"', "SENS:CURR:DC:TERM?": "+3", "ROUT:TERM?": "FRON"}
        session.transport.query.side_effect = replies.__getitem__
        self.app.var_seq_dut_capture.set(False)
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("3000 mA"))
        with self.acv_peer() as peer:
            self.connect(peer)
            self.app.var_dut_current_group.set(f.SequenceCurrentFilter.DUT_10A)
            self.app._on_dut_current_group_changed()
            self.pump_until(lambda: not self.app._io_busy)
            self.assertEqual(self.app._dut_current_group, f.SequenceCurrentFilter.DUT_10A)
            self.app._start_filtered_sequence(0, 0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertIn("측정표 10 A, 본체 3 A", self.app.var_active_row_banner.get())
            session.transport.write.assert_called_once_with("SENS:CURR:DC:TERM 10")
            self.assertFalse(any(cmd.startswith("OUT ") or cmd == "OPER" for cmd in peer.commands))

            def write(command):
                if command == "SENS:CURR:DC:TERM 10":
                    replies["SENS:CURR:DC:TERM?"] = "+10"

            session.transport.write.side_effect = write
            self.app._on_seq_resume_from_paused()
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertIn("OUT 3 A, 0 HZ; OPER", peer.commands)
            self.assertFalse(self.app.is_operating)

    def test_34465_groups_use_input_current_and_leave_over_limit_rows_in_table(self):
        session = self.app.dut_session
        session.identity = "Keysight Technologies,34465A,TEST,A.03"
        session.profile = f.identify_dut(session.identity)
        self.app._handle_excel_paste_at_cell("2.999 A\n3000 mA\n-3 A\n3.05 A\n5 A\n10 A\n10.1 A", 0, 2, replace_all=True)
        steps = self.app.seq_steps
        def eligible():
            return [row.nominal_raw for row in steps if self.app._step_in_current_group(row)]
        self.assertEqual(len(steps), 7)
        self.assertEqual(eligible(), ["2.999 A"])
        self.assertIn("DUT 3 A 단자", self.app._sequence_output_preview(steps[0])[0])
        self.assertIn("DUT 10 A 단자", self.app._sequence_output_preview(steps[1])[0])
        self.assertIn("DUT 10 A 단자", self.app._sequence_output_preview(steps[2])[0])
        self.app.var_dut_current_group.set(f.SequenceCurrentFilter.DUT_10A)
        self.app._on_dut_current_group_changed()
        self.assertEqual(eligible(), ["3000 mA", "-3 A", "3.05 A"])
        self.app.var_high_current_only.set(True)
        self.app._on_current_group_changed()
        self.assertEqual(eligible(), ["5 A", "10 A"])
        self.assertIn("10 A 초과", self.app._sequence_output_preview(steps[-1])[0])
        self.app.var_high_current_only.set(False)
        self.app._on_current_group_changed()
        self.app.var_dut_current_group.set(f.SequenceCurrentFilter.DUT_10A_ALL)
        self.app._on_dut_current_group_changed()
        self.assertEqual(eligible(), ["2.999 A", "3000 mA", "-3 A", "3.05 A"])

    def test_stale_dut_is_detected_before_any_calibrator_output(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n2 V"))
        first = self.app.seq_steps[0]
        first.measured_raw = "0.9 V"
        with self.acv_peer() as peer:
            self.connect(peer)
            with patch.object(self.app.dut_session, "check_connection", side_effect=ConnectionResetError("lost")):
                self.app._start_filtered_sequence(0, 1)
                self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertIn("출력 전 DUT 확인 실패", self.app.var_active_row_banner.get())
            self.assertFalse(any(cmd.startswith("OUT ") or cmd == "OPER" for cmd in peer.commands))
            self.assertFalse(self.app.is_operating)
            self.assertEqual(first.measured_raw, "0.9 V")

    def test_dut_loss_during_read_restores_link_only_after_confirmed_standby(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 V\n2 V"))
        session = self.app.dut_session
        identity, profile = session.identity, session.profile
        settings = f.DUTSettings(mode="WT_LAN", address="192.0.2.8")
        session.settings = settings
        recovery_started_after_standby = []
        with self.acv_peer() as peer:
            self.connect(peer)

            def interrupted(*_args, **_kwargs):
                error = ConnectionResetError("DUT disconnected")
                session._failed_io(error)
                raise error

            def recover():
                recovery_started_after_standby.append(self.app.is_operating is False and "STBY" in peer.commands)
                session.recovery_target = None
                session.recovery_pending = False
                session.profile, session.identity, session.settings = profile, identity, settings
                return identity

            with (patch.object(session, "read", side_effect=interrupted),
                  patch.object(session, "recover_connection", side_effect=recover)):
                self.app._start_filtered_sequence(0, 1)
                self.pump_until(lambda: recovery_started_after_standby and not self.app._dut_busy)
            self.assertEqual(recovery_started_after_standby, [True])
            self.assertEqual(self.app.seq_paused_idx, 0)
            self.assertEqual([cmd for cmd in peer.commands if cmd.startswith("OUT ")], ["OUT 1 V, 0 HZ; OPER"])
            self.assertFalse(self.app.seq_running)

    def resistance_dut(self, readings):
        session = self.app.dut_session
        session.identity = "Keysight Technologies,34465A,TEST,A.03"
        session.profile = f.identify_dut(session.identity)
        session.transport = Mock()
        replies = {"FUNC?": '"FRES"', "RES:NULL:STAT?": "1", "RES:NULL:VAL:AUTO?": "1", "RES:NULL:VAL?": "2",
                   "RES:RANG:AUTO?": "0"}
        readings = iter(readings)

        def write(command):
            if command == 'FUNC "RES"':
                replies["FUNC?"] = '"RES"'
            elif command == "RES:RANG:AUTO ON":
                replies["RES:RANG:AUTO?"] = "1"
            elif command.startswith("RES:NULL:STAT "):
                replies["RES:NULL:STAT?"] = "1" if command.endswith(" ON") else "0"
            elif command.startswith("RES:NULL:VAL "):
                replies["RES:NULL:VAL?"] = command.split()[1]
                replies["RES:NULL:VAL:AUTO?"] = "0"

        def query(command):
            return next(readings) if command == "READ?" else replies[command]

        session.transport.write.side_effect = write
        session.transport.query.side_effect = query
        return session.transport

    def test_34465_two_wire_resistance_zeros_once_and_records_both_rows(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("100 OHM\n10 MΩ"))
        transport = self.resistance_dut(("0.125", "100.002", "10000300"))
        with self.acv_peer() as peer:
            self.connect(peer)
            self.app.var_seq_stop_row.set("2")
            self.app._handle_row_direct_start(0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            out_commands = ["OUT 0 OHM; ZCOMP NONE", "OUT 100 OHM; ZCOMP NONE", "OUT 10000000 OHM; ZCOMP NONE"]
            self.assertEqual([cmd for cmd in peer.commands if cmd.startswith("OUT ")], out_commands)
            for command in out_commands:
                start = peer.commands.index(command)
                operate = peer.commands.index("OPER", start)
                self.assertIn("OUT?", peer.commands[start:operate])
                self.assertIn("ZCOMP?", peer.commands[start:operate])
            self.assertIn("STBY", peer.commands[peer.commands.index(out_commands[0]) + 1:peer.commands.index(out_commands[1])])
            self.assertEqual([call.args[0] for call in transport.write.call_args_list if call.args[0].startswith("RES:")],
                             ["RES:RANG:AUTO ON", "RES:NULL:STAT OFF", "RES:NULL:STAT ON", "RES:NULL:VAL 0.125"])
            self.assertEqual([step.measured_raw for step in self.app.seq_steps], ["100.002 OHM", "10000300 OHM"])
            self.assertEqual(self.app.excel_grid._row_widgets[1]["measurement_var"].get(), "10.000300 MΩ")
            self.assertFalse(self.app.is_operating)

    def test_34465_resistance_stops_before_operating_with_compensation_enabled(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("10 MΩ"))
        self.resistance_dut(("0.125",))
        with self.acv_peer(compensation=lambda reply: "WIRE2" if reply.startswith("10000000,") else "NONE") as peer:
            self.connect(peer)
            self.app._handle_row_direct_start(0)
            self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
            self.assertIn("저항 보상 OFF 확인 실패", self.app.var_active_row_banner.get())
            high_command = peer.commands.index("OUT 10000000 OHM; ZCOMP NONE")
            self.assertNotIn("OPER", peer.commands[high_command:])
            self.assertFalse(self.app.is_operating)

    def test_34465_resistance_blocks_nominal_output_after_failed_zero(self):
        for reply, reading, expected in (
            ("0,V,0,0,0", "0.125", "0 Ω 설정 확인 실패"),
            ("0,OHM,0,0,0", "9.9E37", "0 Ω 측정값이 유효하지 않습니다."),
        ):
            with self.subTest(expected=expected):
                self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("100 OHM"))
                transport = self.resistance_dut((reading,))
                with self.acv_peer(output=reply) as peer:
                    self.connect(peer)
                    self.app._handle_row_direct_start(0)
                    self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
                    self.assertIn(expected, self.app.var_active_row_banner.get())
                    self.assertEqual([cmd for cmd in peer.commands if cmd.startswith("OUT ")], ["OUT 0 OHM; ZCOMP NONE"])
                    self.assertFalse(any(call.args[0].startswith("RES:NULL:VAL ") for call in transport.write.call_args_list))
                    self.assertFalse(self.app.is_operating)
                    self.app.conn_mgr.disconnect(standby=False)

    def test_dmm_resistance_records_after_output_with_nominal_unit(self):
        self.set_rows(f.SmartClipboardSequenceParser.build_sequence_steps("1 kOHM"))
        session = self.app.dut_session
        session.identity = "HEWLETT-PACKARD,34401A,TEST,1.00"
        session.profile = f.identify_dut(session.identity)
        sample = self.sample(1, raw_values={"resistance": "1000.01"}, resistance=1000.01)
        with self.acv_peer() as peer:
            self.connect(peer)
            with (patch.object(session, "_dmm_function", return_value="FRES"),
                  patch.object(session, "read", return_value=sample) as read):
                self.app._start_filtered_sequence(0, 0)
                self.pump_until(lambda: not self.app.seq_running and not self.app._io_busy)
                read.assert_called_once_with(1, include_dc=False, fresh=True, dmm_terminal=None,
                                             expected_dmm_function="OHM")
            self.assertEqual(self.app.seq_steps[0].measured_raw, "1000.01 OHM")
            self.assertEqual(self.app.excel_grid._row_widgets[0]["measurement_var"].get(), "1.00001 kOHM")

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

    def test_stop_button_does_not_wait_for_dut_and_rejects_late_reply(self):
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
            self.app.btn_seq_stop.invoke()
            self.pump_until(lambda: "STBY" in peer.commands and not self.app._io_busy, timeout=2)
            self.assertTrue(self.app._dut_busy)
            self.assertFalse(self.app.is_operating)
            self.release_read.set()
            self.pump_until(lambda: not self.app._dut_busy)
            self.assertEqual(row.measured_raw, "0.9 V")
            self.assertIsNone(self.app._seq_dut_pending)
            self.assertEqual(row.status, "정지됨")

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
