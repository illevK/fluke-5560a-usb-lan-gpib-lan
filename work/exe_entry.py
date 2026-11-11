"""Windowed executable entry point; the bundled check never opens an instrument."""
import json
import sys
import traceback
from pathlib import Path

import fluke_5560a_controller as controller


def check_bundle(report_path):
    report = {}
    app = None
    try:
        import ctypes
        report['frozen'] = bool(getattr(sys, 'frozen', False))
        report['console_window'] = int(ctypes.windll.kernel32.GetConsoleWindow())
        assert report['frozen'] and report['console_window'] == 0
        assert controller.HAS_TKINTER and controller.HAS_PYVISA and controller.HAS_PYSERIAL
        report['pyvisa'] = controller.pyvisa.__version__
        report['pyserial'] = controller.serial.__version__
        # Load only the installed VISA driver; no list_resources/open_resource or commands.
        manager = controller.SharedVisaManager()
        try:
            report['visa_backend'] = str(manager.visalib)
        finally:
            manager.close()
        controller.AutoNetworkAligner.list_ethernet_interfaces = classmethod(lambda cls, refresh=False: [])
        app = controller.Fluke5560AControllerApp()
        app.withdraw()
        app.update_idletasks()
        assert not app.conn_mgr.info.connected
        assert [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()] == ["측정표", "DUT 연결"]
        assert not app.seq_steps
        report['empty_measurement_table'] = True
        report['title'] = app.title()
        assert Path(app.conn_mgr.log_path).parent.parent == Path(sys.executable).parent
        app.conn_mgr.record_event('EXE package verification: no instrument connection')
        assert Path(app.conn_mgr.log_path).is_file()
        report['log_path'] = app.conn_mgr.log_path
        rows = controller.SmartClipboardSequenceParser.build_sequence_steps(
            '120 V / 10 A\tLead\t\t60 Hz\t120 V\t10 A\t0.5\n1 kV')
        assert rows[0].nominal_raw == '0.5 LEAD' and 'DPF 0.5, LEAD' in rows[0].scpi_cmd
        assert rows[1].scpi_cmd == 'OUT 1000 V, 0 HZ'
        app.seq_steps = rows
        app.excel_grid.render_steps(rows)
        app.excel_grid._row_widgets[0]['measurement_var'].set('0.4999')
        app.excel_grid.set_measurement_visibility(False)
        app.excel_grid.set_measurement_visibility(True)
        assert rows[0].measured_raw == '0.4999'
        app.excel_grid._set_measurement_selection(1, 0)
        assert app.excel_grid.measurement_selection_text() == '0.4999\r\n\r\n'
        copied = []
        app.excel_grid.clipboard_clear = lambda: None
        app.excel_grid.clipboard_append = copied.append
        app.excel_grid._copy_measurements(0, force_cells=True)
        assert copied == ['0.4999\r\n\r\n']
        report['measurement_range_copy'] = True
        app.excel_grid._copy_all_measurements()
        assert copied[-1] == '0.4999\r\n\r\n'
        report['measurement_header_copy'] = True
        dc_text = '\n'.join(('120 V / 1 A', '120 V / 5 A', '120 V / 10 A',
                             '240 V / 1 A', '240 V / 5 A', '240 V / 10 A'))
        app.seq_steps = controller.SmartClipboardSequenceParser.build_sequence_steps(dc_text)
        assert [s.nominal_raw for s in app.seq_steps] == ['120 W','600 W','1200 W','240 W','1200 W','2400 W']
        assert all(s.func_type == 'DCP' for s in app.seq_steps)
        app.excel_grid.render_steps(app.seq_steps)
        app.excel_grid._row_widgets[2]['measurement_var'].set('1234.56 W')
        app.excel_grid._commit_cell(2, 9)
        assert app.seq_steps[2].measured_raw == '1234.56 W'
        assert app.excel_grid._row_widgets[2]['measurement_var'].get() == '1234.56 W'
        app.excel_grid._set_measurement_selection(2, 2)
        app.excel_grid._copy_measurements(2, force_cells=True)
        assert copied[-1] == '1234.56 W\r\n'
        report['dc_power'] = True
        assert all(col[0] != '분해능' for col in app.excel_grid.COL_SPECS)
        report['raw_measurements_retained'] = True
        assert app.var_current_group_basis.get() == controller.SequenceCurrentFilter.POSTS
        app._handle_excel_paste_at_cell('1 A\n2 A\n3.2 A\n0.5 A', 0, 2, replace_all=True)
        app.excel_grid._row_widgets[2]['measurement_var'].set('3.001 A')
        app.btn_group_sort.invoke()
        assert [step.nominal_raw for step in app.seq_steps] == ['1 A', '2 A', '0.5 A', '3.2 A']
        assert app.seq_steps[3].measured_raw == '3.001 A'
        app.btn_group_restore.invoke()
        assert [step.nominal_raw for step in app.seq_steps] == ['1 A', '2 A', '3.2 A', '0.5 A']
        assert app.seq_steps[2].measured_raw == '3.001 A'
        report['current_group_order_restore'] = True
        assert controller.identify_dut('NEWTONS4TH,550,123,1.0').family == 'PPA'
        assert controller.identify_dut('YOKOGAWA,WT310E,123,1.0').family == 'WT'
        wt3000 = controller.identify_dut('YOKOGAWA,760304-04-SV,0,F4.01')
        wt3000e = controller.identify_dut('YOKOGAWA,WT3004E-2A0-30A4,0,F6.01')
        wt5000 = controller.identify_dut('YOKOGAWA,WT5000,123,F1.01')
        assert wt3000.channels == wt3000e.channels == 4 and wt5000.channels == 7
        assert wt3000.wt3000_numeric and wt3000e.wt3000_numeric and not wt5000.wt3000_numeric
        assert app.notebook.tab(app.tab_dut, "text") == "DUT 연결"
        assert app._dut_modes[app.var_dut_mode.get()] == "AUTO"
        assert app.var_seq_dut_capture.get()
        # Selecting the tab normally starts discovery; verify its trigger
        # without letting this packaged test contact any USB/LAN instrument.
        searches = []
        start_dut = app._dut_start
        app._dut_start = lambda operation, settings=None: searches.append(operation)
        app.update()
        app.notebook.select(app.tab_dut)
        # The self-check window is withdrawn; invoke the tab handler even
        # when Tk doesn't deliver a tab-change event for an unmapped notebook.
        app._dut_on_tab_open()
        app.update()
        assert searches == ["discover"] and not app._dut_first_visit, searches
        app._dut_start = start_dut
        report['dut_auto_search_on_first_open'] = True
        app.var_dut_mode.set('LAN · WT5000 TCP')
        app.var_dut_address.set('192.168.1.3')
        assert app._dut_settings().mode == 'WT5000_TCP'
        report['yokogawa_dut_profiles'] = [wt3000.model, wt3000e.model, wt5000.model, 'WT310E']
        app.dut_session.connect(controller.DUTSettings(mode='DEMO'))
        sample = app.dut_session.read()
        app._dut_show_sample(sample)
        assert sample.simulated and app.dut_tree.item('voltage', 'values')[1] == '120'
        assert '미리보기' in app.var_dut_status.get()
        report['dut_display_status'] = app.var_dut_status.get()
        app.dut_session.disconnect()
        assert not app.conn_mgr.info.connected
        report['dut_offline_readout'] = True
        assert 'pip install' not in controller.dependency_import_message('pyvisa', 'PyVISA', 'test')
        report['gui_units_pf_measurements'] = True
        report['passed'] = True
        return 0
    except Exception:
        report['passed'] = False
        report['error'] = traceback.format_exc()
        return 1
    finally:
        if app is not None:
            app._on_close()
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check':
        sys.exit(check_bundle(sys.argv[2]))
    sys.exit(controller.main())
