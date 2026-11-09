"""Inspect our Tk layout directly; no desktop input or instrument connection."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f

with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]):
    app = f.Fluke5560AControllerApp()
    problems = []
    measurements = []
    def controls(widget, skip_scrolled=True):
        if isinstance(widget, f.ExcelSheetGrid) or (skip_scrolled and widget is app.dut_content):
            return  # Rows outside the scroll viewport are intentionally clipped.
        for child in widget.winfo_children():
            if isinstance(child, (f.tk.Button, f.ttk.Button, f.tk.Entry, f.ttk.Entry, f.ttk.Combobox, f.ttk.Menubutton)):
                yield child
            yield from controls(child, skip_scrolled)
    try:
        for size in ('1280x900', '1060x740'):
            app.geometry(size)
            app.update()
            for tab in app.notebook.tabs():
                app.notebook.select(tab)
                app.update()
                for widget in controls(app):
                    if not widget.winfo_ismapped():
                        continue
                    x=widget.winfo_rootx()-app.winfo_rootx()
                    y=widget.winfo_rooty()-app.winfo_rooty()
                    if x < 0 or y < 0 or x+widget.winfo_width()>app.winfo_width() or y+widget.winfo_height()>app.winfo_height():
                        problems.append((size, app.notebook.tab(tab,'text'),str(widget),widget.cget('text') if 'text' in widget.keys() else '',x,y))
            app.notebook.select(app.tab_sequence)
            app.update()
            measurements.append({'size':size,'table_height':app.excel_grid.winfo_height(),'table_width':app.excel_grid.winfo_width()})
            app.notebook.select(app.tab_dut)
            for expanded in (False, True):
                if app._dut_details_open != expanded:
                    app._dut_toggle_details()
                app.update()
                canvas, content = app.dut_canvas, app.dut_content
                bounds = canvas.bbox('all')
                assert bounds[3] >= content.winfo_reqheight()
                assert app.dut_tree.winfo_height() >= app.dut_tree.winfo_reqheight()
                for widget in controls(content, False):
                    assert widget.winfo_rootx() >= content.winfo_rootx()
                    assert widget.winfo_rootx()+widget.winfo_width() <= content.winfo_rootx()+content.winfo_width()
                canvas.yview_moveto(1)
                app.update()
                assert abs(canvas.yview()[1] - 1) < .01
                canvas.yview_moveto(0)
            app._dut_toggle_details()
        commands = [s.scpi_cmd for s in app.seq_steps]
        app.var_timeout.set('7')
        app.var_seq_dwell_sec.set('2')
        for mode in ('LAN','RS232','GPIB','USB','AUTO','SIMULATION'):
            app.var_conn_mode.set(mode)
            app._toggle_connection_details()
            app.update()
            assert [k for k,v in app.connection_endpoints.items() if v.winfo_manager()] == [mode]
            app._toggle_connection_details()
        for toggle in (app._toggle_sequence_details, app._toggle_console):
            toggle(); app.update(); toggle(); app.update()
        app.excel_grid.set_command_visibility(True)
        app.update()
        assert all(row['frames'][7].winfo_manager() for row in app.excel_grid._row_widgets)
        app.excel_grid.set_command_visibility(False)
        app.excel_grid.render_steps(app.seq_steps)
        assert all(not row['frames'][7].winfo_manager() for row in app.excel_grid._row_widgets)
        assert [s.scpi_cmd for s in app.seq_steps] == commands
        assert app.var_timeout.get()=='7' and app.var_seq_dwell_sec.get()=='2'
        assert app.bind('<F8>') and app.bind('<F9>')
        report = {'measurements':measurements,'clipped_controls':problems,'folding_preserves_settings_and_commands':True}
        Path('work/ui_layout_results.txt').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))
        if problems:
            raise AssertionError('Clipped controls')
    finally:
        app._on_close()
