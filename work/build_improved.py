from pathlib import Path
import re

root = Path(__file__).resolve().parent.parent
source = (root / 'work/fluke_5560a_controller.original.py').read_text(encoding='utf-8')
dependency_start = source.index('try:\n    import pyvisa\n')
dependency_end = source.index('    HAS_PYVISA = False', dependency_start) + len('    HAS_PYVISA = False')
source = source[:dependency_start] + (root / 'work/runtime_dependencies.pyfrag').read_text(encoding='utf-8').rstrip() + source[dependency_end:]
start = source.index('class AutoNetworkAligner:')
end = source.index('# 6. 실제 엑셀', start)
end = source.rfind('# =====================================================================', start, end)
communication = (root / 'work/communication.pyfrag').read_text(encoding='utf-8')
communication = communication.replace('class Fluke5560AConnectionManager:',
    (root / 'work/serial_transport.pyfrag').read_text(encoding='utf-8') + 'class Fluke5560AConnectionManager:')
source = source[:start] + (root / 'work/model_profiles.pyfrag').read_text(encoding='utf-8') + (root / 'work/current_group_policy.pyfrag').read_text(encoding='utf-8') + communication + '\n\n' + source[end:]
start = source.index('        def _build_connection_panel(self)')
end = source.index('        def _build_safety_bar(self)', start)
source = source[:start] + (root / 'work/connection_ui.pyfrag').read_text(encoding='utf-8') + '\n' + source[end:]
start = source.index('        def _on_connect_clicked(self)')
end = source.index('        def _update_connection_ui(self', start)
source = source[:start] + (root / 'work/connect_handlers.pyfrag').read_text(encoding='utf-8') + '\n' + source[end:]
start = source.index('"""')
end = source.index('"""', start + 3) + 3
source = source[:start] + '''"""
Fluke 5560A / 5522A / 5520A / 5080A Controller v5.3.1
LAN: verified physical adapter, source-IP binding, Telnet stream framing,
strict *IDN? validation, read-only diagnosis, background connection tasks.
USB/GPIB: PyVISA with backend diagnostics and correct interface handling.
RS232: pyserial, including PC USB-to-RS232 adapters, with configurable line settings.
No automatic PC IP/DHCP changes; no open-port identification fallback.
Reference: Fluke 5560A Remote Programmers Manual, Rev. 3 (3/25),
https://media.fluke.com/b2b30a71-26d0-4067-be01-b308017892a8_original%20file.pdf
"""''' + source[end:]
source = source.replace('import tempfile\n', 'import ipaddress\nimport json\nimport threading\nimport queue\nimport argparse\n')
source = source.replace('from concurrent.futures import ThreadPoolExecutor, as_completed\n', '')
source = source.replace('from tkinter import ttk, messagebox', 'from tkinter import ttk, messagebox, filedialog')
source = source.replace('CANDIDATE_LAN_PORTS = (3490, 5025, 23)\n', '')
source = source.replace('v5.2', 'v5.3.1')
source = source.replace('# 4. 내 PC 랜카드 IP 자동 변경 및 5560A IP 교차 대역 자동 탐지 엔진 (AutoNetworkAligner)',
                        '# 4. 실제 랜카드 조회 및 USB/GPIB/LAN 통신 엔진')
source = source.replace('(5560A IP 자동탐지 & 내 PC IP 자동변경 + 엑셀 시트 복붙)', '(USB / LAN / GPIB + LAN 진단)')
source = source.replace('            self.is_operating = False\n\n            self.seq_steps', '            self.is_operating = None\n\n            self.seq_steps')
source = source.replace('            self._build_ui()\n', '            self._build_ui()\n            self.protocol("WM_DELETE_WINDOW", self._on_close)\n            self._refresh_oper_badge()\n', 1)
source = source.replace('f"랜선 연결이 안 될 때는 상단의 주황색 [⚡ 5560A IP 자동탐지 + 내 PC IP 자동변경] 버튼을 누르세요!"', 'f"장비의 현재 IP와 실제 랜카드를 선택한 뒤 [LAN 진단]으로 확인하세요."')
source = source.replace('        def _ensure_connected_or_sim(self) -> bool:\n', '        def _ensure_connected_or_sim(self) -> bool:\n            if self._io_busy:\n                self._log("연결 작업이 끝난 뒤 명령을 실행하세요.")\n                return False\n')
# Simulation connection is asynchronous; do not allow the pending command to run early.
source = source.replace('                    self._on_sim_connect_clicked()\n                    return True', '                    self._on_sim_connect_clicked()\n                    return False')
source = source.replace('        def _refresh_oper_badge(self) -> None:\n            if self.is_operating:', '        def _refresh_oper_badge(self) -> None:\n            if self.is_operating is None:\n                self.lbl_output_state.configure(text="출력 상태 미확인 — 장비 화면 확인", bg="#FEF3C7", fg="#92400E")\n                return\n            if self.is_operating:')
source = source.replace('                if is_query or cmd.endswith("?"):', '                if is_query or "?" in cmd:')
async_methods = ''.join((root / f'work/{name}.pyfrag').read_text(encoding='utf-8')
                        for name in ('async_operations', 'current_group_ui', 'multimodel_ui', 'clean_ui'))
for block in re.split(r'(?=^        def )', async_methods, flags=re.M):
    if not block.strip():
        continue
    name = re.match(r'        def (\w+)', block).group(1)
    pattern = rf'^        def {name}\(.*?(?=^        def |^# ====|\Z)'
    source, count = re.subn(pattern, lambda _match: block.rstrip() + '\n\n', source, count=1, flags=re.M | re.S)
    if count == 0:
        source = source.replace('        def _log(self, msg: str) -> None:', block.rstrip() + '\n\n        def _log(self, msg: str) -> None:', 1)
source = source.replace('            ts = datetime.datetime.now().strftime("%H:%M:%S")',
                        '            self.conn_mgr.record_event(msg)\n            ts = datetime.datetime.now().strftime("%H:%M:%S")')
source = source.replace('연결 작업이 끝난 뒤 명령을 실행하세요.', '현재 명령의 완료를 기다려 주세요. 중단하려면 STBY를 누르세요.')
source = source.replace('v5.3.1', 'v5.4.0')
source = source.replace('            status_banner = tk.Frame(container,',
                        '            self._build_current_group_panel(container)\n\n            status_banner = tk.Frame(container,', 1)
# Freeze spreadsheet mutation during output/automatic runs; the worker captures
# a command, and the row must continue to describe that same command.
for name in ('_load_sample_into_sheet', '_on_paste_into_sheet_button', '_handle_excel_paste_at_cell',
             '_handle_excel_cell_edited', '_on_add_blank_row', '_on_clear_sheet', '_recalculate_all_grid_steps'):
    source = re.sub(rf'(^        def {name}\([^\n]+\n)',
                    lambda m: m[1] + '            if self._io_busy or self.seq_running:\n                self._log("표를 수정하려면 실행을 일시중단하고 현재 응답을 기다려 주세요.")\n                return\n',
                    source, count=1, flags=re.M)
cell_header = '        def _handle_excel_cell_edited(self, r_idx: int, vi_c: str, fq_c: str, pf_c: str, nom_c: str) -> None:\n'
source = source.replace(cell_header + '            if self._io_busy or self.seq_running:\n',
                        cell_header + '            if self._io_busy or self.seq_running:\n                if 0 <= r_idx < len(self.seq_steps):\n                    self.excel_grid._update_single_row_visual(r_idx, self.seq_steps[r_idx], is_active=(r_idx == self.seq_current_idx))\n')
source = source.replace('            local_ips = self.conn_mgr.get_local_pc_ipv4_list()',
                        '            self._log(f"통신 기록 자동 저장: {self.conn_mgr.log_path}")\n            self._log(f"실행 Python: {sys.executable} | PyVISA={HAS_PYVISA} | pyserial={HAS_PYSERIAL}")\n            local_ips = self.conn_mgr.get_local_pc_ipv4_list()')
source = re.sub(r'^( +)self\.excel_grid\.render_steps\(self.seq_steps, [^\n]+\n',
                lambda m: m[0] + m[1] + 'self._refresh_current_group_preview()\n', source, flags=re.M)
source = source.replace('                self.excel_grid._update_single_row_visual(r_idx, updated, is_active=(r_idx == self.seq_current_idx))',
                        '                self.excel_grid._update_single_row_visual(r_idx, updated, is_active=(r_idx == self.seq_current_idx))\n                self._refresh_current_group_preview()')
source = source[:source.index('if __name__ == "__main__":')] + '''def main():
    parser = argparse.ArgumentParser(description="Fluke 5560A/5522A/5520A/5080A USB/LAN/RS232/GPIB controller")
    parser.add_argument("--model", choices=list(MODEL_PROFILES), default="5560A")
    parser.add_argument("--cli", action="store_true", help="simulation smoke check")
    parser.add_argument("--diagnose", metavar="IP", help="read-only LAN *IDN? diagnosis")
    parser.add_argument("--port", type=int, default=3490)
    parser.add_argument("--source-ip", default="")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--eol", choices=["CRLF", "CR", "LF"], default="CRLF")
    args = parser.parse_args()
    if args.diagnose:
        mgr = Fluke5560AConnectionManager()
        mgr.expected_model = args.model
        mgr.profile = MODEL_PROFILES[args.model]
        try:
            report = mgr.diagnose_lan(args.diagnose, args.port, args.timeout, args.source_ip, args.eol, args.adapter)
            print(report)
            return 0 if "[진단 성공]" in mgr.last_error else 1
        except Exception as exc:
            print(f"진단 실패: {exc}")
            return 1
    if HAS_TKINTER and not args.cli:
        app = Fluke5560AControllerApp()
        if args.model != "5560A":
            app.var_model.set(args.model)
            app._on_model_changed()
        app.mainloop()
    else:
        run_cli_interactive(args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
# Model-aware parser and UI. The original class names remain import-compatible.
source = source.replace('v5.4.0', 'v6.0.0')
parser_start = source.index('class SmartClipboardSequenceParser:')
parser_end = source.index('@dataclass(frozen=True)', parser_start)
parser_source = source[parser_start:parser_end]
parser_source = parser_source.replace('        breakpoint: bool = False,\n',
                                      '        breakpoint: bool = False,\n        model: str = "5560A",\n')
parser_source = parser_source.replace('    ) -> List[SequenceStep]:',
                                      '        model: str = "5560A",\n    ) -> List[SequenceStep]:')
parser_source = parser_source.replace('                    step_no=step_counter,',
                                      '                    model=model,\n                    step_no=step_counter,')
parser_source = parser_source.replace('Fluke5560ACommandBuilder.build_', 'CalibratorCommandBuilder(model).build_')
source = source[:parser_start] + parser_source + source[parser_end:]
# Keep the clipboard's nine-column mapping; hide only the generated command
# column visually. Model-generated commands and row callbacks remain unchanged.
grid_start = source.index('    class ExcelSheetGrid(')
grid_end = source.index('    class Fluke5560AControllerApp(', grid_start)
grid_source = source[grid_start:grid_end]
grid_source = re.sub(r'        COL_SPECS = \[.*?\n        \]', '''        COL_SPECS = [
            ("행", 44, "center", False), ("제어", 76, "center", False),
            ("전압·전류 조건", 140, "center", True), ("주파수·조건", 105, "center", True),
            ("역률", 130, "center", True), ("목표값", 125, "e", True),
            ("실제 출력", 240, "w", False), ("전송 명령", 230, "w", False),
            ("상태", 110, "center", False),
        ]''', grid_source, count=1, flags=re.S)
grid_source = grid_source.replace('            self._build_grid_skeleton()\n',
    '            self._build_grid_skeleton()\n            self.show_commands = False\n            self.set_command_visibility(False)\n', 1)
grid_source = grid_source.replace('            self.cells_frame.update_idletasks()\n',
    '            self.set_command_visibility(self.show_commands)\n            self.cells_frame.update_idletasks()\n')
grid_source = grid_source.replace('            self.header_canvas.create_window(', '            self.header_window = self.header_canvas.create_window(')
grid_source = grid_source.replace('            self.canvas.bind("<Enter>"',
    '            self.header_frame.grid_columnconfigure(6, weight=1)\n            self.cells_frame.grid_columnconfigure(6, weight=1)\n            self.canvas.bind("<Configure>", self._resize_columns)\n            self.canvas.bind("<Enter>"')
grid_source = grid_source.replace('        def _on_frame_configure(', '''        def set_command_visibility(self, visible):
            self.show_commands = bool(visible)
            cells = [self.header_frame.winfo_children()[7]] + [row["frames"][7] for row in self._row_widgets]
            for cell in cells:
                if visible and not cell.winfo_manager():
                    cell.grid()
                elif not visible and cell.winfo_manager():
                    cell.grid_remove()
            self.header_frame.update_idletasks()
            self.cells_frame.update_idletasks()
            self._resize_columns()
            self._on_frame_configure()

        def _resize_columns(self, _event=None):
            base = sum(spec[1] for index, spec in enumerate(self.COL_SPECS) if index != 7 or getattr(self, "show_commands", False))
            width = max(base, self.canvas.winfo_width())
            self.canvas.itemconfigure(self.canvas_window, width=width)
            self.header_canvas.itemconfigure(self.header_window, width=width)

        def _on_frame_configure(''', 1)
grid_source = grid_source.replace('text="▶시작"', 'text="시작"').replace('bg="#DCFCE7",\n                        fg="#15803D"', 'bg="#F1F5F9",\n                        fg="#475569"')
grid_source = grid_source.replace('"#FDE047"', '"#DBEAFE"').replace('"#FEF9C3"', '"#EFF6FF"')
source = source[:grid_start] + grid_source + source[grid_end:]
app_start = source.index('    class Fluke5560AControllerApp(')
app_source = source[app_start:]
app_source = app_source.replace('Fluke5560ACommandBuilder.build_', 'self._builder().build_')
for name in ('build_sequence_steps', 'build_Single_step_from_fields'):
    # Already model-aware override is left intact.
    app_source = re.sub(rf'(SmartClipboardSequenceParser\.{name}\(\s*)(\w+)=',
                        lambda m: m[0] if m[2] == 'model' else m[1] + 'model=self._selected_model(), ' + m[2] + '=', app_source)
app_source = app_source.replace('            cmb_post.grid(', '            self.combo_current_post = cmb_post\n            cmb_post.configure(values=["AUTO"])\n            cmb_post.grid(')
app_source = app_source.replace('FLUKE 5560A MULTI-PRODUCT CALIBRATOR CONTROLLER', 'FLUKE 5560A / 5522A / 5520A / 5080A')
app_source = app_source.replace('Fluke 5560A Controller', 'Fluke Calibrator Controller')
app_source = app_source.replace('5560A 실제', '장비 실제')
app_source = app_source.replace('(최대 30A 한계 내 정상)', '(선택 모델 범위 확인 완료)')
app_source = app_source.replace('현재 연결된 Fluke 5560A 장비', '현재 연결된 Fluke 교정기')
app_source = app_source.replace('Fluke 5560A 통합 제어 프로그램', 'Fluke 교정기 통합 제어 프로그램')
app_source = app_source.replace('(USB / LAN / GPIB + LAN 진단)', '(USB / LAN / RS232 / GPIB)')
app_source = app_source.replace('self.lbl_conn_badge.configure(text="● DISCONNECTED", bg="#334155", fg="#E2E8F0")',
                                'self.lbl_conn_badge.configure(text="연결 안 됨", bg="#E2E8F0", fg="#64748B")')
app_source = app_source.replace('            self._poll_after_id = self.after(100, self._poll_io_result)\n\n        def _on_refresh_adapters',
    '            self._refresh_connection_controls()\n            self._poll_after_id = self.after(100, self._poll_io_result)\n\n        def _on_refresh_adapters')
app_source = app_source.replace('"#FDE047"', '"#DBEAFE"').replace('bg="#FEF9C3"', 'bg="#F8FAFC"')
app_source = app_source.replace('▶ 엑셀 시트에 총 {len(steps)}개 행 로드 완료 — 각 행의 [▶시작]을 누르거나 아무 셀에나 Ctrl+V로 붙여넣으세요.',
                                '{len(steps)}개 행 준비됨 · 셀을 선택하고 Ctrl+V로 측정값을 붙여넣을 수 있습니다.')
app_source = app_source.replace('            ttk.Label(pad, text="저항(Resistance) 출력 및 리드 보상(ZCOMP) 설정 (0 Ω ~ 1100 MΩ)",',
    '            self.var_resistance_title = tk.StringVar(value="저항 출력 및 리드 보상 (0 Ω ~ 1100 MΩ)")\n            ttk.Label(pad, textvariable=self.var_resistance_title,')
source = source[:app_start] + app_source
source = source.replace('E: 5560A 실제 출력', 'E: 교정기 실제 출력')
source = source.replace('def run_cli_interactive() -> None:', 'def run_cli_interactive(model="5560A") -> None:')
source = source.replace('mgr.scan_and_auto_connect("SIMULATION")', 'mgr.scan_and_auto_connect("SIMULATION", model=model)')
source = source.replace('v6.0.0', 'v6.1.0')
source = source.replace('v6.1.0', 'v6.1.1')
from enhance_units_grid import enhance_source
source = enhance_source(source, root)
from enhance_resolution_grid import enhance_resolution
source = enhance_resolution(source, root)
source = source.replace('v6.2.0', 'v6.8.1')
# A new sheet establishes a new original row order. Edits and appends keep it.
source = re.sub(r'^( +)self\.seq_steps = (?:steps|\[\])\n',
                lambda match: match[0] + match[1] + 'self._original_row_positions = None\n', source, flags=re.M)
source = source.replace('class Fluke5560AConnectionManager:',
    ''.join((root / 'work' / fragment).read_text(encoding='utf-8').rstrip() + '\n\n'
            for fragment in ('dut_support.pyfrag', 'dut_discovery.pyfrag'))
    + 'class Fluke5560AConnectionManager:', 1)
source = source.replace('        def _build_ui(self):', (root / 'work/dut_ui.pyfrag').read_text(encoding='utf-8') + '        def _build_ui(self):', 1)
source = source.replace('("resistance", "저항"), ("power", "전력")):', '("resistance", "저항"), ("power", "전력"), ("dut", "DUT 연결")):', 1)
source = source.replace('            self._on_seq_stop_auto(silent=True)\n            try:\n                self.conn_mgr.disconnect()', '            self._on_seq_stop_auto(silent=True)\n            if not self._dut_close_ready():\n                self.after(200, self._on_close)\n                return\n            try:\n                self.conn_mgr.disconnect()', 1)
from enhance_dut_sequence import enhance_dut_sequence
source = enhance_dut_sequence(source, root)
compile(source, 'fluke_5560a_controller.py', 'exec')
(root / 'outputs/fluke_5560a_controller.py').write_text(source, encoding='utf-8')
print('Built outputs/fluke_5560a_controller.py')
