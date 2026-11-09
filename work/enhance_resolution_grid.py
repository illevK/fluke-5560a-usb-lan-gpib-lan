"""DC V/I import and per-row display resolution; assembled after existing grid extensions."""
import re


def enhance_resolution(source, root):
    def once(old, new):
        nonlocal source
        if old not in source:
            raise ValueError('Resolution build hook missing: ' + old[:100])
        source = source.replace(old, new, 1)

    once('class SmartClipboardSequenceParser:',
         (root / 'work/resolution_values.pyfrag').read_text(encoding='utf-8') + 'class SmartClipboardSequenceParser:')
    once('        pf_row = cls.parse_power_factor_row(line)', '''        dc_row = cls.parse_dc_power_row(line)
        if dc_row is not None:
            return dc_row
        pf_row = cls.parse_power_factor_row(line)''')
    once('        parsed_nom = cls.parse_value_and_unit(nominal_raw)', '''        if not nominal_raw.strip() and freq_cond_raw.strip().upper() in ("", "DC", "0", "0 HZ"):
            dc = cls.parse_dc_power_row(vi_cond_raw)
            if dc:
                nominal_raw, pf_cond_raw = dc[3], "1"
        parsed_nom = cls.parse_value_and_unit(nominal_raw)''')
    start = source.index('    @classmethod\n    def build_sequence_steps(')
    end = source.index('\n\n# ===', start)
    source = source[:start] + (root / 'work/resolution_parser.pyfrag').read_text(encoding='utf-8').rstrip() + source[end:]
    once('    measured_raw: str = ""', '    measured_raw: str = ""\n    resolution_raw: str = ""')
    once('("측정값", 130, "e", True),', '("측정값", 140, "e", True),\n            ("분해능", 120, "e", True),')
    once('return 6 if index == 9 else (index + 1 if index >= 6 else index)',
         'return 6 if index == 10 else 7 if index == 9 else (index + 2 if index >= 6 else index)')
    # Keep the default width close to the existing table after adding one column.
    for old, new in [('("주파수·조건", 105,', '("주파수·조건", 90,'),
                     ('("역률", 130,', '("역률", 100,'),
                     ('("목표값", 125,', '("목표값", 110,'),
                     ('("실제 출력", 240,', '("실제 출력", 180,'),
                     ('("상태", 110,', '("상태", 90,')]:
        once(old, new)
    source = source.replace('self.header_frame.grid_columnconfigure(7,', 'self.header_frame.grid_columnconfigure(8,')
    source = source.replace('self.cells_frame.grid_columnconfigure(7,', 'self.cells_frame.grid_columnconfigure(8,')
    once('        def _bind_measurement_header(self):',
         (root / 'work/resolution_grid_methods.pyfrag').read_text(encoding='utf-8') + '        def _bind_measurement_header(self):')
    once('            self._bind_measurement_selection(r_idx)',
         '            self._bind_measurement_selection(r_idx)\n            self._bind_resolution_entry(r_idx)')
    once('            self._paint_measurement_cell(r_idx)\n            for c_idx, val_str',
         '            self._paint_measurement_cell(r_idx)\n            self._sync_resolution_row(r_idx)\n            for c_idx, val_str')
    once('            if c_idx != self.MEASUREMENT_COLUMN:', '''            if c_idx == self.MEASUREMENT_COLUMN:
                self._begin_measurement_edit(r_idx)
            if c_idx not in (self.MEASUREMENT_COLUMN, self.RESOLUTION_COLUMN):''')
    once('            if c_idx == self.MEASUREMENT_COLUMN:\n                return\n',
         '            if c_idx in (self.MEASUREMENT_COLUMN, self.RESOLUTION_COLUMN):\n                self._commit_result_value(r_idx)\n                return\n')
    once('            if c_idx == self.MEASUREMENT_COLUMN:\n                if not self._paste_measurements',
         '            if c_idx == self.RESOLUTION_COLUMN:\n                self._paste_resolutions(clip_text, r_idx)\n                return "break"\n            if c_idx == self.MEASUREMENT_COLUMN:\n                if not self._paste_measurements')
    once('return "\\r\\n".join(step.measured_raw for step in self._steps[lo:hi + 1]) + "\\r\\n"',
         'return "\\r\\n".join(self._measurement_display(row) for row in range(lo, hi + 1)) + "\\r\\n"')
    once('            text = self.measurement_selection_text()', '''            try:
                text = self.measurement_selection_text()
            except ValueError as exc:
                self._report_resolution_error(row, str(exc))
                return "break"''')
    once('                self._row_widgets[row]["measurement_var"].set(value)\n            return True',
         '                self._row_widgets[row]["measurement_var"].set(value)\n                self._refresh_resolution_value(row, force=True)\n            return True')
    # Preserve the unrounded source while status repaints use the formatted value.
    once('            if measured.get() != st.measured_raw:', '            if measured.get() != st.measured_raw and self.focus_get() is not entries[9]:')
    source = source.replace('updated.measured_raw = st.measured_raw', 'updated.measured_raw = st.measured_raw\n                updated.resolution_raw = st.resolution_raw')
    source = source.replace('rebuilt.measured_raw = st.measured_raw', 'rebuilt.measured_raw = st.measured_raw\n                rebuilt.resolution_raw = st.resolution_raw')
    source = source.replace('self.seq_steps[target_idx].measured_raw = st.measured_raw',
                            'self.seq_steps[target_idx].measured_raw = st.measured_raw\n                    self.seq_steps[target_idx].resolution_raw = st.resolution_raw')
    once('"⚠ 설정 확인", st.breakpoint, st.measured_raw)', '"⚠ 설정 확인", st.breakpoint, st.measured_raw, st.resolution_raw)')
    once('            if st.func_type == "PF":', '''            if vi_c != st.vi_cond_raw and fq_c.strip().upper() in ("", "DC", "0", "0 HZ"):
                try:
                    dc = SmartClipboardSequenceParser.parse_dc_power_row(vi_c)
                except ValueError as exc:
                    invalid = SequenceStep(st.step_no, vi_c, fq_c, pf_c, nom_c, "ERROR", "", str(exc), "⚠ 설정 확인", st.breakpoint, st.measured_raw, st.resolution_raw)
                    self.seq_steps[r_idx] = invalid
                    self.excel_grid._update_single_row_visual(r_idx, invalid)
                    self.var_active_row_banner.set(f"{r_idx + 1}행 출력 조건 확인: {exc}")
                    self._log(str(exc))
                    return
                if dc:
                    nom_c, pf_c = dc[3], "1"
            if st.func_type == "PF":''')
    once('            if start_col_idx == self.excel_grid.MEASUREMENT_COLUMN:', '''            if start_col_idx == self.excel_grid.RESOLUTION_COLUMN:
                self.excel_grid._paste_resolutions(clip_text, start_row_idx)
                return
            if start_col_idx == self.excel_grid.MEASUREMENT_COLUMN:''')
    once('            if not replace_all and self.excel_grid.selected_col_idx == self.excel_grid.MEASUREMENT_COLUMN:',
         '            if not replace_all and self.excel_grid.selected_col_idx in (self.excel_grid.MEASUREMENT_COLUMN, self.excel_grid.RESOLUTION_COLUMN):')
    once('self._handle_excel_paste_at_cell(text, self.excel_grid.selected_row_idx, self.excel_grid.MEASUREMENT_COLUMN)',
         'self._handle_excel_paste_at_cell(text, self.excel_grid.selected_row_idx, self.excel_grid.selected_col_idx)')
    once('                    pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)',
         '                    pf, _resolution = SmartClipboardSequenceParser.parse_input_row(ln)')
    once('                for offset, (vi_c, fq_c, pf_c, nom_c) in enumerate(parsed_rows):',
         '                for offset, (vi_c, fq_c, pf_c, nom_c) in enumerate(parsed_rows):')
    once('                    if target_idx < len(self.seq_steps):\n                        self.seq_steps[target_idx] = new_step',
         '                    _, new_step.resolution_raw = SmartClipboardSequenceParser.parse_input_row(lines[offset])\n                    if target_idx < len(self.seq_steps):\n                        self.seq_steps[target_idx] = new_step')
    once('            self.excel_grid.pack(fill="both", expand=True, pady=(0, 8))', '''            self.excel_grid.on_resolution_error_callback = self._show_resolution_error
            self.excel_grid.pack(fill="both", expand=True, pady=(0, 8))''')
    once('        def _build_current_group_panel(self, container):', '''        def _show_resolution_error(self, text):
            self.var_active_row_banner.set(text)
            self._log(text)

        def _build_current_group_panel(self, container):''')
    return source
