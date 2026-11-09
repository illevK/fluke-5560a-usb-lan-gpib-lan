"""Result range selection and clipboard payload tests; never use the real clipboard/device."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_units_measurements as fixtures

f = fixtures.f


class MeasurementCopyTests(unittest.TestCase):
    setUp = fixtures.MeasurementTests.setUp
    tearDown = fixtures.MeasurementTests.tearDown
    enter = fixtures.MeasurementTests.enter

    def test_drag_copies_only_selected_rows_in_table_order(self):
        for row, value in enumerate(('unselected', '1.00020 mV', '0.4999 LEAD')):
            self.enter(row, value)
        commands = [s.scpi_cmd for s in self.app.seq_steps]
        event = SimpleNamespace(state=0, y_root=120)
        self.grid._measurement_press(event, 2)
        with patch.object(self.grid, '_measurement_row_at', return_value=1), patch.object(self.grid, '_measurement_autoscroll'):
            self.assertEqual(self.grid._measurement_motion(event), 'break')
        self.assertIsNone(self.grid._measurement_release(event))
        self.assertEqual(self.grid._measurement_bounds(), (1, 2))
        with patch.object(self.grid, 'clipboard_clear') as clear, patch.object(self.grid, 'clipboard_append') as copy:
            self.assertEqual(self.grid._copy_measurements(2), 'break')
            clear.assert_called_once()
            copy.assert_called_once_with('1.00020 mV\r\n0.4999 LEAD\r\n')
        self.assertEqual([s.scpi_cmd for s in self.app.seq_steps], commands)
        self.app.conn_mgr.send_command.assert_not_called()

    def test_empty_middle_and_trailing_cells_preserve_excel_row_alignment(self):
        self.enter(0, '0.00100')
        self.enter(1, '')
        self.enter(2, '')
        self.grid._set_measurement_selection(2, 0)
        self.assertEqual(self.grid.measurement_selection_text(), '0.00100\r\n\r\n\r\n')
        self.enter(2, '3.0')
        self.assertEqual(self.grid.measurement_selection_text(), '0.00100\r\n\r\n3.0\r\n')

    def test_single_cell_text_selection_and_normal_cursor_are_preserved(self):
        entry = self.enter(0, '1.234 V')
        event = SimpleNamespace(state=0, y_root=120)
        self.assertIsNone(self.grid._measurement_press(event, 0))
        with patch.object(self.grid, '_measurement_row_at', return_value=0):
            self.assertIsNone(self.grid._measurement_motion(event))
        self.grid._measurement_release(event)
        entry.selection_range(0, 5)
        with patch.object(self.grid, 'clipboard_clear') as clear:
            self.assertIsNone(self.grid._copy_measurements(0))
            clear.assert_not_called()
        self.assertTrue(entry.bind('<<Copy>>'))
        self.assertTrue(entry.bind('<B1-Motion>'))
        with patch.object(self.grid, 'clipboard_clear'), patch.object(self.grid, 'clipboard_append') as copy:
            self.grid._copy_measurements(0, force_cells=True)
            copy.assert_called_once_with('1.234 V\r\n')

    def test_shift_click_and_keys_extend_and_enter_returns_to_editing(self):
        self.grid._measurement_press(SimpleNamespace(state=0, y_root=100), 0)
        self.grid._measurement_release(None)
        self.grid._measurement_press(SimpleNamespace(state=1, y_root=160), 2)
        self.grid._measurement_release(None)
        self.assertEqual(self.grid._measurement_bounds(), (0, 2))
        self.grid._measurement_extend_key(2, -1)
        self.assertEqual(self.grid._measurement_bounds(), (0, 1))
        self.grid._measurement_extend_key(2, 1)
        self.grid._measurement_extend_key(2, 1)
        self.assertEqual(self.grid._measurement_bounds(), (0, 2))
        self.grid._commit_and_move(0, 9, 1, 0)
        self.assertEqual(self.grid._measurement_bounds(), (1, 1))
        self.app.conn_mgr.send_command.assert_not_called()

    def test_context_menu_preserves_selection_inside_and_selects_outside(self):
        self.grid._set_measurement_selection(1, 2)
        event = SimpleNamespace(x_root=20, y_root=30)
        with patch.object(self.grid._measurement_menu, 'tk_popup') as popup, patch.object(self.grid._measurement_menu, 'grab_release'):
            self.grid._measurement_context(event, 2)
            self.assertEqual(self.grid._measurement_bounds(), (1, 2))
            popup.assert_called_once_with(20, 30)
            self.grid._measurement_context(event, 0)
            self.assertEqual(self.grid._measurement_bounds(), (0, 0))

    def test_live_status_refresh_keeps_highlight_and_replacement_clears_selection(self):
        self.enter(1, '1.999')
        self.grid._set_measurement_selection(1, 2)
        self.app.seq_steps[1].status = '✓ 완료'
        self.grid.render_steps(self.app.seq_steps, active_idx=2)
        self.assertEqual(self.grid._measurement_bounds(), (1, 2))
        self.assertEqual(self.grid._row_widgets[1]['entries'][9].cget('bg'), '#DBEAFE')
        self.grid.set_measurement_visibility(False)
        self.assertIsNone(self.grid._measurement_bounds())
        self.grid.set_measurement_visibility(True)
        self.assertEqual(self.app.seq_steps[1].measured_raw, '1.999')
        self.grid._set_measurement_selection(1, 2)
        self.app.seq_steps = self.app.seq_steps[:1]
        self.grid.render_steps(self.app.seq_steps)
        self.assertIsNone(self.grid._measurement_bounds())

    def test_autoscroll_stops_on_release_without_mutating_results(self):
        self.grid._set_measurement_selection(0, 1)
        self.grid._measurement_drag_active = self.grid._measurement_dragging = True
        self.grid._measurement_pointer_y = 301
        with patch.object(self.grid.canvas, 'winfo_rooty', return_value=100), \
             patch.object(self.grid.canvas, 'winfo_height', return_value=200), \
             patch.object(self.grid.canvas, 'yview', side_effect=[(0, .5), (.1, .6)]), \
             patch.object(self.grid.canvas, 'yview_scroll') as scroll, \
             patch.object(self.grid, '_measurement_row_at', return_value=2):
            self.grid._measurement_autoscroll()
            scroll.assert_called_once_with(1, 'units')
        self.assertIsNotNone(self.grid._measurement_scroll_after)
        self.assertEqual(self.grid._measurement_bounds(), (0, 2))
        self.grid._measurement_release(None)
        self.assertIsNone(self.grid._measurement_scroll_after)
        self.assertFalse(self.grid._measurement_drag_active)
        self.assertEqual([s.measured_raw for s in self.app.seq_steps], ['', '', ''])

    def test_hit_testing_uses_row_positions_and_copy_event_routes_to_range(self):
        self.app.update_idletasks()
        self.grid.canvas.yview_moveto(0)
        origin = self.grid.canvas.winfo_rooty() - self.grid.canvas.canvasy(0)
        for row, widgets in enumerate(self.grid._row_widgets):
            frame = widgets['frames'][9]
            self.assertEqual(self.grid._measurement_row_at(origin + frame.winfo_y() + frame.winfo_height() / 2), row)
        self.assertEqual(self.grid._measurement_row_at(origin - 100), 0)
        self.assertEqual(self.grid._measurement_row_at(origin + 100000), 2)
        self.enter(1, '1.999 V')
        self.enter(2, '2.999 V')
        self.grid._set_measurement_selection(1, 2)
        with patch.object(self.grid, 'clipboard_clear'), patch.object(self.grid, 'clipboard_append') as copied:
            self.grid._row_widgets[1]['entries'][9].event_generate('<<Copy>>')
            copied.assert_called_once_with('1.999 V\r\n2.999 V\r\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
