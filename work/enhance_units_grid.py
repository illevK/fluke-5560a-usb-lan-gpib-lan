"""Build-time extensions for SI inputs and a separate editable result column."""
import re


def replace_method(source, name, block):
    pattern = rf'^        def {name}\(.*?(?=^        def |^        @|^# ====|\Z)'
    source, count = re.subn(pattern, lambda _: block.rstrip() + '\n\n', source, count=1, flags=re.M | re.S)
    if count != 1:
        raise ValueError(f'Method not found: {name}')
    return source


def enhance_source(source, root):
    source = source.replace('import re\n', 'import re\nfrom decimal import Decimal, DecimalException\n', 1)
    source = source.replace('class Fluke5560ACommandBuilder:',
        (root / 'work/si_units.pyfrag').read_text(encoding='utf-8') + 'class Fluke5560ACommandBuilder:', 1)
    start = source.index('    @staticmethod\n    def normalize_unit(')
    end = source.index('    @staticmethod\n    def decode_signed_pf', start)
    source = source[:start] + '''    @staticmethod
    def normalize_unit(unit):
        return SIUnits.normalize(unit)

    @classmethod
    def unit_multiplier(cls, unit):
        return SIUnits.split(unit)[1]

''' + source[end:]
    source = source.replace('volts = value * cls.unit_multiplier(norm_unit)', 'volts = SIUnits.to_base(value, norm_unit, "V")')
    source = source.replace('target_a = input_value * cls.unit_multiplier(norm_unit)', 'target_a = SIUnits.to_base(input_value, norm_unit, "A")')
    source = source.replace('ohms = value * cls.unit_multiplier(norm_unit)', 'ohms = SIUnits.to_base(value, norm_unit, "OHM")')
    source = source.replace('f"OUT {value:.8g} {norm_unit},', 'f"OUT {volts:.12g} V,')
    source = source.replace('f"OUT {value:.8g} {norm_unit};', 'f"OUT {ohms:.12g} OHM;')
    source = source.replace('current_or_power_val * cls.unit_multiplier(power_unit)', 'SIUnits.to_base(current_or_power_val, power_unit, "W")')

    start = source.index('    VI_SLASH_PATTERN =', source.index('class SmartClipboardSequenceParser:'))
    end = source.index('    @classmethod\n    def _is_header_line', start)
    source = source[:start] + '''    VI_SLASH_PATTERN = SIUnits.VI
    TOKEN_FINDER = SIUnits.TOKEN
    FREQ_UNITS = set(SIUnits.units("HZ"))
    POWER_UNITS = set(SIUnits.units("W"))
    VOLTAGE_UNITS = set(SIUnits.units("V"))
    CURRENT_UNITS = set(SIUnits.units("A"))
    RESISTANCE_UNITS = set(SIUnits.units("OHM"))
    OUTPUT_UNITS = POWER_UNITS | VOLTAGE_UNITS | CURRENT_UNITS | RESISTANCE_UNITS

    @classmethod
    def parse_value_and_unit(cls, text):
        match = SIUnits.VALUE.fullmatch(SIUnits.clean(text))
        if match:
            try:
                return SIUnits.number(match[1]), SIUnits.normalize(match[2])
            except ValueError:
                pass
        return None

''' + source[end:]
    start = source.index('class SmartClipboardSequenceParser:')
    end = source.index('@dataclass(frozen=True)', start)
    parser = source[start:end]
    for old, new in [('("UV", "MV", "V", "KV")', 'cls.VOLTAGE_UNITS'),
                     ('("UA", "MA", "A")', 'cls.CURRENT_UNITS'),
                     ('("OHM", "KOHM", "MOHM", "GOHM")', 'cls.RESISTANCE_UNITS')]:
        parser = parser.replace(old, new)
    parser = parser.replace('line = raw_line.strip()', 'line = SIUnits.clean(raw_line).strip()')
    parser = parser.replace('        parsed_nom = cls.parse_value_and_unit(nominal_raw)',
        '        vi_cond_raw = SIUnits.clean(vi_cond_raw)\n        freq_cond_raw = SIUnits.clean(freq_cond_raw)\n        parsed_nom = cls.parse_value_and_unit(nominal_raw)')
    parser = re.sub(r'float\((m\w*\.group\(\d\)|num_str)\)', r'SIUnits.number(\1)', parser)
    parser = parser.replace('voltage_v = v_val * Fluke5560ACommandBuilder.unit_multiplier(v_unit)', 'voltage_v = SIUnits.to_base(v_val, v_unit, "V")')
    parser = parser.replace('cond_current_a = i_val * Fluke5560ACommandBuilder.unit_multiplier(i_unit)', 'cond_current_a = SIUnits.to_base(i_val, i_unit, "A")')
    parser = parser.replace('freq_hz = f_val * Fluke5560ACommandBuilder.unit_multiplier(f_unit)', 'freq_hz = SIUnits.to_base(f_val, f_unit, "HZ")\n                    if freq_hz < 0:\n                        raise ValueError("주파수는 0 이상이어야 합니다.")')
    # Invalid frequency must not quietly turn an AC request into DC.
    parser = parser.replace('            except ValueError:\n                continue\n\n        if nom_unit', '            except ValueError as exc:\n                raise ValueError(f"주파수 입력 오류: {freq_cond_raw}") from exc\n\n        if nom_unit', 1)
    parser = parser.replace('    @classmethod\n    def _is_header_line',
        (root / 'work/power_factor_parser.pyfrag').read_text(encoding='utf-8') + '    @classmethod\n    def _is_header_line', 1)
    parser = parser.replace('        parsed_nom = cls.parse_value_and_unit(nominal_raw)', '''        if not nominal_raw.strip() and freq_cond_raw.strip().upper() in ("", "DC", "0", "0 HZ"):
            dc = cls.parse_dc_power_row(vi_cond_raw)
            if dc:
                nominal_raw, pf_cond_raw = dc[3], "1"
        parsed_nom = cls.parse_value_and_unit(nominal_raw)''', 1)
    parser = parser.replace('        if parsed_nom is None:\n', '''        if parsed_nom is None and cls.VI_SLASH_PATTERN.fullmatch(vi_cond_raw.strip()):
            return cls.build_power_factor_step(step_no, vi_cond_raw, freq_cond_raw, nominal_raw,
                                               use_coil, coil_turns, breakpoint, model)
        if parsed_nom is None:
''', 1)
    parser = re.sub(r'            pf_num_match = .*?            dpf_val, dpf_lead_lag, pf_display = [^\n]+',
        '            dpf_val, dpf_lead_lag, pf_display = cls.parse_power_factor(pf_cond_raw if pf_cond_raw.strip() not in ("", "-") else "1")', parser, count=1, flags=re.S)
    parser = parser.replace('        m_vi = cls.VI_SLASH_PATTERN.search(line)', '''        if "\\t" in raw_line:
            return cls.parse_tabular_row(SIUnits.clean(raw_line))
        dc_row = cls.parse_dc_power_row(line)
        if dc_row is not None:
            return dc_row
        m_vi = cls.VI_SLASH_PATTERN.search(line)''', 1)
    parser = parser.replace('''            calc_current_a = target_power_w / (voltage_v * dpf_val)
            if cond_current_a is not None and abs(cond_current_a - calc_current_a) <= max(1e-4, 0.02 * cond_current_a):
                target_current_a = cond_current_a
            else:
                target_current_a = calc_current_a''', '''            if voltage_v == 0:
                raise ValueError("전력 조건의 전압은 0이 아닌 값으로 입력하세요.")
            factor = dpf_val if has_freq else 1.0
            if cond_current_a is None:
                target_current_a = target_power_w / (voltage_v * factor)
            else:
                expected_power = voltage_v * cond_current_a * factor
                if not math.isclose(expected_power, target_power_w, rel_tol=0.02):
                    detail = f"조건 계산값 {expected_power:.9g} W / 명목값 {nominal_raw.strip()}"
                    return SequenceStep(step_no, vi_display, freq_display, pf_display, nominal_raw.strip(),
                                        "ERROR", "", detail, "⚠ 전력 불일치", breakpoint)
                target_current_a = cond_current_a''', 1)
    parser = parser.replace('        m_vi = cls.VI_SLASH_PATTERN.search(line)', '''        pf_row = cls.parse_power_factor_row(line)
        if pf_row is not None:
            return pf_row
        m_vi = cls.VI_SLASH_PATTERN.search(line)''', 1)
    parser = parser.replace('''            if power_nom_str:
                return (vi_cond_str, freq_str, pf_str, power_nom_str)''', '''            if power_nom_str:
                return (vi_cond_str, freq_str, pf_str, power_nom_str)
            if pf_match and any(
                    SIUnits.split(token[2])[0] == "HZ" for token in cls.TOKEN_FINDER.finditer(rest)):
                _, _, display = cls.parse_power_factor(pf_str)
                return vi_cond_str, freq_str, display, display
            dc = cls.parse_dc_power_row(vi_cond_str)
            return vi_cond_str, freq_str, "1", dc[3]''', 1)
    parser = parser.replace('            pf_str = pf_match.group(1) if pf_match else "1"', '''            pf_str = pf_match.group(1) if pf_match else "1"
            direction = re.search(r"\\b(LEAD|LAG)\\b", rem_str, re.I)
            if direction:
                pf_str += " " + direction[1]''')
    source = source[:start] + parser + source[end:]
    source = source.replace('DPF {dpf_val:.2f}', 'DPF {dpf_val:.6g}')

    # Drop-downs use SI case; all generated voltage/resistance commands use base units.
    for old, new in [('values=["uV", "mV", "V", "kV"]', 'values=SIUnits.units("V")'),
                     ('values=["uA", "mA", "A"]', 'values=SIUnits.units("A")'),
                     ('values=["OHM", "KOHM", "MOHM", "GOHM"]', 'values=SIUnits.units("OHM")'),
                     ('values=["W", "KW", "A"]', 'values=SIUnits.units("W") + ("A",)')]:
        source = source.replace(old, new)
    source = source.replace('value="KOHM"', 'value="kOHM"')
    for field, unit in [('v_freq', 'HZ'), ('i_freq', 'HZ'), ('p_freq', 'HZ'), ('p_volt', 'V')]:
        source = source.replace(f'float(self.var_{field}.get())', f'SIUnits.input_value(self.var_{field}.get(), "{unit}")')
    source = source.replace('W/kW', 'W 계열')

    source = source.replace('    breakpoint: bool = False\n', '    breakpoint: bool = False\n    measured_raw: str = ""\n', 1)
    start = source.index('    class ExcelSheetGrid(')
    end = source.index('    class Fluke5560AControllerApp(', start)
    grid = source[start:end]
    grid = grid.replace('("상태", 110, "center", False),', '("상태", 110, "center", False),\n            ("측정값", 130, "e", True),')
    grid = grid.replace('            self._build_grid_skeleton()', '            self._steps = []\n            self._rendering_measurements = False\n            self.show_commands = False\n            self.show_measurements = True\n            self._init_measurement_selection()\n            self._build_grid_skeleton()\n            self._bind_measurement_header()', 1)
    grid = grid.replace('column=c_idx,', 'column=self._display_column(c_idx),')
    grid = grid.replace('grid_columnconfigure(6,', 'grid_columnconfigure(7,')
    grid = grid.replace('            while len(self._row_widgets) > len(steps):', '            if self._steps is not steps or len(self._row_widgets) != len(steps):\n                self._reset_measurement_selection()\n            self._steps = steps\n            while len(self._row_widgets) > len(steps):')
    grid = grid.replace('        def _update_single_row_visual(self, r_idx: int, st: SequenceStep, is_active: bool = False) -> None:\n',
        '        def _update_single_row_visual(self, r_idx: int, st: SequenceStep, is_active: bool = False) -> None:\n            self._steps[r_idx] = st\n')
    grid = grid.replace('            for c_idx, val_str in col_vals.items():', '''            self._refresh_measurement_value(r_idx, preserve_entry=True)
            entries[9].configure(bg=row_bg, fg=row_fg, font=row_font)
            rw["measurement_base_colors"] = (row_bg, row_fg, border_col)
            self._paint_measurement_cell(r_idx)
            for c_idx, val_str in col_vals.items():''')
    grid = grid.replace('            self._row_widgets.append({"frames": row_frames, "entries": entries, "labels": labels})', '''            measured = tk.StringVar(master=self, value="")
            entries[9].configure(textvariable=measured)
            self._row_widgets.append({"frames": row_frames, "entries": entries, "labels": labels, "measurement_var": measured})
            measured.trace_add("write", lambda *_args, r=r_idx: self._measurement_changed(r))
            self._bind_measurement_selection(r_idx)''')
    grid = grid.replace('            self.on_row_select_callback(r_idx)\n\n        def _commit_cell', '            if c_idx != self.MEASUREMENT_COLUMN:\n                self.on_row_select_callback(r_idx)\n            elif not self._measurement_drag_active and not self._measurement_contains(r_idx):\n                self._set_measurement_selection(r_idx, r_idx)\n\n        def _commit_cell')
    grid = grid.replace('        def _commit_cell(self, r_idx: int, c_idx: int) -> None:\n', '        def _commit_cell(self, r_idx: int, c_idx: int) -> None:\n            if c_idx == self.MEASUREMENT_COLUMN:\n                if 0 <= r_idx < len(self._steps):\n                    self._refresh_measurement_value(r_idx)\n                return\n')
    grid = grid.replace('                self.scroll_to_row(target_r)\n', '                self.scroll_to_row(target_r)\n            return "break"\n')
    grid = grid.replace('            target_c = c_idx + dc\n', '            target_c = c_idx + dc\n            if c_idx == self.MEASUREMENT_COLUMN:\n                self._set_measurement_selection(target_r, target_r)\n')
    grid = grid.replace('            self.on_paste_text_callback(clip_text, start_row_idx=r_idx, start_col_idx=c_idx)', '''            if c_idx == self.MEASUREMENT_COLUMN:
                if not self._paste_measurements(clip_text, r_idx):
                    self.bell()
                return "break"
            self.on_paste_text_callback(clip_text, start_row_idx=r_idx, start_col_idx=c_idx)''')
    methods = (root / 'work/measurement_grid_methods.pyfrag').read_text(encoding='utf-8')
    for name in ('set_command_visibility', '_resize_columns'):
        match = re.search(rf'^        def {name}\(.*?(?=^        def |^        @|\Z)', methods, re.M | re.S)
        grid = replace_method(grid, name, match[0])
        methods = methods[:match.start()] + methods[match.end():]
    grid = grid.replace('        def _build_grid_skeleton(self)', methods + '        def _build_grid_skeleton(self)', 1)
    source = source[:start] + grid + source[end:]
    source = source.replace('                updated.status = st.status', '                updated.measured_raw = SIUnits.with_nominal_unit(st.measured_raw, st.nominal_raw)\n                if updated.scpi_cmd and st.scpi_cmd:\n                    updated.status = st.status')
    source = source.replace('            if (vi_c, fq_c, pf_c, nom_c) ==', '''            if vi_c != st.vi_cond_raw and fq_c.strip().upper() in ("", "DC", "0", "0 HZ"):
                try:
                    dc = SmartClipboardSequenceParser.parse_dc_power_row(vi_c)
                except ValueError as exc:
                    invalid = SequenceStep(st.step_no, vi_c, fq_c, pf_c, nom_c, "ERROR", "", str(exc),
                                           "⚠ 설정 확인", st.breakpoint, st.measured_raw)
                    self.seq_steps[r_idx] = invalid
                    self.excel_grid._update_single_row_visual(r_idx, invalid)
                    self._refresh_current_group_preview()
                    self._log(f"[#{st.step_no}행 셀 입력 오류] {exc}")
                    return
                if dc:
                    nom_c, pf_c = dc[3], "1"
            if (vi_c, fq_c, pf_c, nom_c) ==''', 1)
    source = source.replace('            if (vi_c, fq_c, pf_c, nom_c) ==', '''            if st.func_type == "PF":
                if pf_c != st.pf_cond_raw and nom_c == st.nominal_raw:
                    nom_c = pf_c
                elif nom_c != st.nominal_raw:
                    pf_c = nom_c
            if (vi_c, fq_c, pf_c, nom_c) ==''', 1)
    source = source.replace('                rebuilt_steps.append(rebuilt)', '                rebuilt.measured_raw = st.measured_raw\n                rebuilt_steps.append(rebuilt)')
    source = source.replace('                pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)', '''                try:
                    pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)
                except ValueError as exc:
                    self._log(f"[붙여넣기 입력 오류] {exc}")
                    messagebox.showerror("붙여넣기 값 오류", str(exc))
                    return''')
    source = source.replace('                self._log(f"[#{st.step_no}행 셀 입력 오류] {e}")', '''                invalid = SequenceStep(st.step_no, vi_c, fq_c, pf_c, nom_c, "ERROR", "", str(e), "⚠ 설정 확인", st.breakpoint, st.measured_raw)
                self.seq_steps[r_idx] = invalid
                self.excel_grid._update_single_row_visual(r_idx, invalid)
                self._refresh_current_group_preview()
                self._log(f"[#{st.step_no}행 셀 입력 오류] {e}")''')
    source = source.replace('        def _handle_excel_paste_at_cell(self, clip_text: str, start_row_idx: int, start_col_idx: int) -> None:\n', '''        def _handle_excel_paste_at_cell(self, clip_text: str, start_row_idx: int, start_col_idx: int) -> None:
            if start_col_idx == self.excel_grid.MEASUREMENT_COLUMN:
                if not self.excel_grid._paste_measurements(clip_text, start_row_idx):
                    self._log("측정값은 한 열만 복사해서 붙여넣으세요.")
                return
''')
    source = source.replace('        def _on_paste_into_sheet_button(self, replace_all: bool = False) -> None:\n', '''        def _on_paste_into_sheet_button(self, replace_all: bool = False, *, append=False) -> None:
            if not (replace_all or append) and self.excel_grid.selected_col_idx == self.excel_grid.MEASUREMENT_COLUMN:
                try:
                    text = self.clipboard_get()
                except tk.TclError:
                    return
                self._handle_excel_paste_at_cell(text, self.excel_grid.selected_row_idx, self.excel_grid.MEASUREMENT_COLUMN)
                return
''')
    # Preserve results on a cell/column correction, but explicit replacement/new rows start blank.
    source = source.replace('                        breakpoint=st.breakpoint,\n                    )', '                        breakpoint=st.breakpoint,\n                    )\n                    self.seq_steps[target_idx].measured_raw = SIUnits.with_nominal_unit(st.measured_raw, st.nominal_raw)')
    # Retain empty tab cells so a missing rightmost target cannot turn into a different output.
    source = source.replace('lines = [ln.strip() for ln in clip_text.splitlines() if ln.strip()]',
                            'lines = [ln for ln in clip_text.splitlines() if ln.strip()]', 1)
    source = source.replace('''            if replace_all:
                self.seq_steps = []
            self._handle_excel_paste_at_cell(clip_text, start_row_idx=start_r, start_col_idx=2)''',
        '''            self._handle_excel_paste_at_cell(clip_text, start_row_idx=start_r,
                                             start_col_idx=2, replace_all=replace_all, append=append)''', 1)
    source = source.replace('start_col_idx: int) -> None:', 'start_col_idx: int, *, replace_all=False, append=False) -> None:', 1)
    source = source.replace('            parsed_rows: List[Tuple[str, str, str, str]] = []',
        '            if append:\n                start_row_idx = len(self.seq_steps)\n'
        '            parsed_rows: List[Tuple[str, str, str, str]] = []', 1)
    source = source.replace('''            for ln in lines:
                try:
                    pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)''',
        '''            for line_no, ln in enumerate(lines, 1):
                try:
                    pf = SmartClipboardSequenceParser.parse_line_to_fields(ln)
                    if append and pf is None and not SmartClipboardSequenceParser._is_header_line(ln):
                        raise ValueError(f"붙여넣은 {line_no}행의 출력 조건을 인식할 수 없습니다.")''', 1)
    source = source.replace('''            if len(parsed_rows) == len(lines) and parsed_rows:
                if start_row_idx == 0 and len(parsed_rows) > 1:
                    self.seq_steps = []

                for offset''', '''            tabular = any("\\t" in line for line in lines)
            if parsed_rows and (append or tabular or len(parsed_rows) == len(lines)):
                new_steps = []
                for offset''', 1)
    source = source.replace('''                        break

                    if target_idx < len(self.seq_steps):
                        self.seq_steps[target_idx] = new_step
                    else:
                        self.seq_steps.append(new_step)

                for i, st''', '''                        return
                    new_steps.append(new_step)

                if replace_all or (not append and start_row_idx == 0 and len(new_steps) > 1):
                    self.seq_steps = []
                    self.seq_current_idx = self.seq_paused_idx = -1
                    self.progress_var.set(0)
                self.seq_steps[start_row_idx:start_row_idx + len(new_steps)] = new_steps
                if append or replace_all:
                    self.var_seq_start_row.set("1")
                if append:
                    self.var_active_row_banner.set(f"{len(new_steps)}행 추가 · 실행 범위 1~{len(self.seq_steps)}행")
                invalid_rows = [str(step.step_no) for step in new_steps if not step.scpi_cmd]
                if invalid_rows:
                    text = "전력 조건과 명목값 확인 · " + ", ".join(invalid_rows) + "행"
                    self.var_active_row_banner.set(text)
                    self._log(text)
                for i, st''', 1)
    source = source.replace('            for offset, ln in enumerate(lines):\n                target_idx',
        '            if tabular or replace_all or append:\n                return\n            for offset, ln in enumerate(lines):\n                ln = ln.strip()\n                target_idx', 1)
    source = source.replace('v6.1.1', 'v6.2.0')
    return source
