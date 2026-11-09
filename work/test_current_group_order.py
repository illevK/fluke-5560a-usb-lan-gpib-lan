"""Current-group ordering through the grid controls, without a device."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


class CurrentGroupOrderTests(unittest.TestCase):
    def setUp(self):
        with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]), patch.object(f, 'SAMPLE_POWER_TABLE', '1 V'):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.app.conn_mgr.send_command = Mock(side_effect=AssertionError('Ordering must not send device commands'))
        self.grid = self.app.excel_grid

    def tearDown(self):
        try:
            self.app.conn_mgr.send_command.assert_not_called()
        finally:
            self.app._io_busy = self.app.seq_running = False
            self.app._on_close()

    def load_current_example(self):
        self.app._load_sample_into_sheet('1 A\n2 A\n3 A\n0.5 A', silent=True)

    def set_high_group(self):
        self.app.var_high_current_only.set(True)
        self.app._on_current_group_changed()

    def nominal_values(self):
        return [step.nominal_raw for step in self.app.seq_steps]

    def test_group_toggle_only_changes_filter_and_sort_button_remaps_rows(self):
        self.load_current_example()
        original = ['1 A', '2 A', '3 A', '0.5 A']
        self.set_high_group()
        self.assertEqual(self.nominal_values(), original)
        self.app.var_high_current_only.set(False)
        self.app._on_current_group_changed()
        self.assertEqual(self.nominal_values(), original)

        self.grid.selected_row_idx = 2
        self.app.seq_current_idx = 3
        self.app.seq_paused_idx = 2
        self.app.var_seq_start_row.set('2')
        self.app.var_seq_stop_row.set('3')
        self.app.btn_group_sort.invoke()
        self.assertEqual(self.nominal_values(), ['1 A', '2 A', '0.5 A', '3 A'])
        self.assertEqual([step.step_no for step in self.app.seq_steps], [1, 2, 3, 4])
        self.assertEqual(self.grid.selected_row_idx, 3)
        self.assertEqual(self.app.seq_current_idx, 2)
        self.assertEqual(self.app.seq_paused_idx, -1)
        self.assertEqual((self.app.var_seq_start_row.get(), self.app.var_seq_stop_row.get()), ('1', '4'))

    def test_power_uses_current_filter_and_keeps_both_partitions_stable(self):
        self.app._load_sample_into_sheet('120 V / 1 A\n120 V / 3 A\n120 V / 2 A\n120 V / 5 A', silent=True)
        self.set_high_group()
        self.assertEqual(self.nominal_values(), ['120 W', '360 W', '240 W', '600 W'])
        self.app._reorder_current_group_rows()
        self.assertEqual(self.nominal_values(), ['360 W', '600 W', '120 W', '240 W'])
        self.assertEqual([self.app._step_in_current_group(step) for step in self.app.seq_steps], [True, True, False, False])

    def test_restore_keeps_pasted_readings_and_metadata_with_edited_rows(self):
        self.load_current_example()
        self.app._reorder_current_group_rows()
        self.app._handle_excel_paste_at_cell('0.9999 A\n1.9999 A\n0.4999 A\n2.9999 A', 0, self.grid.MEASUREMENT_COLUMN)
        self.app._handle_excel_paste_at_cell('0.01 A\n0.001 A\n0.001 A\n0.0001 A', 0, self.grid.RESOLUTION_COLUMN)
        self.app.seq_steps[3].breakpoint = True
        self.app._handle_excel_cell_edited(2, '-', 'DC', '-', '4 A')
        self.app.var_model.set('5522A')
        self.app._on_model_changed()
        self.app._on_add_blank_row()
        self.app.btn_group_restore.invoke()

        self.assertEqual(self.nominal_values(), ['1 A', '2 A', '3 A', '4 A', '1.0 V'])
        self.assertEqual([step.step_no for step in self.app.seq_steps], [1, 2, 3, 4, 5])
        self.assertEqual([step.measured_raw for step in self.app.seq_steps], ['0.9999 A', '1.9999 A', '2.9999 A', '0.4999 A', ''])
        self.assertEqual([step.resolution_raw for step in self.app.seq_steps], ['0.01 A', '0.001 A', '0.0001 A', '0.001 A', ''])
        self.assertEqual([step.breakpoint for step in self.app.seq_steps], [False, False, True, False, False])
        self.assertEqual(self.grid._row_widgets[2]['measurement_var'].get(), '2.9999 A')
        self.assertEqual(self.grid._row_widgets[3]['measurement_var'].get(), '0.500 A')

    def test_sort_and_restore_are_blocked_during_execution_and_pending_io(self):
        self.load_current_example()
        self.app._reorder_current_group_rows()
        self.set_high_group()
        ordered = ['1 A', '2 A', '0.5 A', '3 A']
        self.assertEqual(self.nominal_values(), ordered)
        for busy, running in ((True, False), (False, True)):
            with self.subTest(busy=busy, running=running):
                self.app._io_busy = busy
                self.app.seq_running = running
                self.app._reorder_current_group_rows()
                self.assertEqual(self.nominal_values(), ordered)
                self.app._reorder_current_group_rows(restore=True)
                self.assertEqual(self.nominal_values(), ordered)

    def test_loading_new_sheet_replaces_the_original_order(self):
        self.load_current_example()
        self.app._reorder_current_group_rows()
        self.app._load_sample_into_sheet('5 A\n0.1 A\n3 A', silent=True)
        self.app._reorder_current_group_rows(restore=True)
        self.assertEqual(self.nominal_values(), ['5 A', '0.1 A', '3 A'])
        self.app._reorder_current_group_rows()
        self.assertEqual(self.nominal_values(), ['0.1 A', '5 A', '3 A'])
        self.app._reorder_current_group_rows(restore=True)
        self.assertEqual(self.nominal_values(), ['5 A', '0.1 A', '3 A'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
