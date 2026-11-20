"""Qt model/view tests. Offscreen; no physical device, mouse, or desktop clipboard."""
import os
import tempfile
from threading import Event
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch, Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix='calibration-ui-')
os.environ['CALIBRATION_DATA_DIR'] = TEST_DIRECTORY.name

from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
from PySide6.QtCore import Qt, QPoint, QItemSelection, QItemSelectionModel
from PySide6.QtTest import QTest
from calibration.ui.window import MainWindow
from calibration.ui.dialogs import OptionsDialog, ConnectionDialog
from calibration.ui.table import MEASURED
from calibration.services.engine import EngineEvent, State
from calibration.services.connections import ConnectionSettings
from calibration.domain.document import Document
from calibration.services.storage import data_dir

class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()
        self.window.show()
        self.application.processEvents()

    def tearDown(self):
        if self.window.job:
            self.window.stop()
            self.pump(lambda: self.window.job is None)
        self.window._save_timer.stop()
        self.window._close_allowed = True
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()

    def pump(self, condition, limit=5):
        end = time.monotonic() + limit
        while not condition() and time.monotonic() < end:
            self.application.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def paste(self, text):
        self.window.paste_text(text, -1, -1)
        self.application.processEvents()

    def test_empty_start_and_unit_edit_result_reinterpretation_prevented(self):
        window = self.window
        self.assertEqual(window.model.rowCount(), 0)
        self.paste('1 kV\n2 V')
        window.model.setData(window.model.index(0, MEASURED), '0.9999')
        window.model.setData(window.model.index(0, 5), '1000 V')
        self.assertEqual(window.document.rows[0].result(), '999.9 V')
        self.assertEqual(window.document.rows[0].step.measured_raw, '0.9999 kV')

    def test_clear_button_preserves_project_setup_connection_and_next_paste(self):
        window = self.window
        self.assertFalse(window.clear_button.isEnabled())
        window.hub.set_demo(True, model='5522A', dut_model='34461A')
        cal, dut = window.hub.calibrator, window.hub.dut
        doc = window.document
        doc.model = cal.model
        doc.options.dwell = 4.7
        doc.options.current_group = 'low'
        doc.options.default_channel = 2
        doc.options.dmm_terminal = '3'
        doc.options.wait_for_entry = False
        options_before = asdict(doc.options)
        self.paste('1 V\n2 V')
        window.model.setData(window.model.index(0, MEASURED), '0.998')
        window.table.setColumnHidden(MEASURED, True)
        window.refresh_connections()
        commands_before = list(cal.commands)
        badge = window.output_badge.text()
        prefs = dict(window.hub.settings.values)
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / 'plate.calibration.json'
            doc.save(project)
            original_file = project.read_bytes()
            window._next_uid = window.model.active_uid = doc.rows[-1].uid
            window.progress.setValue(500)
            window.clear_button.click()
            self.assertIs(window.document, doc)
            self.assertEqual((doc.path, doc.model, asdict(doc.options)), (project, '5522A', options_before))
            self.assertEqual(project.read_bytes(), original_file)
            self.assertTrue(doc.dirty)
            self.assertEqual(window.model.rowCount(), 0)
            self.assertIsNone(window._next_uid)
            self.assertFalse(window.model.active_uid)
            self.assertEqual(window.progress.value(), 0)
            self.assertEqual(window.progress_label.text(), '0 / 0')
            self.assertFalse(window.table.currentIndex().isValid())
            self.assertFalse(window.clear_button.isEnabled())
            self.assertTrue(window.table.isColumnHidden(MEASURED))
            self.assertIs(window.hub.calibrator, cal)
            self.assertIs(window.hub.dut, dut)
            self.assertEqual((cal.commands, window.output_badge.text()), (commands_before, badge))
            self.assertEqual(window.hub.settings.values, prefs)
            self.paste('3 mV')
            self.assertTrue(window.clear_button.isEnabled())
            self.assertEqual([(r.step.step_no, r.order, r.step.measured_raw) for r in doc.rows], [(1, 0, '')])
            self.assertEqual(doc.rows[0].step.nominal_raw, '3 mV')

    def test_clear_before_autosave_recovers_pending_edit_and_channel(self):
        window = self.window
        self.paste('10 V')
        window.document.rows[0].step.dut_channel = 2
        index = window.model.index(0, MEASURED)
        window.table.setCurrentIndex(index)
        window.table.edit(index)
        self.application.processEvents()
        editor = next(e for e in window.table.findChildren(QLineEdit) if e.isVisible())
        editor.setFocus()
        editor.setText('9.999')
        window.clear_button.click()
        self.assertEqual(window.model.rowCount(), 0)
        self.assertTrue(window.restore_button.isVisible())
        self.assertTrue((data_dir() / 'autosave.json').exists())
        window.restore_button.click()
        self.assertEqual([(r.step.nominal_raw, r.step.measured_raw, r.step.dut_channel)
                          for r in window.document.rows], [('10 V', '9.999 V', 2)])

    def test_clear_unavailable_during_manual_measurement(self):
        window = self.manual_run()
        self.entry_editor(0)
        self.assertFalse(window.clear_button.isEnabled())
        ids = [row.uid for row in window.document.rows]
        window.clear_button.click()
        window.clear_sheet()
        self.assertEqual([row.uid for row in window.document.rows], ids)
        self.assertTrue(window.hub.calibrator.active)
        self.assertTrue(window.model.entry)
        window.stop_button.click()
        self.pump(lambda: not window.busy)
        self.assertTrue(window.clear_button.isEnabled())

    def test_unavailable_recovery_only_clears_if_operator_confirms(self):
        window = self.window
        self.paste('1 V')
        with patch.object(Document, 'save', side_effect=OSError('disk full')), \
                patch.object(QMessageBox, 'question', side_effect=[QMessageBox.StandardButton.No,
                                                                   QMessageBox.StandardButton.Yes]) as ask:
            window.clear_button.click()
            self.assertEqual(window.model.rowCount(), 1)
            window.clear_button.click()
            self.assertEqual(window.model.rowCount(), 0)
        self.assertEqual(ask.call_count, 2)
        self.assertFalse(window.restore_button.isVisible())

    def test_result_column_paste_copy_preserves_selected_rows_and_blanks(self):
        window = self.window
        self.paste('1 V\n2 V\n3 V\n4 V')
        window.paste_text('0.99\n\n2.99', 0, MEASURED)
        clipboard = Mock()
        with patch('calibration.ui.table.QApplication.clipboard', return_value=clipboard):
            window.table.copy_results(all_rows=True)
        data = clipboard.setMimeData.call_args.args[0]
        self.assertEqual(data.text(), '0.99 V\n\n2.99 V\n')
        self.assertTrue(data.html().endswith('<tr><td></td></tr></table>'))
        self.assertEqual(len(window.document.rows), 4)
        self.assertEqual(window.document.rows[2].step.nominal_raw, '3 V')

    def test_column_paste_keeps_frequency_context_and_invalid_rows(self):
        self.paste('60 Hz\t1 A\n60 Hz\t2 A')
        self.window.paste_text('3 A\n300 A', 0, 5)
        rows = self.window.document.rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].step.func_type, 'ACI')
        self.assertEqual(rows[1].step.func_type, 'OUT_OF_RANGE')
        self.assertEqual(rows[1].step.freq_cond_raw, '60 Hz')

    def test_header_copy_is_connected_and_does_not_send_commands(self):
        self.paste('1 V\n2 V')
        clipboard = Mock()
        with patch('calibration.ui.table.QApplication.clipboard', return_value=clipboard):
            self.window.table.horizontalHeader().sectionClicked.emit(MEASURED)
        self.assertEqual(clipboard.setMimeData.call_args.args[0].text(), '\n')
        self.assertFalse(self.window.hub.calibrator.connected)

    def test_copy_includes_live_last_editor_and_last_selected_column(self):
        self.paste('1 V\n2 V\n3 V')
        table, model = self.window.table, self.window.model
        last = model.index(2, MEASURED)
        table.setCurrentIndex(last)
        table.edit(last)
        table.indexWidget(last).setText('3001.050 mV')
        clip = Mock()
        with patch('calibration.ui.table.QApplication.clipboard', return_value=clip):
            header = table.horizontalHeader()
            x = header.sectionViewportPosition(MEASURED) + header.sectionSize(MEASURED) // 2
            QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, header.height() // 2))
            self.assertEqual(clip.setMimeData.call_args.args[0].text(), '\n\n3.001050 V')
            table.setCurrentIndex(model.index(1, MEASURED))
            table.copy_results(to_end=True)
            self.assertEqual(clip.setMimeData.call_args.args[0].text(), '\n3.001050 V')
            table.selectionModel().select(QItemSelection(model.index(1, 5), last),
                                          QItemSelectionModel.SelectionFlag.ClearAndSelect)
            table.copy_selection()
            self.assertEqual(clip.setMimeData.call_args.args[0].text(), '2 V\t\n3 V\t3.001050 V')

    def test_enter_commits_and_moves_down_without_output(self):
        self.paste('1 V\n2 V')
        table = self.window.table
        table.setCurrentIndex(self.window.model.index(0, MEASURED))
        table.edit(table.currentIndex())
        self.application.processEvents()
        editor = table.findChild(QLineEdit)
        self.assertIsNotNone(editor)
        editor.setText('0.999')
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.application.processEvents()
        self.assertEqual(self.window.document.rows[0].step.measured_raw, '0.999 V')
        self.assertEqual(table.currentIndex().row(), 1)
        self.assertFalse(self.window.hub.calibrator.connected)

    def test_group_sort_restore_retains_row_results(self):
        window = self.window
        self.paste('1 A\n5 A\n2 A')
        window.document.options.current_group = 'low'
        window.document.rows[1].step.measured_raw = '4.999 A'
        identities = [row.uid for row in window.document.rows]
        window.sort_group()
        self.assertEqual([row.step.nominal_raw for row in window.document.rows], ['1 A', '2 A', '5 A'])
        window.restore_order()
        self.assertEqual([row.uid for row in window.document.rows], identities)
        self.assertEqual(window.document.rows[1].result(), '4.999 A')

    def test_async_engine_updates_rows_and_does_not_block_ui(self):
        window = self.window
        self.paste('1 V\n60 Hz\t2 V')
        window.document.options.dwell = .2
        window.hub.set_demo(True)
        window.table.setCurrentIndex(window.model.index(0, 5))
        window.start()
        self.assertTrue(window.busy)
        self.assertTrue(window.model.locked)
        self.pump(lambda: not window.busy)
        self.assertEqual(window.engine.state, State.COMPLETE)
        self.assertEqual([row.step.measured_raw for row in window.document.rows], ['1 V', '2 V'])
        self.assertTrue(all(row.simulated for row in window.document.rows))
        self.assertIn('미리보기', window.document.rows[0].result())
        self.assertFalse(window.hub.calibrator.active)

    def manual_run(self, *, timed=False):
        window = self.window
        self.paste('1 mV\n2 mV')
        window.hub.set_demo(True, dut_model=None)
        window.document.options.dwell = .3
        window.document.options.wait_for_entry = not timed
        window.table.setCurrentIndex(window.model.index(0, 5))
        window.start_button.click()
        self.assertTrue(window.busy)
        return window

    def entry_editor(self, row):
        window = self.window
        uid = window.document.rows[row].uid
        def current_editor():
            return next((e for e in window.table.findChildren(QLineEdit)
                         if e.isVisible() and e.property('entry_uid') == uid), None)
        self.pump(current_editor)
        return current_editor()

    def test_calibrator_only_opens_hidden_column_and_enter_confirms_each_output(self):
        window = self.window
        window.toggle_results(False)
        try:
            self.manual_run()
            first = self.entry_editor(0)
            self.assertFalse(window.table.isColumnHidden(MEASURED))
            self.assertTrue(window.model.flags(window.model.index(0, MEASURED)) & Qt.ItemFlag.ItemIsEditable)
            for row, col in ((0, 5), (1, MEASURED)):
                self.assertFalse(window.model.flags(window.model.index(row, col)) & Qt.ItemFlag.ItemIsEditable)
            QTest.keyClick(first, Qt.Key.Key_Return)
            self.assertEqual(window.engine.state, State.MANUAL)
            self.assertEqual(window.engine.completed, 0)
            self.assertTrue(window.hub.calibrator.active)
            first.setText('999 uV')
            window.table.setFocus()  # Losing focus must not confirm or discard an entry.
            window.table.edit(window.model.index(0, MEASURED))
            first = self.entry_editor(0)
            self.assertEqual(first.text(), '999 uV')
            self.assertEqual(window.document.rows[0].result(), '')
            QTest.keyClick(first, Qt.Key.Key_Return)
            second = self.entry_editor(1)
            self.assertIn('0.999 mV', window.document.rows[0].result())
            self.assertEqual(window.document.rows[0].step.measured_raw, '999 uV')
            second.setText('1.999')
            QTest.keyClick(second, Qt.Key.Key_Return)
            self.pump(lambda: not window.busy)
            self.assertEqual(window.document.rows[1].step.measured_raw, '1.999 mV')
            self.assertEqual(window.engine.state, State.COMPLETE)
            self.assertFalse(window.hub.calibrator.active)
            self.assertTrue(window.table.isColumnHidden(MEASURED))
        finally:
            window.toggle_results(True)

    def test_stop_discards_unconfirmed_entry_and_prevents_next_output(self):
        window = self.manual_run()
        editor = self.entry_editor(0)
        run_id, uid = window.model.entry
        editor.setText('123')
        window.stop_button.click()
        self.assertFalse(window.engine.submit_entry(run_id, uid, '123'))
        self.pump(lambda: not window.busy)
        self.assertEqual(window.engine.state, State.STOPPED)
        self.assertEqual(window.engine.completed, 0)
        self.assertFalse(window.hub.calibrator.active)
        self.assertTrue(all(not row.step.measured_raw for row in window.document.rows))

    def test_timed_manual_measurements_remain_editable_during_output(self):
        window = self.manual_run(timed=True)
        index = window.model.index(0, MEASURED)
        self.assertTrue(window.model.flags(index) & Qt.ItemFlag.ItemIsEditable)
        window.table.edit(index)
        self.application.processEvents()
        editor = next(e for e in window.table.findChildren(QLineEdit) if e.isVisible())
        editor.setText('0.987')
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.pump(lambda: not window.busy)
        self.assertEqual(window.engine.state, State.COMPLETE)
        self.assertEqual([r.step.measured_raw for r in window.document.rows], ['0.987 mV', ''])
        self.assertTrue(window.document.rows[0].simulated)

    def test_default_advances_with_blank_results_and_keeps_unfinished_edit_on_chosen_row(self):
        window = self.window
        self.paste('1 mV\n2 mV\n3 mV')
        window.hub.set_demo(True, dut_model=None)
        window.refresh_connections()
        window.document.options.dwell = .25
        self.assertEqual(window.document.options.record_mode(False), 'timed')
        self.assertIn('자동 진행 · 자유 입력', window.summary.text())
        window.toggle_results(False)
        try:
            window.table.setCurrentIndex(window.model.index(0, 5))
            window.start_button.click()
            self.assertTrue(window.busy)
            index = window.model.index(1, MEASURED)
            window.table.setCurrentIndex(index)
            window.table.edit(index)
            self.application.processEvents()
            editor = next(e for e in window.table.findChildren(QLineEdit) if e.isVisible())
            editor.setText('1.')
            self.pump(lambda: not window.busy)
            self.assertEqual((window.engine.state, window.engine.completed), (State.COMPLETE, 3))
            self.assertEqual(window.hub.calibrator.commands.count('OPER'), 3)
            self.assertFalse(window.hub.calibrator.active)
            self.assertEqual([r.step.measured_raw for r in window.document.rows], ['', '', ''])
            self.assertFalse(window.table.isColumnHidden(MEASURED))
            self.assertFalse(window.hub.settings.get('show_results'))
            self.assertTrue(editor.isVisible())
            self.assertEqual(window.table.currentIndex(), index)
            self.assertEqual(editor.text(), '1.')
            QTest.keyClicks(editor, '999')
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.application.processEvents()
            self.assertEqual([r.step.measured_raw for r in window.document.rows], ['', '1.999 mV', ''])
            self.assertTrue(window.document.rows[1].simulated)
            self.assertEqual(window.table.currentIndex(), window.model.index(2, MEASURED))
        finally:
            window.toggle_results(True)

    def test_free_entry_edits_do_not_advance_output_or_overwrite_earlier_rows(self):
        window = self.window
        self.paste('1 mV\n2 mV\n3 mV')
        window.hub.set_demo(True, dut_model=None)
        window.document.options.dwell = .35
        window.table.setCurrentIndex(window.model.index(0, 5))
        window.start_button.click()
        self.pump(lambda: window.status_title.text() == '2행 · 2 mV 출력 중')
        window.paste_text('0.998\n1.995', 0, MEASURED)
        third = window.model.index(2, MEASURED)
        self.assertTrue(window.model.setData(third, '99'))
        window.table.setCurrentIndex(third)
        QTest.keyClick(window.table, Qt.Key.Key_Delete)
        self.assertFalse(window.model.setData(window.model.index(0, 5), '100 V'))
        self.pump(lambda: not window.busy)
        self.assertEqual(window.engine.state, State.COMPLETE)
        self.assertEqual([r.step.measured_raw for r in window.document.rows], ['0.998 mV', '1.995 mV', ''])
        self.assertEqual(window.hub.calibrator.commands.count('OPER'), 3)

    def test_free_entry_stops_at_wiring_change_without_waiting_for_a_value(self):
        window = self.window
        self.paste('1 A\n5 A')
        window.hub.set_demo(True, dut_model=None)
        window.document.options.dwell = .2
        window.table.setCurrentIndex(window.model.index(0, 5))
        window.start_button.click()
        self.pump(lambda: not window.busy)
        self.assertEqual((window.engine.state, window.engine.completed), (State.WIRING, 1))
        self.assertFalse(window.hub.calibrator.active)
        self.assertEqual(window._next_uid, window.document.rows[1].uid)
        self.assertEqual(window.hub.calibrator.commands.count('OPER'), 1)

    def test_free_entry_can_commit_while_idle_connection_poll_is_pending(self):
        window = self.window
        self.paste('1 V\n2 V')
        editor_index = window.model.index(0, MEASURED)
        window.table.setCurrentIndex(editor_index)
        window.table.edit(editor_index)
        self.application.processEvents()
        editor = next(e for e in window.table.findChildren(QLineEdit) if e.isVisible())
        editor.setText('0.998')
        release = Event()
        def poll():
            release.wait(2)
            return False
        with patch.object(window.hub, 'calibrator', Mock(connected=True, model='5560A')), patch.object(window.hub, 'health_check', poll):
            window.health_check()
            try:
                self.assertTrue(window.busy)
                QTest.keyClick(editor, Qt.Key.Key_Return)
                self.assertEqual(window.document.rows[0].step.measured_raw, '0.998 V')
            finally:
                release.set()
                self.pump(lambda: not window.busy)

    def test_stale_run_result_does_not_change_table(self):
        self.paste('1 V')
        window = self.window
        window.engine = Mock(run_id='current')
        event = EngineEvent('old', 'result', State.READING, window.document.rows[0].uid, measured='999 V')
        window.on_engine_event(event)
        self.assertEqual(window.document.rows[0].step.measured_raw, '')

    def test_dialog_options_round_trip_and_nominal_units(self):
        dialog = OptionsDialog(self.window.document.options, '5560A')
        dialog.dual.setChecked(True)
        dialog.dc.setText('100 mV')
        dialog.ac.setText('0.1 V')
        options = dialog.value()
        options.validate()
        self.assertEqual(options.dual_dc, .1)
        self.assertEqual(options.dwell, 3.5)
        self.assertEqual(options.record_mode(False), 'timed')
        self.assertEqual(options.record_mode(True), 'dut')
        dialog.manual_progress.setCurrentIndex(dialog.manual_progress.findData(True))
        self.assertEqual(dialog.value().record_mode(False), 'entry')
        self.assertEqual(dialog.value().record_mode(True), 'dut')
        restored_options = OptionsDialog(dialog.value(), '5560A')
        self.assertEqual(restored_options.manual_progress.currentData(), True)
        restored_options.deleteLater()
        dialog.deleteLater()
        connection = ConnectionDialog(ConnectionSettings(), 'dut')
        self.assertEqual(connection.value().mode, 'AUTO')
        choices = [connection.model.itemData(i) for i in range(connection.model.count())]
        self.assertEqual(set(choices), {'AUTO', 'WT', 'PPA', 'HIOKI', '34401A', '34461A', '34465A'})
        for model in choices:
            connection.model.setCurrentIndex(connection.model.findData(model))
            self.assertEqual(connection.value().model, model)
            restored = ConnectionDialog(connection.value(), 'dut')
            self.assertEqual(restored.value().model, model)
            restored.deleteLater()
        connection.deleteLater()

    def test_large_table_uses_model_rows_instead_of_per_cell_widgets(self):
        self.paste('\n'.join(f'{i % 100 + 1} V' for i in range(3000)))
        self.assertEqual(self.window.model.rowCount(), 3000)
        self.assertLess(len(self.window.table.findChildren(QLineEdit)), 3)
        self.window.table.scrollToBottom()
        self.application.processEvents()
