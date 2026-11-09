"""DC V/I and resolution grid integration; no devices or real clipboard."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


DC_POINTS = '120 V / 1 A\n120 V / 5 A\n120 V / 10 A\n240 V / 1 A\n240 V / 5 A\n240 V / 10 A'
RESOLUTIONS = '0.01 W\n0.01 W\n0.0001 kW\n0.01 W\n0.0001 kW\n0.0001 kW'


class ResolutionGridTests(unittest.TestCase):
    def setUp(self):
        with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]), patch.object(f, 'SAMPLE_POWER_TABLE', '1 V'):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.grid = self.app.excel_grid
        self.app.conn_mgr.send_command = Mock(side_effect=AssertionError('Editing must not send device commands'))
        self.grid.on_resolution_error_callback = Mock()
        self.clipboard = []
        self.grid.clipboard_clear = Mock()
        self.grid.clipboard_append = self.clipboard.append
        self.grid.bell = Mock()

    def tearDown(self):
        try:
            self.app.conn_mgr.send_command.assert_not_called()
        finally:
            self.app._on_close()

    def paste_points(self):
        self.app._handle_excel_paste_at_cell(DC_POINTS, 0, 2)

    def paste_resolutions(self):
        self.app._handle_excel_paste_at_cell(RESOLUTIONS, 0, 10)

    def enter_measurement(self, row, text):
        self.grid._row_widgets[row]['measurement_var'].set(text)
        self.grid._commit_cell(row, 9)

    def test_six_user_dc_points_and_resolution_column_paste(self):
        self.paste_points()
        self.assertEqual([step.nominal_raw for step in self.app.seq_steps],
                         ['120 W', '600 W', '1200 W', '240 W', '1200 W', '2400 W'])
        self.assertEqual([step.func_type for step in self.app.seq_steps], ['DCP'] * 6)
        self.assertEqual([step.freq_cond_raw for step in self.app.seq_steps], ['DC'] * 6)
        self.assertEqual([step.scpi_cmd for step in self.app.seq_steps],
                         ['OUT 120 V, 1 A, 0 HZ', 'OUT 120 V, 5 A, 0 HZ', 'OUT 120 V, 10 A, 0 HZ',
                          'OUT 240 V, 1 A, 0 HZ', 'OUT 240 V, 5 A, 0 HZ', 'OUT 240 V, 10 A, 0 HZ'])
        before = [step.scpi_cmd for step in self.app.seq_steps]
        self.paste_resolutions()
        self.assertEqual([step.resolution_raw for step in self.app.seq_steps], RESOLUTIONS.splitlines())
        self.assertEqual([step.scpi_cmd for step in self.app.seq_steps], before)
        self.assertEqual([widgets['resolution_var'].get() for widgets in self.grid._row_widgets], RESOLUTIONS.splitlines())
        self.grid.on_resolution_error_callback.assert_not_called()

    def test_paired_target_resolution_columns_import_without_target_confusion(self):
        self.app._handle_excel_paste_at_cell('120 V / 1 A\t0.01 W\n10 V\t0.001 V', 0, 2)
        self.assertEqual(len(self.app.seq_steps), 2)
        power, volts = self.app.seq_steps
        self.assertEqual((power.nominal_raw, power.func_type, power.resolution_raw), ('120 W', 'DCP', '0.01 W'))
        self.assertEqual((volts.nominal_raw, volts.func_type, volts.resolution_raw), ('10 V', 'DCV', '0.001 V'))
        self.assertEqual(volts.scpi_cmd, 'OUT 10 V, 0 HZ')

    def test_commit_rounds_display_and_header_range_copies_but_retains_raw(self):
        self.paste_points()
        self.paste_resolutions()
        raw = ['120.004 W', '600.005 W', '1200.06 W', '240.004 W', '1200.06 W', '2400.04 W']
        display = ['120.00 W', '600.01 W', '1.2001 kW', '240.00 W', '1.2001 kW', '2.4000 kW']
        for row, text in enumerate(raw):
            self.enter_measurement(row, text)
        self.assertEqual([step.measured_raw for step in self.app.seq_steps], raw)
        self.assertEqual([widgets['measurement_var'].get() for widgets in self.grid._row_widgets], display)
        self.grid._copy_all_measurements()
        self.assertEqual(self.clipboard[-1], '\r\n'.join(display) + '\r\n')
        self.grid._set_measurement_selection(2, 1)
        self.grid._copy_measurements(1, force_cells=True)
        self.assertEqual(self.clipboard[-1], '600.01 W\r\n1.2001 kW\r\n')
        self.assertEqual([step.measured_raw for step in self.app.seq_steps], raw)

    def test_resolution_changes_reround_original_and_clear_restores_input(self):
        self.app._handle_excel_paste_at_cell('120 V / 1 A\t0.0001 kW', 0, 2)
        self.enter_measurement(0, '1234.56 W')
        widgets, step = self.grid._row_widgets[0], self.app.seq_steps[0]
        self.assertEqual(widgets['measurement_var'].get(), '1.2346 kW')
        widgets['resolution_var'].set('0.001 kW')
        self.grid._commit_cell(0, 10)
        self.assertEqual(widgets['measurement_var'].get(), '1.235 kW')
        self.assertEqual(step.measured_raw, '1234.56 W')
        widgets['resolution_var'].set('0.01 W')
        self.grid._commit_cell(0, 10)
        self.assertEqual(widgets['measurement_var'].get(), '1234.56 W')
        widgets['resolution_var'].set('')
        self.grid._commit_cell(0, 10)
        self.assertEqual(widgets['measurement_var'].get(), '1234.56 W')
        self.grid._copy_all_measurements()
        self.assertTrue(self.clipboard[-1].startswith('1234.56 W\r\n'))

    def test_model_and_coil_rebuild_preserve_resolution_and_raw_readings(self):
        self.paste_points()
        self.paste_resolutions()
        self.enter_measurement(0, '120.004 W')
        before = [(step.resolution_raw, step.measured_raw) for step in self.app.seq_steps]
        self.app.var_model.set('5522A')
        self.app._on_model_changed()
        self.assertEqual([(step.resolution_raw, step.measured_raw) for step in self.app.seq_steps], before)
        self.app.var_seq_use_coil.set(True)
        self.app.var_seq_coil_turns.set('50')
        self.app._recalculate_all_grid_steps()
        self.assertEqual([(step.resolution_raw, step.measured_raw) for step in self.app.seq_steps], before)
        self.assertEqual(self.grid._row_widgets[0]['measurement_var'].get(), '120.00 W')
        self.assertIn('OUT 120 V, 0.02 A, 0 HZ', self.app.seq_steps[0].scpi_cmd)

    def test_invalid_resolution_paste_is_atomic_and_reports_error(self):
        self.paste_points()
        self.paste_resolutions()
        before = [step.resolution_raw for step in self.app.seq_steps]
        self.app._handle_excel_paste_at_cell('0.001 W\n0.001 V', 0, 10)
        self.assertEqual([step.resolution_raw for step in self.app.seq_steps], before)
        self.grid.on_resolution_error_callback.assert_called_once()
        self.assertIn('단위', self.grid.on_resolution_error_callback.call_args.args[0])
        self.grid.on_resolution_error_callback.reset_mock()
        self.app._handle_excel_paste_at_cell('0 W', 0, 10)
        self.assertEqual([step.resolution_raw for step in self.app.seq_steps], before)
        self.grid.on_resolution_error_callback.assert_called_once()

    def test_resolution_enter_moves_down_and_dimension_error_keeps_clipboard(self):
        self.paste_points()
        next_entry = self.grid._row_widgets[1]['entries'][10]
        self.grid._row_widgets[0]['resolution_var'].set('0.01 W')
        with patch.object(next_entry, 'focus_set') as focus, patch.object(self.grid, 'on_cell_edited_callback') as edit:
            self.assertEqual(self.grid._commit_and_move(0, 10, 1, 0), 'break')
            focus.assert_called_once()
            edit.assert_not_called()
        self.enter_measurement(0, '120 W')
        self.grid._row_widgets[0]['resolution_var'].set('0.01 A')
        self.assertTrue(self.grid._row_widgets[0]['resolution_error'])
        self.grid._copy_all_measurements()
        self.grid.clipboard_clear.assert_not_called()
        self.assertEqual(self.clipboard, [])
        self.grid.on_resolution_error_callback.assert_called_once()

    def test_pf_and_si_voltage_resolution_rules(self):
        self.app._handle_excel_paste_at_cell('Lead\t308001\t\t60 Hz\t120 V\t10 A\t0.5\n1 kV', 0, 2)
        self.assertEqual([step.func_type for step in self.app.seq_steps], ['PF', 'DCV'])
        self.app._handle_excel_paste_at_cell('0.001\n1 V', 0, 10)
        self.enter_measurement(0, '0.4995')
        self.enter_measurement(1, '999.5 V')
        self.assertEqual(self.grid._row_widgets[0]['measurement_var'].get(), '0.500')
        self.assertEqual(self.grid._row_widgets[1]['measurement_var'].get(), '1000 V')
        self.assertEqual(self.app.seq_steps[1].scpi_cmd, 'OUT 1000 V, 0 HZ')
        self.grid._copy_all_measurements()
        self.assertEqual(self.clipboard[-1], '0.500\r\n1000 V\r\n')

    def test_manual_vi_edit_infers_dc_power_and_invalid_zero_voltage_clears_command(self):
        self.assertEqual(self.app.seq_steps[0].func_type, 'DCV')
        self.app._handle_excel_cell_edited(0, '120 V / 1 A', 'DC', '-', '1 V')
        step = self.app.seq_steps[0]
        self.assertEqual((step.func_type, step.nominal_raw), ('DCP', '120 W'))
        self.assertEqual(step.scpi_cmd, 'OUT 120 V, 1 A, 0 HZ')
        self.app._handle_excel_cell_edited(0, '0 V / 1 A', 'DC', step.pf_cond_raw, step.nominal_raw)
        invalid = self.app.seq_steps[0]
        self.assertEqual(invalid.func_type, 'ERROR')
        self.assertEqual(invalid.scpi_cmd, '')
        self.assertIn('전압', invalid.actual_out_desc)

    def test_invalid_resolution_with_blank_measurement_blocks_header_and_range_copy(self):
        self.assertEqual(self.app.seq_steps[0].measured_raw, '')
        self.grid._row_widgets[0]['resolution_var'].set('0 W')
        self.assertTrue(self.grid._row_widgets[0]['resolution_error'])
        self.clipboard[:] = ['existing clipboard']
        self.assertEqual(self.grid._copy_all_measurements(), 'break')
        self.grid._set_measurement_selection(0, 0)
        self.assertEqual(self.grid._copy_measurements(0, force_cells=True), 'break')
        self.assertEqual(self.clipboard, ['existing clipboard'])
        self.grid.clipboard_clear.assert_not_called()
        self.assertEqual(self.grid.on_resolution_error_callback.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
