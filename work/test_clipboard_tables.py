"""Exact clipboard layouts and the measurement table paths they use."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


def power_factor_table():
    return '\n'.join(
        f'{f"{volts} V / 1 A" if index == 0 else ""}\t\t\t\t\t{hz} Hz\t{volts} V\t1 A\t{nominal} '
        for hz in (50, 60) for volts in (120, 240)
        for index, nominal in enumerate(('1', '0.5', '0.5'))
    )


def power_table():
    points = (
        (120, '4 mA', '480 mW', '240 W'), (120, '10 mA', '1.2 W', '0.6 W'),
        (120, '50 mA', '6 W', '3 W'), (120, '200 mA', '24 W', '12 W'),
        (120, '1 A', '120 W', '60 W'), (120, '5 A', '0.6 kW', '300 W'),
        (120, '20 A', '2.4 kW', '1.2 kW'), (240, '2 mA', '0.48 W', '0.24 W'),
        (240, '10 mA', '2.4 W', '1.2 W'), (240, '50 mA', '12 W', '6 W'),
        (240, '200 mA', '48 W', '24 W'), (240, '1 A', '240 W', '120 W'),
        (240, '5 A', '1.2 kW', '0.6 kW'), (240, '20 A', '4.8 kW', '2.4 kW'),
    )
    return '\n'.join(
        f'{volts} V / {amps}\t\t\t\t\t{hz} Hz\t{pf}\t{volts} V\t{unity if pf == "1" else half}'
        for hz in (50, 60) for pf in ('1', '0.5', '-0.5')
        for volts, amps, unity, half in points
    )


class ClipboardTableTests(unittest.TestCase):
    def setUp(self):
        with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.addCleanup(self.app._on_close)
        self.grid = self.app.excel_grid

    def test_empty_sheet_keeps_two_tabs_and_accepts_first_paste(self):
        app = self.app
        self.assertEqual([app.notebook.tab(tab, 'text') for tab in app.notebook.tabs()], ['측정표', 'DUT 연결'])
        self.assertFalse(app.seq_steps)
        self.assertTrue(app.bind('<<Paste>>'))
        self.assertTrue(app.chk_dual_current.winfo_manager())
        with patch.object(app, 'clipboard_get', return_value='1 A\n31 A\n60 Hz\t2 A'):
            self.assertEqual(app._paste_into_empty_sheet(SimpleNamespace(widget=self.grid)), 'break')
        self.assertEqual([row.nominal_raw for row in app.seq_steps], ['1 A', '31 A', '2 A'])
        self.assertEqual(app.seq_steps[1].func_type, 'OUT_OF_RANGE')
        app.chk_dual_current.invoke()
        self.assertEqual(app._sequence_output_command(app.seq_steps[0]), ('OUT 0.01 V, 1 A, 0 HZ', True))
        self.assertEqual(app._sequence_output_command(app.seq_steps[2]),
                         ('OUT 0.033 V, 2 A, 60 HZ; WAVE SINE,SINE', True))
        app._on_clear_sheet()
        self.assertFalse(app.seq_steps)
        app._on_add_blank_row()
        self.assertEqual(app.seq_steps[0].nominal_raw, '')
        self.assertEqual(app.seq_steps[0].scpi_cmd, '')
        self.assertFalse(app._step_in_current_group(app.seq_steps[0]))
        app._handle_excel_cell_edited(0, '', 'DC', '', '1 V')
        self.assertEqual(app.seq_steps[0].scpi_cmd, 'OUT 1 V, 0 HZ')

    def test_dual_voltage_edits_must_be_applied_and_update_the_preview(self):
        app = self.app
        app._handle_excel_paste_at_cell('1 A\n60 Hz\t2 A', 0, 2, replace_all=True)
        app.chk_dual_current.invoke()
        app.var_dual_dc_voltage.set('250 mV')
        app.var_dual_ac_voltage.set('20 mV')
        app._on_dual_current_voltages_applied()
        self.assertIn('0.033', app.var_active_row_banner.get())
        self.assertEqual((app._dual_dc_voltage, app._dual_ac_voltage), (0.01, 0.033))
        with self.assertRaisesRegex(ValueError, '전압 적용'):
            app._sequence_output_command(app.seq_steps[0])
        app.var_dual_ac_voltage.set('1.25')
        app._on_dual_current_voltages_applied()
        self.assertIn('0.25 V DC', app._sequence_output_preview(app.seq_steps[0])[0])
        self.assertEqual(app._sequence_output_command(app.seq_steps[0]), ('OUT 0.25 V, 1 A, 0 HZ', True))
        self.assertEqual(app._sequence_output_command(app.seq_steps[1]),
                         ('OUT 1.25 V, 2 A, 60 HZ; WAVE SINE,SINE', True))

    def test_toolbar_paste_preserves_all_pf_points_and_reads_rightmost_value(self):
        with patch.object(self.app, 'clipboard_get', return_value=power_factor_table()):
            self.app._on_paste_into_sheet_button(replace_all=True)
        rows = self.app.seq_steps
        self.assertEqual(len(rows), 12)
        for i, row in enumerate(rows):
            hz = 50 if i < 6 else 60
            volts = 120 if i // 3 % 2 == 0 else 240
            pf = '1 LEAD' if i % 3 == 0 else '0.5 LAG'
            self.assertEqual((row.func_type, row.nominal_raw), ('PF', pf))
            self.assertEqual(row.scpi_cmd, f'OUT {volts} V, 1 A, {hz} HZ; DPF {pf.replace(" ", ", ")}')
        self.assertNotIn('분해능', [spec[0] for spec in self.grid.COL_SPECS])

    def test_cell_paste_power_pf_sign_units_and_no_command_on_mismatch(self):
        self.app._handle_excel_paste_at_cell(power_table(), 0, 2)
        rows = self.app.seq_steps
        self.assertEqual(len(rows), 84)
        self.assertEqual([i for i, row in enumerate(rows) if not row.scpi_cmd], [14, 28, 56, 70])
        self.assertEqual(rows[0].scpi_cmd, 'OUT 120 V, 0.004 A, 50 HZ; DPF 1, LEAD')
        self.assertEqual(rows[19].scpi_cmd, 'OUT 120 V, 5 A, 50 HZ; DPF 0.5, LAG')
        self.assertEqual(rows[33].scpi_cmd, 'OUT 120 V, 5 A, 50 HZ; DPF 0.5, LEAD')
        self.assertEqual(rows[55].nominal_raw, '4.8 kW')
        self.assertEqual(rows[55].scpi_cmd, 'OUT 240 V, 20 A, 60 HZ; DPF 1, LEAD')
        with patch.object(self.app, '_show_report') as report, patch.object(self.app.conn_mgr, 'send_command') as send:
            self.app._start_filtered_sequence(14, 14)
            send.assert_not_called()
            self.assertIn('0.24 W', report.call_args.args[1])
        row = rows[14]
        self.app._handle_excel_cell_edited(14, row.vi_cond_raw, row.freq_cond_raw, row.pf_cond_raw, '240 mW')
        self.assertEqual(rows[14].scpi_cmd, 'OUT 120 V, 0.004 A, 50 HZ; DPF 0.5, LAG')
        self.assertEqual(rows[14].status, '대기')

    def test_append_tables_keeps_prior_results_and_start_all_uses_full_range(self):
        with patch.object(self.app, 'clipboard_get', return_value='50 Hz\t1 V\n60 Hz\t2 V'):
            self.app._on_paste_into_sheet_button(replace_all=True)
        first = self.app.seq_steps[0]
        first.breakpoint = True
        self.grid._row_widgets[0]['measurement_var'].set('0.9998')
        self.grid.selected_row_idx = 0
        self.grid.selected_col_idx = self.grid.MEASUREMENT_COLUMN
        self.app.var_seq_start_row.set('2')
        for text in ('주파수\t명목값\n60 Hz\t1 A', '120 V / 1 A\t60 Hz\t1\t120 W'):
            with patch.object(self.app, 'clipboard_get', return_value=text):
                self.app._on_paste_into_sheet_button(append=True)
        rows = self.app.seq_steps
        self.assertEqual([row.nominal_raw for row in rows], ['1 V', '2 V', '1 A', '120 W'])
        self.assertEqual([row.func_type for row in rows], ['ACV', 'ACV', 'ACI', 'ACP'])
        self.assertEqual([row.step_no for row in rows], [1, 2, 3, 4])
        self.assertIs(rows[0], first)
        self.assertTrue(first.breakpoint)
        self.assertEqual(first.measured_raw, '0.9998 V')
        self.assertEqual((self.app.var_seq_start_row.get(), self.app.var_seq_stop_row.get()), ('1', '4'))
        self.app.var_seq_start_row.set('3')
        self.app.var_seq_stop_row.set('3')
        with patch.object(self.app, '_start_filtered_sequence') as run:
            self.app._on_seq_start_from_selected_or_range(all_rows=True)
            run.assert_called_once_with(0, 3)

    def test_append_unrecognized_row_is_atomic_and_does_not_insert_dummy_output(self):
        self.app._handle_excel_paste_at_cell('1 V', 0, 2)
        with patch.object(self.app, 'clipboard_get', return_value='50 Hz\t1 V\nbroken value'), \
             patch.object(f.messagebox, 'showerror') as error:
            self.app._on_paste_into_sheet_button(append=True)
        error.assert_called_once()
        self.assertEqual([row.nominal_raw for row in self.app.seq_steps], ['1 V'])

    def test_append_uses_actual_coil_current_and_accepts_correction(self):
        app = self.app
        app._handle_excel_paste_at_cell('1 V', 0, 2)
        app.var_seq_use_coil.set(True)
        with patch.object(app, '_get_coil_turns', return_value=50):
            app._handle_excel_paste_at_cell('1500 A\n1550 A\n2 kV', 0, 2, append=True)
            self.assertEqual([row.nominal_raw for row in app.seq_steps], ['1 V', '1500 A', '1550 A', '2 kV'])
            self.assertEqual(app.seq_steps[1].scpi_cmd, 'OUT 30 A, 0 HZ')
            self.assertEqual([row.func_type for row in app.seq_steps[2:]], ['OUT_OF_RANGE', 'OUT_OF_RANGE'])
            app._handle_excel_cell_edited(2, '-', 'DC', '-', '100 A')
            self.assertEqual(app.seq_steps[2].scpi_cmd, 'OUT 2 A, 0 HZ')
            self.assertEqual(app.seq_steps[2].status, '대기')

    def test_missing_last_cell_never_becomes_current_and_failed_replace_is_atomic(self):
        self.app._handle_excel_paste_at_cell('1 V', 0, 2)
        malformed = '120 V / 1 A\t\t50 Hz\t120 V\t1 A\t'
        with patch.object(self.app, 'clipboard_get', return_value=power_factor_table() + '\n' + malformed), \
             patch.object(f.messagebox, 'showerror') as error:
            self.app._on_paste_into_sheet_button(replace_all=True)
        error.assert_called_once()
        self.assertEqual([row.nominal_raw for row in self.app.seq_steps], ['1 V'])

    def test_rightmost_target_and_existing_dc_vi_edit(self):
        self.app._handle_excel_paste_at_cell('조건\t명목값\n10 V\t0.001 V', 0, 2)
        self.assertEqual(self.app.seq_steps[0].scpi_cmd, 'OUT 0.001 V, 0 HZ')
        self.app._handle_excel_cell_edited(0, '120 V / 1 A', 'DC', '-', '0.001 V')
        self.assertEqual(self.app.seq_steps[0].scpi_cmd, 'OUT 120 V, 1 A, 0 HZ')
        self.app._handle_excel_cell_edited(0, '0 V / 1 A', 'DC', '1', '120 W')
        self.assertEqual(self.app.seq_steps[0].scpi_cmd, '')
