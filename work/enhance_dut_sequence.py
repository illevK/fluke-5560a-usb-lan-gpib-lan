"""Add linked DUT acquisition to the generated controller."""
import re


def enhance_dut_sequence(source, root):
    def patch_method(name, old, new):
        nonlocal source
        start = source.index(f"        def {name}(", source.index("    class Fluke5560AControllerApp("))
        end = source.find("\n        def ", start + 1)
        if end < 0:
            end = len(source)
        method = source[start:end]
        if method.count(old) != 1:
            raise ValueError(f"DUT sequence integration anchor changed in {name}: {old!r}")
        source = source[:start] + method.replace(old, new, 1) + source[end:]

    def add_guard(name, guard):
        nonlocal source
        header = re.search(rf"^        def {name}\([^\n]*\):[^\n]*\n", source, re.M)
        if not header:
            # Some inherited sequence handlers have return annotations.
            header = re.search(rf"^        def {name}\([^\n]*\) -> [^\n]+:\n", source, re.M)
        if not header:
            raise ValueError(f"DUT sequence integration method missing: {name}")
        source = source[:header.end()] + guard + source[header.end():]

    patch_method("__init__", "            self.seq_after_id: Optional[str] = None\n",
        "            self.seq_after_id: Optional[str] = None\n"
        "            self._seq_dut_run = False\n"
        "            self._seq_dut_pending = None\n"
        "            self._seq_dut_reading = False\n"
        "            self._seq_dut_resume_required = False\n"
        "            self._seq_close_standby = False\n"
        "            self._standby_in_progress = False\n")
    patch_method("_build_sequence_tab", "            self.var_seq_auto_oper = tk.BooleanVar(value=True)\n",
        "            self.var_seq_auto_oper = tk.BooleanVar(value=True)\n"
        "            self.var_seq_dut_capture = tk.BooleanVar(value=True)\n")
    patch_method("_build_sequence_tab",
        '            ttk.Checkbutton(controls, text="자동 출력", variable=self.var_seq_auto_oper).pack(side="left", padx=6)\n',
        '            ttk.Checkbutton(controls, text="자동 출력", variable=self.var_seq_auto_oper).pack(side="left", padx=6)\n'
        '            ttk.Checkbutton(controls, text="DUT 자동 기록", variable=self.var_seq_dut_capture).pack(side="left", padx=6)\n')

    start = source.index("        def _execute_sequence_step(")
    end = source.index("\n        def ", start + 1)
    fragment = (root / "work/dut_sequence_integration.pyfrag").read_text(encoding="utf-8").rstrip()
    source = source[:start] + fragment + "\n" + source[end:]

    # Freeze output-affecting table edits during a manually started linked row.
    app_start = source.index("    class Fluke5560AControllerApp(")
    source = source[:app_start] + source[app_start:].replace(
        "if self._io_busy or self.seq_running:",
        "if self._io_busy or self.seq_running or self._seq_dut_pending is not None:",
    )
    sequence_guard = (
        "            if self._sequence_dut_reserved() or self._seq_dut_reading:\n"
        "                self._log('DUT 기록을 중단하거나 현재 조회가 끝난 뒤 실행하세요.')\n"
        "                return\n"
    )
    for handler in ("_on_seq_output_selected", "_on_seq_step_next", "_on_seq_step_prev",
                    "_start_filtered_sequence", "_on_reset_clicked"):
        add_guard(handler, sequence_guard)

    patch_method("_start_filtered_sequence",
        '                if not math.isfinite(dwell) or dwell < .2:\n'
        '                    raise ValueError("유지시간은 0.2초 이상의 유한한 숫자여야 합니다.")\n',
        '                if not .2 <= dwell <= 86_400:\n'
        '                    raise ValueError("유지시간은 0.2~86400초 사이여야 합니다.")\n')
    patch_method("_start_filtered_sequence", "            self.seq_running = True\n",
        "            self._seq_dut_run = self._sequence_dut_enabled() and any(\n"
        "                self.seq_steps[i].func_type in self._DUT_SEQUENCE_QUANTITIES for i in plan)\n"
        "            self._seq_dut_resume_required = False\n"
        "            self.seq_running = True\n"
        "            self._dut_sync_controls()\n")
    add_guard("_on_seq_resume_from_paused",
        "            if (self._seq_dut_resume_required and self.var_seq_dut_capture.get()\n"
        "                    and not self._sequence_dut_enabled()):\n"
        "                self._log('DUT를 다시 연결한 뒤 중단된 행부터 재개하세요.')\n"
        "                return\n")
    patch_method("_run_next_auto_step", "            def completed():\n",
        "            linked = self._seq_dut_run and self.seq_steps[idx].func_type in self._DUT_SEQUENCE_QUANTITIES\n"
        "            def completed():\n")
    patch_method("_run_next_auto_step", "                self._send_and_log(\"STBY\", on_done=finished)\n",
        "                self._on_seq_stop_auto(silent=True)\n"
        "                self._send_and_log(\"STBY\", on_done=finished)\n")
    patch_method("_run_next_auto_step",
        "                    self.excel_grid._update_single_row_visual(idx, self.seq_steps[idx], is_active=False)\n"
        "                    return\n"
        "                self.seq_after_id = self.after(dwell_ms, lambda: self._run_next_auto_step(idx + 1, stop_idx, dwell_ms))\n"
        "            if not self._execute_sequence_step(idx, on_complete=completed):\n"
        "                self.seq_running = False\n",
        "                    self.excel_grid._update_single_row_visual(idx, self.seq_steps[idx], is_active=False)\n"
        "                    if self._seq_dut_run:\n"
        "                        self._seq_dut_resume_required = True\n"
        "                        self._on_stby_clicked()\n"
        "                    return\n"
        "                self.seq_after_id = self.after(0 if linked else dwell_ms,\n"
        "                    lambda: self._run_next_auto_step(idx + 1, stop_idx, dwell_ms))\n"
        "            if not self._execute_sequence_step(idx, on_complete=completed, dwell_ms=dwell_ms):\n"
        "                if self._seq_dut_run and self.conn_mgr.info.connected:\n"
        "                    self._on_stby_clicked()\n"
        "                else:\n"
        "                    self._on_seq_stop_auto(silent=True)\n")
    patch_method("_run_next_auto_step", "                self._show_report(\"전류 그룹 확인\", str(exc))\n",
        "                if self._seq_dut_run:\n"
        "                    self._on_stby_clicked()\n"
        "                self._show_report(\"전류 그룹 확인\", str(exc))\n")

    add_guard("_on_seq_stop_auto",
        "            had_linked_capture = self._sequence_dut_reserved()\n"
        "            if self._seq_dut_pending is not None or (had_linked_capture and not silent):\n"
        "                self._seq_dut_resume_required = True\n"
        "            self._seq_dut_run = False\n"
        "            self._seq_dut_pending = None\n"
        "            if self._seq_dut_reading:\n"
        "                self.dut_session.cancel.set()\n")
    patch_method("_on_seq_stop_auto",
        '                self._log(f"[일시중단] #{self.seq_current_idx + 1}번 행에서 순차 실행을 중단했습니다.")\n',
        '                self._log(f"[일시중단] #{self.seq_current_idx + 1}번 행에서 순차 실행을 중단했습니다.")\n'
        '            if had_linked_capture:\n'
        '                self._dut_sync_controls()\n'
        '                if not silent and self.conn_mgr.info.connected:\n'
        '                    self._on_stby_clicked()\n')
    add_guard("_on_disconnect_clicked",
        "            if self._sequence_dut_reserved():\n"
        "                if self._io_busy:\n"
        "                    self._on_stby_clicked()\n"
        "                    return\n"
        "                self._on_seq_stop_auto(silent=True)\n")
    add_guard("_on_close",
        "            if self._sequence_dut_reserved() and self.conn_mgr.info.connected:\n"
        "                self._seq_close_standby = True\n"
        "                self._on_stby_clicked()\n"
        "                self.after(200, self._on_close)\n"
        "                return\n")
    patch_method("_on_close", "                self.conn_mgr.request_abort()\n",
        "                if not (self._seq_close_standby or self._standby_in_progress):\n"
        "                    self.conn_mgr.request_abort()\n")
    patch_method("_on_stby_clicked", "            if self._io_busy:\n",
        "            if self._standby_in_progress:\n"
        '                self._log("STBY가 이미 전송되었으며 완료 응답을 기다리고 있습니다.")\n'
        "                return\n"
        "            if self._io_busy:\n")

    # Existing STBY/disconnect handlers release the reservation before I/O.
    io_guard = (
        "            if stop_sequence and self._sequence_dut_reserved():\n"
        "                self._log('DUT 기록을 중단한 뒤 다른 교정기 명령을 실행하세요.')\n"
        "                return False\n"
    )
    patch_method("_start_io", "on_error=None, stop_sequence=True):\n",
        "on_error=None, stop_sequence=True, *, standby=False):\n")
    add_guard("_start_io",
        "            if (self._seq_close_standby or self._dut_closing) and not standby:\n"
        "                return False\n" + io_guard)
    patch_method("_start_io", "            self._io_busy = True\n",
        "            self._io_busy = True\n"
        "            self._standby_in_progress = standby\n")
    patch_method("_poll_io_result", "                self._io_busy = False\n",
        "                self._io_busy = False\n"
        "                self._standby_in_progress = False\n")
    add_guard("_send_and_log",
        '            standby = cmd == "STBY" and not auto_oper\n'
        '            if (self._seq_close_standby or self._dut_closing) and not standby:\n'
        '                return False\n' + io_guard)
    patch_method("_send_and_log", "            self.conn_mgr.operation_timeout = self._get_operation_timeout()\n",
        "            if not standby:\n"
        "                self.conn_mgr.operation_timeout = self._get_operation_timeout()\n")
    patch_method("_send_and_log",
        '                if state not in ("0", "1"):\n'
        '                    self.conn_mgr.disconnect(standby=False)\n'
        '                    raise InstrumentProtocolError(f"출력 상태 확인 실패: {state}")\n',
        '                if state not in ("0", "1"):\n'
        '                    error = InstrumentProtocolError(f"출력 상태 확인 실패: {state}")\n'
        '                    with self.conn_mgr._lock:\n'
        '                        self.conn_mgr._fail_control_session("OPER? 응답 확인", error)\n'
        '                    raise error\n'
        '                if standby and state != "0":\n'
        '                    raise InstrumentProtocolError("STBY 요청 후에도 OPER 상태입니다. 본체에서 출력을 끄세요.")\n')
    patch_method("_send_and_log",
        "                                  operation, done, on_error=on_error, stop_sequence=stop_sequence)\n",
        "                                  operation, done, on_error=on_error, stop_sequence=stop_sequence,\n"
        "                                  standby=standby)\n")

    # The DUT worker can wait for a fresh measurement independently of the
    # calibrator I/O worker. STBY can run immediately while the DUT is waiting.
    add_guard("_dut_start",
        '            if self._sequence_dut_reserved() and operation not in ("sequence_read", "disconnect"):\n'
        "                return\n")
    patch_method("_dut_start", "            self._dut_busy = True\n",
        '            self._dut_busy = True\n'
        '            self._seq_dut_reading = operation == "sequence_read"\n')
    patch_method("_dut_start", '"read": "조회 중… · 표시된 값은 이전 수신값입니다.",',
        '"sequence_read": "새 DUT 측정값 대기 중…",\n'
        '                                     "read": "조회 중… · 표시된 값은 이전 수신값입니다.",')
    patch_method("_dut_start",
        '                    elif operation == "read":\n'
        '                        result = self.dut_session.read(channel)\n',
        '                    elif operation == "read":\n'
        '                        result = self.dut_session.read(channel)\n'
        '                    elif operation == "sequence_read":\n'
        '                        result = self.dut_session.read(settings[0], include_dc=settings[1], fresh=True)\n')
    patch_method("_dut_start", "                    self._dut_results.put((operation, result, error))\n",
        '                    if operation == "sequence_read":\n'
        "                        result = (settings[2], result)\n"
        "                    self._dut_results.put((operation, result, error))\n")
    patch_method("_dut_start", '            threading.Thread(target=run, name="DUT-IO", daemon=True).start()\n',
        '            threading.Thread(target=run, name="DUT-IO", daemon=True).start()\n'
        '            return True\n')
    add_guard("_dut_disconnect",
        "            if self._sequence_dut_reserved():\n"
        "                self._seq_dut_resume_required = True\n"
        "                self._on_stby_clicked()\n")
    patch_method("_dut_poll", "                self._dut_busy = False\n",
        '                self._dut_busy = False\n'
        '                callback = None\n'
        '                if operation == "sequence_read":\n'
        '                    self._seq_dut_reading = False\n'
        '                    callback, result = result\n')
    patch_method("_dut_poll", '                elif operation == "read":\n',
        '                elif operation in ("read", "sequence_read"):\n')
    patch_method("_dut_poll", "                self._dut_sync_controls()\n",
        "                self._dut_sync_controls()\n"
        "                if callback:\n"
        "                    callback(result, error)\n")
    patch_method("_dut_poll", "            if not self._dut_closing and self.var_dut_auto.get()",
        "            if not self._sequence_dut_reserved() and not self._dut_closing and self.var_dut_auto.get()")
    patch_method("_dut_sync_controls", "            ready = self.dut_session.connected",
        "            reserved = self._sequence_dut_reserved()\n"
        "            ready = not reserved and self.dut_session.connected")
    patch_method("_dut_sync_controls", '            self.check_dut_auto.configure(state="normal" if self.dut_session.connected',
        '            self.check_dut_auto.configure(state="normal" if not reserved and self.dut_session.connected')
    return source
