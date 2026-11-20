"""Windowed application entry point. Importing it never connects to equipment."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

def check_packaged_manual_entry(window, application, document):
    """Exercise the same Start button, result editor and Enter event as the operator."""
    import time
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QLineEdit
    window.hub.set_demo(True, dut_model=None)
    window.document = document
    window.model.refresh(document)
    window.refresh_connections()
    window.table.setCurrentIndex(window.model.index(0, 5))
    window.start_button.click()
    deadline = time.monotonic() + 8
    submitted = set()
    while window.busy and time.monotonic() < deadline:
        application.processEvents()
        for editor in window.table.findChildren(QLineEdit):
            uid = editor.property('entry_uid')
            if editor.isVisible() and uid and uid not in submitted:
                submitted.add(uid)
                editor.setText('0.999')
                application.sendEvent(editor, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
        time.sleep(.01)
    if window.busy:
        window.stop()
        while window.busy:
            application.processEvents()
            time.sleep(.01)
    return [row.step.measured_raw for row in document.rows]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', metavar='REPORT')
    parser.add_argument('--render-preview', metavar='PNG')
    parser.add_argument('--render-empty', metavar='PNG')
    args = parser.parse_args()
    checking = args.self_check or args.render_preview or args.render_empty
    if checking:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        temporary = tempfile.TemporaryDirectory(prefix='calibration-check-')
        os.environ['CALIBRATION_DATA_DIR'] = temporary.name
    from PySide6.QtWidgets import QApplication
    from calibration import __version__
    from calibration.ui.window import MainWindow
    from calibration.ui.theme import STYLE
    application = QApplication(sys.argv[:1])
    if checking and sys.platform == 'win32':
        from PySide6.QtGui import QFontDatabase
        # The offscreen Qt backend does not enumerate the Windows font registry.
        for filename in ('malgun.ttf', 'malgunbd.ttf', 'segoeui.ttf'):
            font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / filename
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))
    application.setApplicationName('교정자동화')
    application.setApplicationVersion(__version__)
    application.setOrganizationName('CalibrationAutomation')
    application.setStyle('Fusion')
    application.setStyleSheet(STYLE)
    window = MainWindow()
    empty_start_rows = window.model.rowCount()
    if args.render_preview:
        from calibration.domain.document import RunOptions
        window.document.insert(window.document.parse('DC VOLT\n5 V\n10 V\n100 V\nAC VOLT\n60 Hz\t120 V\n60 Hz\t240 V\nDC CURR\n0.1 A\n1 A\n3 A\nDC POWER\n120 V / 1 A\n120 V / 5 A\n'))
        for row in window.document.rows:
            row.step.dut_channel = 1
        for row, value in zip(window.document.rows[:3], ['5.000018 V', '9.999984 V', '100.00012 V']):
            row.step.measured_raw = value
            row.step.status = '완료 · 미리보기'
            row.simulated = True
        window.model.refresh()
        window.hub.set_demo(True, dut_model='PW3390')
        window.refresh_connections()
        window.show_output(False)
        window.notice('화면 미리보기 · 실제 장비에 연결하지 않았습니다.')
    window.show()
    if checking:
        application.processEvents()
        if args.render_preview or args.render_empty:
            window.grab().save(args.render_preview or args.render_empty)
        if args.self_check:
            from calibration.domain.document import Document
            from calibration.domain.planning import make_plan
            from calibration.services.drivers import SimulatedCalibrator, SimulatedDUT
            from calibration.services.engine import MeasurementEngine, State
            doc = Document()
            doc.options.dwell = .2
            doc.insert(doc.parse('DC VOLT\n1 V\nAC VOLT\n60 Hz\t2 V'))
            events = []
            engine = MeasurementEngine(SimulatedCalibrator(), SimulatedDUT(), events.append)
            engine.run(make_plan(doc.rows, 0, doc.options, doc.model, '34465A'), doc.options)
            success = engine.state == State.COMPLETE and len([event for event in events if event.kind == 'result']) == 2
            from calibration.services.connections import ConnectionSettings
            from calibration.ui.dialogs import ConnectionDialog
            dialog = ConnectionDialog(ConnectionSettings(), 'dut', window)
            choices = [dialog.model.itemData(i) for i in range(dialog.model.count())]
            success &= {'AUTO', 'WT', 'PPA', 'HIOKI', '34401A', '34461A', '34465A'}.issubset(choices)
            source_dialog = ConnectionDialog(ConnectionSettings(model='5730A'), 'calibrator', window)
            title = window.windowTitle()
            success &= source_dialog.model.currentData() == '5730A' and title == f'교정자동화 · v{__version__}'
            source_dialog.mode.setCurrentIndex(source_dialog.mode.findData('USB'))
            setup_guide_ok = '가상 COM' in source_dialog.pc_guide.text()
            retry_dialog = ConnectionDialog(ConnectionSettings(model='5522A', mode='RS232'), 'calibrator', window,
                                            failure='모의 연결 실패', allow_connect=True)
            setup_guide_ok &= ('SERIAL 1 FROM HOST' in retry_dialog.panel_guide.text()
                               and retry_dialog.mode.findData('LAN') < 0 and retry_dialog.connect_button.text() == '다시 연결')
            success &= setup_guide_ok
            blank_results = check_packaged_manual_entry(window, application, doc)
            free_entry = (window.engine.state == State.COMPLETE and window.engine.completed == 2
                          and window.engine.output is False and blank_results == ['', ''])
            success &= free_entry
            doc.options.wait_for_entry = True
            recorded = check_packaged_manual_entry(window, application, doc)
            success &= (window.engine is not None and window.engine.state == State.COMPLETE
                        and recorded == ['0.999 V', '0.999 V'] and window.engine.output is False)
            source = window.hub.calibrator
            window.table.copy_results(all_rows=True)
            copied_rows = application.clipboard().mimeData().text().splitlines()
            # The offscreen backend owns this synthetic clipboard until it is cleared.
            application.clipboard().clear()
            success &= copied_rows == [row.result() for row in doc.rows] and len(copied_rows) == 2
            window.clear_button.click()
            cleared = (window.model.rowCount() == 0 and window.document is doc and doc.options.dwell == .2
                       and window.hub.calibrator is source and source.connected and window.restore_button.isVisible())
            success &= cleared
            Path(args.self_check).write_text(json.dumps({'version': __version__, 'passed': success,
                'window_title': title, 'calibrator_choice': source_dialog.model.currentData(), 'copied_rows': copied_rows,
                'calibrator_connection_guide': setup_guide_ok,
                'cleared_sheet_preserving_setup': cleared,
                'timed_run_without_entry': free_entry,
                'dut_choices': choices, 'calibrator_only_entries': recorded,
                'empty_start_rows': empty_start_rows, 'tk_loaded': 'tkinter' in sys.modules,
                'simulated_only': True, 'state': engine.state.value}, ensure_ascii=False), encoding='utf-8')
            if not success:
                return 1
        window._close_allowed = True
        window.close()
        window.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
        temporary.cleanup()
        return 0
    return application.exec()

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        from calibration.services.storage import data_dir
        report = data_dir() / 'startup-error.log'
        report.write_text(traceback.format_exc(), encoding='utf-8')
        if sys.platform == 'win32' and not any(arg.startswith('--') for arg in sys.argv[1:]):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'프로그램 시작 중 오류가 발생했습니다.\n{report}', '교정자동화', 0x10)
        raise SystemExit(1)
