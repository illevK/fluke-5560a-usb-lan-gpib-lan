"""Only local peers/simulation are used; no real calibrator output."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f
import test_gui_async as gui
import test_communications as peers


def current_step(value, number=1, unit='A', coil=False, turns=50):
    return f.SmartClipboardSequenceParser.build_Single_step_from_fields(
        number, '-', 'DC', '-', f'{value} {unit}', use_coil=coil, coil_turns=turns)


def power_step(current, number=1, coil=False, turns=50):
    return f.SmartClipboardSequenceParser.build_Single_step_from_fields(
        number, f'120 V / {current} A', '50 Hz', '0.5', f'{60 * current} W',
        use_coil=coil, coil_turns=turns)


class GroupPolicyTests(unittest.TestCase):
    def test_5560a_dual_current_uses_specified_low_voltage_ranges(self):
        profile = f.MODEL_PROFILES["5560A"]
        self.assertEqual(profile.dual_current_command("OUT -2 A, 0 HZ"), "OUT 0.01 V, -2 A, 0 HZ")
        self.assertEqual(profile.dual_current_command("OUT 1.2001e-3 A, 45 HZ"),
                         "OUT 0.033 V, 1.2001e-3 A, 45 HZ; WAVE SINE,SINE")
        self.assertEqual(profile.dual_current_command("OUT 20.5 A, 65 HZ"),
                         "OUT 0.033 V, 20.5 A, 65 HZ; WAVE SINE,SINE")
        for command in ("OUT 0.0012 A, 60 HZ", "OUT 20.5001 A, 60 HZ",
                        "OUT 1 A, 44.9 HZ", "OUT 1 A, 65.1 HZ"):
            self.assertIsNone(profile.dual_current_command(command))
        self.assertIsNone(f.MODEL_PROFILES["5522A"].dual_current_command("OUT 1 A, 0 HZ"))

    def test_requested_order_and_inclusive_three_amp_boundary(self):
        steps=[current_step(v,i+1) for i,v in enumerate((.1,1,3,5,2))]
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s)], [1,2,5])
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s,True)], [3,4])

    def test_physical_terminal_boundary_is_strictly_above_3_1(self):
        values=(2.999,3,3.1,3.1001,-3.1,-5)
        self.assertEqual([f.SequenceCurrentFilter.includes(current_step(v),True,f.SequenceCurrentFilter.POSTS)
                          for v in values], [False,False,False,True,False,True])

    def test_units_signs_and_scientific_notation(self):
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(-3000,unit='mA'),True))
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(100000,unit='uA')))
        step=current_step(1)
        step.scpi_cmd='OUT -3e0 A, 0 HZ'
        self.assertEqual(f.SequenceCurrentFilter.actual_current(step),3)

    def test_power_uses_current_not_watts_and_includes_pf_calculation(self):
        steps=[power_step(v,i+1) for i,v in enumerate((.1,1,3,5,2))]
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s)], [1,2,5])
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s,True)], [3,4])

    def test_coil_current_and_power_use_actual_output(self):
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(100,coil=True)))
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(150,coil=True),True))
        self.assertTrue(f.SequenceCurrentFilter.includes(power_step(100,coil=True)))
        self.assertTrue(f.SequenceCurrentFilter.includes(power_step(150,coil=True),True))

    def test_noncurrent_runs_in_normal_group_only_and_invalid_current_is_rejected(self):
        step=f.SequenceStep(1,'-','DC','-','100 V','DCV','OUT 100 V, 0 HZ','100 V')
        self.assertTrue(f.SequenceCurrentFilter.includes(step))
        self.assertFalse(f.SequenceCurrentFilter.includes(step,True))
        step.func_type='DCI'
        with self.assertRaises(ValueError): f.SequenceCurrentFilter.includes(step)


class GroupGuiTests(unittest.TestCase):
    setUp=gui.AsyncGuiTests.setUp
    tearDown=gui.AsyncGuiTests.tearDown
    pump_until=gui.AsyncGuiTests.pump_until
    connect=gui.AsyncGuiTests.connect

    def test_paste_keeps_out_of_range_rows_and_sequence_only_outputs_valid_rows(self):
        app = self.app
        text = "1 A\n31 A\n120 V / 31 A\n2 A"
        with patch.object(f.messagebox, "showerror") as error:
            app._handle_excel_paste_at_cell(text, 0, 2, replace_all=True)
        error.assert_not_called()
        self.assertEqual([row.nominal_raw for row in app.seq_steps], ["1 A", "31 A", "3720 W", "2 A"])
        self.assertEqual(app.seq_steps[2].vi_cond_raw, "120 V / 31 A")
        for row in app.seq_steps[1:3]:
            self.assertEqual(row.status, "⚠ 출력 범위 초과")
            self.assertEqual(row.scpi_cmd, "")
        app.var_seq_dwell_sec.set(".2")
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            before = list(server.commands)
            app._handle_row_direct_start(1)
            self.assertFalse(app._execute_sequence_step(2))
            self.assertEqual(server.commands, before)
            self.run_group()
            self.assertEqual([cmd for cmd in server.commands if cmd.startswith("OUT ")],
                             ["OUT 1 A, 0 HZ; OPER", "OUT 2 A, 0 HZ; OPER"])
            self.assertFalse(app.is_operating)

    def test_output_groups_intersect_current_group_and_keep_the_current_nominal(self):
        app = self.app
        parser = f.SmartClipboardSequenceParser.build_Single_step_from_fields
        voltage = parser(1, "-", "DC", "-", "10 V")
        dc = current_step(1, 2)
        ac = parser(3, "-", "50 Hz", "-", "100 A", use_coil=True, coil_turns=50)
        unsupported = parser(4, "-", "400 Hz", "-", "1 A")
        power = power_step(2, 5)
        high_power = power_step(5, 6)
        steps = app.seq_steps = [voltage, dc, ac, unsupported, power, high_power]
        app.excel_grid.render_steps(steps)

        def select(group, *, dual=False, high=False):
            app.var_output_group.set(group)
            app.var_dual_current.set(dual)
            app.var_high_current_only.set(high)
            app._on_current_group_changed()
            return [step.step_no for step in steps if app._step_in_current_group(step)]

        groups = f.SequenceCurrentFilter
        self.assertEqual(select(groups.SINGLE_OUTPUT), [1, 2, 3, 4])
        self.assertEqual(select(groups.DUAL_OUTPUT), [5])
        self.assertEqual(select(groups.DUAL_OUTPUT, dual=True), [2, 3, 5])
        self.assertEqual(app._sequence_output_command(ac), ("OUT 0.033 V, 2 A, 50 HZ; WAVE SINE,SINE", True))
        self.assertEqual((ac.func_type, ac.nominal_raw), ("ACI", "100 A"))
        self.assertEqual(select(groups.DUAL_OUTPUT, dual=True, high=True), [6])
        self.assertEqual(select(groups.SINGLE_OUTPUT, dual=True), [1, 4])
        self.assertEqual(app._sequence_output_command(unsupported), (unsupported.scpi_cmd, False))
        self.assertEqual(select(groups.ALL_OUTPUTS, dual=True), [1, 2, 3, 4, 5])
        with self.assertRaisesRegex(ValueError, "싱글 출력 그룹"):
            app._sequence_output_command(unsupported)

    def test_dual_current_requires_readback_before_oper(self):
        app = self.app
        app.seq_steps = [current_step(1)]
        app.excel_grid.render_steps(app.seq_steps)
        app.var_dual_current.set(True)
        app._on_current_group_changed()
        operating = False
        actual = "0.010000,V,1.00000,A,0E+00"

        def reply(conn, command):
            nonlocal operating
            if command == "OPER":
                operating = True
            elif command == "STBY":
                operating = False
            response = {"*IDN?": peers.IDN, "COMM_MODE? TELNET": "COMP", "*OPC?": "1",
                        "OPER?": str(int(operating)), "ERR?": '0,"No error"', "OUT?": actual}.get(command)
            if response is not None:
                conn.sendall((response + "\r\n").encode("ascii"))

        with peers.FakeInstrument(handler=reply) as server:
            self.connect(server)
            self.assertTrue(app._execute_sequence_step(0))
            self.pump_until(lambda: not app._io_busy)
            index = server.commands.index("OUT 0.01 V, 1 A, 0 HZ")
            self.assertEqual(server.commands[index - 2:index + 5],
                             ["OPER?", "ERR?", "OUT 0.01 V, 1 A, 0 HZ", "*OPC?", "ERR?", "OUT?", "OPER"])
            self.assertTrue(app.is_operating)
            actual = "1.00000,A,0E+00,0,0E+00"
            before = len(server.commands)
            self.assertTrue(app._execute_sequence_step(0))
            self.pump_until(lambda: not app._io_busy)
            attempted = server.commands[before:]
            self.assertLess(attempted.index("STBY"), attempted.index("OUT 0.01 V, 1 A, 0 HZ"))
            self.assertNotIn("OPER", attempted)
            self.assertFalse(operating)
            self.assertIn("설정 확인 실패", app.var_active_row_banner.get())

    def rows(self,power=False):
        self.app.var_current_group_basis.set(f.SequenceCurrentFilter.JOB)
        self.app._on_current_group_changed()
        maker=power_step if power else current_step
        self.app.seq_steps=[maker(v,i+1) for i,v in enumerate((.1,1,3,5,2))]
        self.app.excel_grid.render_steps(self.app.seq_steps)
        self.app.var_seq_start_row.set('1')
        self.app.var_seq_stop_row.set('5')
        self.app.var_seq_dwell_sec.set('.2')
        self.app._refresh_current_group_preview()

    def run_group(self):
        self.app._on_seq_start_from_selected_or_range()
        self.pump_until(lambda:not self.app._io_busy and not self.app.seq_running)

    def switch_high(self):
        self.app.var_high_current_only.set(True)
        self.app._on_current_group_changed()
        self.pump_until(lambda:not self.app._io_busy)

    def test_low_then_high_current_runs_once_each_and_switch_starts_at_top(self):
        self.rows()
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            self.run_group()
            expected=[self.app.seq_steps[i].scpi_cmd+'; OPER' for i in (0,1,4)]
            self.assertEqual([c for c in server.commands if c.startswith('OUT ')],expected)
            self.assertFalse(self.app.is_operating)
            before=len(server.commands)
            self.switch_high()
            self.assertIn('STBY',server.commands[before:])
            self.assertEqual([c for c in server.commands if c.startswith('OUT ')],expected)
            self.assertEqual(self.app.var_seq_start_row.get(),'1')
            self.assertEqual(self.app.seq_paused_idx,-1)
            self.run_group()
            expected += [self.app.seq_steps[i].scpi_cmd+'; OPER' for i in (2,3)]
            self.assertEqual([c for c in server.commands if c.startswith('OUT ')],expected)
            self.assertFalse(self.app.is_operating)
            self.assertEqual(self.app.progress_var.get(),100)

    def test_same_filter_controls_power_rows(self):
        self.rows(power=True)
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            self.run_group()
            self.switch_high()
            self.run_group()
            self.assertEqual([c for c in server.commands if c.startswith('OUT ')],
                             [self.app.seq_steps[i].scpi_cmd+'; OPER' for i in (0,1,4,2,3)])

    def test_excluded_direct_row_is_blocked_and_next_prev_skip_it(self):
        self.rows()
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            self.assertFalse(self.app._execute_sequence_step(2))
            self.assertFalse(any(c.startswith('OUT ') for c in server.commands))
            self.app.seq_current_idx=1
            self.app._on_seq_step_next()
            self.pump_until(lambda:not self.app._io_busy)
            self.assertEqual(self.app.seq_current_idx,4)
            self.app._on_seq_step_prev()
            self.pump_until(lambda:not self.app._io_busy)
            self.assertEqual(self.app.seq_current_idx,1)

    def test_empty_range_does_not_send_commands(self):
        self.rows()
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            self.app.var_seq_start_row.set('3')
            self.app.var_seq_stop_row.set('4')
            before=list(server.commands)
            self.run_group()
            self.assertEqual(server.commands,before)
            self.assertIn('대상이 없습니다',self.app.var_active_row_banner.get())

    def test_group_change_during_pending_or_running_is_rejected(self):
        self.rows()
        for busy,running in ((True,False),(False,True)):
            self.app._io_busy=busy
            self.app.seq_running=running
            self.app.var_high_current_only.set(True)
            self.app._on_current_group_changed()
            self.assertFalse(self.app._group_high_only)
            self.assertFalse(self.app.var_high_current_only.get())
        self.app._io_busy=False
        self.app.seq_running=False

    def test_switch_does_not_commit_if_standby_fails(self):
        self.rows()
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            with patch.object(self.app.conn_mgr,'send_command',side_effect=TimeoutError('STBY 확인 실패')):
                self.switch_high()
            self.assertFalse(self.app._group_high_only)
            self.assertFalse(self.app.var_high_current_only.get())
            self.assertIsNone(self.app.is_operating)

    def test_excluded_breakpoint_does_not_interrupt_remaining_normal_rows(self):
        self.rows()
        self.app.seq_steps[2].breakpoint=True
        with peers.SlowOutputTests().peer() as server:
            self.connect(server)
            self.run_group()
            self.assertIn(self.app.seq_steps[4].scpi_cmd+'; OPER',server.commands)
            self.assertFalse(self.app.is_operating)


if __name__=='__main__':
    unittest.main(verbosity=2)
