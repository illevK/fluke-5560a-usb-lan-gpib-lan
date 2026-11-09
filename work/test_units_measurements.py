"""SI conversion and result-entry regressions. No physical instrument or desktop input."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


class UnitTests(unittest.TestCase):
    def step(self, nominal, freq='DC', vi='-', model='5560A', **kwargs):
        return f.SmartClipboardSequenceParser.build_Single_step_from_fields(1, vi, freq, '1', nominal, model=model, **kwargs)

    def test_all_prefixes_and_dimensions(self):
        factors = [('p', 1e-12), ('n', 1e-9), ('u', 1e-6), ('m', .001), ('', 1), ('k', 1000), ('M', 1e6), ('G', 1e9), ('T', 1e12)]
        for base in ('V', 'A', 'W', 'OHM', 'HZ'):
            for prefix, factor in factors:
                with self.subTest(base=base, prefix=prefix):
                    text = f'{1 / factor:.12g} {prefix}{base}'
                    parsed = f.SmartClipboardSequenceParser.parse_value_and_unit(text)
                    self.assertIsNotNone(parsed)
                    self.assertAlmostEqual(f.SIUnits.to_base(*parsed, base), 1)
                    if base == 'HZ':
                        step = self.step('1 V', freq=text)
                    elif base == 'W':
                        step = self.step(text, vi='1 V / 1 A')
                    else:
                        step = self.step(text)
                    values = f.MODEL_PROFILES['5560A'].output_values(step.scpi_cmd.split(';')[0])
                    self.assertAlmostEqual(values['A' if base == 'W' else base], 1)

    def test_kilovolt_output_on_every_model_and_large_values_rejected(self):
        for model in f.MODEL_PROFILES:
            self.assertEqual(self.step('1 kV', model=model).scpi_cmd, 'OUT 1000 V, 0 HZ')
            with self.assertRaises(ValueError): self.step('1.1 kV', model=model)
            with self.assertRaises(ValueError): self.step('1 MV', model=model)
            with self.assertRaises(ValueError): self.step('1 TA', model=model)
            with self.assertRaises(ValueError): self.step('1 TOHM', model=model)

    def test_case_micro_symbols_numbers_and_unicode(self):
        cases = {'1 mV': .001, '1 MV': 1e6, '1 μV': 1e-6, '1 µV': 1e-6,
                 '1 uV': 1e-6, '1\u00a0kV': 1000, '−.5 kV': -500,
                 '1,000 mV': 1, '1e-3 kV': 1, '1. kV': 1000, '1 밀리V': .001}
        for text, expected in cases.items():
            with self.subTest(text=text):
                value, unit = f.SmartClipboardSequenceParser.parse_value_and_unit(text)
                self.assertAlmostEqual(f.SIUnits.to_base(value, unit, 'V'), expected)
        self.assertEqual(f.SIUnits.to_base(1, 'mΩ', 'OHM'), .001)
        self.assertEqual(f.SIUnits.to_base(1, 'MΩ', 'OHM'), 1e6)
        for invalid in ('nan V', 'inf V', '1e999 V', '1,00 V', '1 qV', '1 mmV'):
            self.assertIsNone(f.SmartClipboardSequenceParser.parse_value_and_unit(invalid))

    def test_spreadsheet_tokens_power_frequency_and_coil(self):
        for text, command in [('1 kV', 'OUT 1000 V, 0 HZ'), ('1e3 mV', 'OUT 1 V, 0 HZ'), ('−.5 V', 'OUT -0.5 V, 0 HZ')]:
            rows = f.SmartClipboardSequenceParser.build_sequence_steps(text)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].scpi_cmd, command)
        step = self.step('1 kW', '50 kHz', '1 kV / 1 A')
        self.assertIn('OUT 1000 V, 1 A, 50000 HZ', step.scpi_cmd)
        coil = self.step('1 kA', '50 Hz', use_coil=True, coil_turns=50)
        self.assertIn('OUT 20 A', coil.scpi_cmd)
        self.assertEqual(self.step('1 nA').scpi_cmd, 'OUT 1e-09 A, 0 HZ')
        with self.assertRaises(ValueError): self.step('1 V', '-1 Hz')


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.adapters = patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[])
        self.adapters.start()
        # Each test replaces the sample; skip building 63 unused example rows.
        with patch.object(f, 'SAMPLE_POWER_TABLE', '1 V'):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.app.seq_steps = [f.SmartClipboardSequenceParser.build_Single_step_from_fields(i, '-', 'DC', '-', f'{i} V') for i in (1, 2, 3)]
        self.grid = self.app.excel_grid
        self.grid.render_steps(self.app.seq_steps)
        self.app.conn_mgr.send_command = Mock(side_effect=AssertionError('Result entry must not send commands'))

    def tearDown(self):
        self.app._on_close()
        self.adapters.stop()

    def enter(self, row, value):
        ent = self.grid._row_widgets[row]['entries'][9]
        ent.delete(0, 'end')
        ent.insert(0, value)
        return ent

    def test_records_survive_visibility_rerender_and_recalculation(self):
        self.enter(0, '0.9998 V')
        self.enter(1, '1.9997')
        commands = [s.scpi_cmd for s in self.app.seq_steps]
        for visibility in (False, True, False, True):
            self.grid.set_measurement_visibility(visibility)
            self.grid.set_command_visibility(not visibility)
            self.grid.render_steps(self.app.seq_steps, active_idx=1)
            self.assertEqual(bool(self.grid._row_widgets[0]['frames'][9].winfo_manager()), visibility)
        self.app._recalculate_all_grid_steps()
        self.assertEqual([s.measured_raw for s in self.app.seq_steps], ['0.9998 V', '1.9997', ''])
        self.assertEqual([s.scpi_cmd for s in self.app.seq_steps], commands)
        self.app.conn_mgr.send_command.assert_not_called()

    def test_enter_down_commits_only_results_and_never_starts_output(self):
        entry = self.enter(0, '0.9999')
        next_entry = self.grid._row_widgets[1]['entries'][9]
        with patch.object(next_entry, 'focus_set') as focus, patch.object(self.grid, 'on_cell_edited_callback') as edit:
            self.assertEqual(self.grid._commit_and_move(0, 9, 1, 0), 'break')
            focus.assert_called_once()
            edit.assert_not_called()
        self.assertEqual(self.app.seq_steps[0].measured_raw, '0.9999')
        self.assertTrue(entry.bind('<Return>'))
        self.assertEqual(self.grid._commit_and_move(2, 9, 1, 0), 'break')
        self.assertEqual(len(self.app.seq_steps), 3)
        self.app.conn_mgr.send_command.assert_not_called()

    def test_live_edit_and_result_paste_preserve_output_rows(self):
        commands = [s.scpi_cmd for s in self.app.seq_steps]
        self.app._io_busy = self.app.seq_running = True
        try:
            self.enter(0, '0.9')
            self.grid._update_single_row_visual(0, self.app.seq_steps[0], True)
            self.assertEqual(self.app.seq_steps[0].measured_raw, '0.9')
            self.app._handle_excel_paste_at_cell('1 kV\n\n3.0\n', 0, 9)
            self.assertEqual([s.measured_raw for s in self.app.seq_steps], ['1 kV', '', '3.0'])
            self.assertEqual([s.scpi_cmd for s in self.app.seq_steps], commands)
            self.assertFalse(self.grid._paste_measurements('1\t2', 0))
            self.assertEqual(self.app.seq_steps[0].measured_raw, '1 kV')
        finally:
            self.app._io_busy = self.app.seq_running = False

    def test_goal_edits_keep_result_but_invalid_unit_removes_command(self):
        self.enter(0, '0.999')
        self.app._handle_excel_cell_edited(0, '-', 'DC', '-', '1 kV')
        self.assertEqual(self.app.seq_steps[0].scpi_cmd, 'OUT 1000 V, 0 HZ')
        self.app._handle_excel_cell_edited(0, '-', 'DC', '-', '1 MV')
        self.assertEqual(self.app.seq_steps[0].measured_raw, '0.999')
        self.assertEqual(self.app.seq_steps[0].scpi_cmd, '')
        self.assertEqual(self.app.seq_steps[0].func_type, 'ERROR')
        self.app._handle_excel_cell_edited(0, '-', 'DC', '-', '1 mV')
        self.assertEqual(self.app.seq_steps[0].scpi_cmd, 'OUT 0.001 V, 0 HZ')
        self.assertEqual(self.app.seq_steps[0].measured_raw, '0.999')

    def test_pf_table_paste_then_edit_direction_and_result(self):
        self.app._handle_excel_paste_at_cell(PowerFactorTests.SAMPLE, 0, 2)
        self.assertEqual(len(self.app.seq_steps), 15)
        self.enter(1, '0.4998')
        self.app._handle_excel_cell_edited(1, '120 V / 10 A', '60 Hz', '0.5 Lag', '0.5 LEAD')
        self.assertEqual(self.app.seq_steps[1].nominal_raw, '0.5 LAG')
        self.assertIn('DPF 0.5, LAG', self.app.seq_steps[1].scpi_cmd)
        self.assertEqual(self.app.seq_steps[1].measured_raw, '0.4998')
        self.app._handle_excel_cell_edited(1, '120 V / 10 A', '60 Hz', '0.5 LAG', '1')
        self.assertEqual(self.app.seq_steps[1].pf_cond_raw, '1 LEAD')
        self.assertIn('DPF 1, LEAD', self.app.seq_steps[1].scpi_cmd)
        self.assertEqual(int(self.grid._row_widgets[1]['frames'][10].grid_info()['column']), 6)
        self.assertEqual(int(self.grid._row_widgets[1]['frames'][9].grid_info()['column']), 7)
        self.app.conn_mgr.send_command.assert_not_called()


class PowerFactorTests(unittest.TestCase):
    SAMPLE = '\n'.join((
        '1\t308001\t\t60 Hz\t120 V\t10 A\t1',
        'Lead\t308001\t\t60 Hz\t120 V\t10 A\t0.5',
        'Lag\t308001\t\t60 Hz\t120 V\t10 A\t0.5',
    ) * 5)

    def test_exact_user_table_all_rows_direction_and_reference_number(self):
        for model in f.MODEL_PROFILES:
            steps = f.SmartClipboardSequenceParser.build_sequence_steps(self.SAMPLE, model=model)
            self.assertEqual(len(steps), 15)
            for index, step in enumerate(steps):
                expected = ('1 LEAD', '0.5 LEAD', '0.5 LAG')[index % 3]
                self.assertEqual(step.func_type, 'PF')
                self.assertEqual(step.nominal_raw, expected)
                self.assertEqual(step.pf_cond_raw, expected)
                self.assertIn('OUT 120 V, 10 A, 60 HZ', step.scpi_cmd)
                self.assertIn('DPF ' + expected.replace(' ', ', '), step.scpi_cmd)
                self.assertNotIn('308001', step.scpi_cmd)
                self.assertTrue(f.SequenceCurrentFilter.includes(step, True, model=model))
                self.assertFalse(f.SequenceCurrentFilter.includes(step, False, model=model))

    def test_direction_notation_and_signed_legacy(self):
        for text, display in [('0.5 lead', '0.5 LEAD'), ('0.5 LAG', '0.5 LAG'), ('1', '1 LEAD'),
                              ('1lead', '1 LEAD'), ('Lead .5', '0.5 LEAD'), ('-0.5', '0.5 LEAD'),
                              ('0.5', '0.5 LAG'), ('-0.5 (0.5 LEAD)', '0.5 LEAD')]:
            self.assertEqual(f.SmartClipboardSequenceParser.parse_power_factor(text)[2], display)
        for text in ('0', '1.1', '308001', '0.5 lead lag', 'nan', '0.5 LGA'):
            with self.assertRaises(ValueError): f.SmartClipboardSequenceParser.parse_power_factor(text)

    def test_existing_power_goal_and_new_coil_pf(self):
        step = f.SmartClipboardSequenceParser.build_Single_step_from_fields(1, '120 V / 10 A', '60 Hz', '0.5 Lead', '600 W')
        self.assertIn('DPF 0.5, LEAD', step.scpi_cmd)
        self.assertEqual(step.func_type, 'ACP')
        coil = f.SmartClipboardSequenceParser.build_sequence_steps(self.SAMPLE, use_coil=True, coil_turns=50)[1]
        self.assertIn('OUT 120 V, 0.2 A, 60 HZ; DPF 0.5, LEAD', coil.scpi_cmd)
        self.assertTrue(f.SequenceCurrentFilter.includes(coil, False))

    def test_direction_column_and_tail_conflict_rejected(self):
        line = 'Lead\t308001\t\t60 Hz\t120 V\t10 A\t0.5 Lag'
        with self.assertRaises(ValueError): f.SmartClipboardSequenceParser.build_sequence_steps(line)


if __name__ == '__main__':
    unittest.main(verbosity=2)
