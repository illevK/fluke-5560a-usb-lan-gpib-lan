"""New engine tests: hardware is injected, and no external device is opened."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest

from calibration.domain.document import Document
from calibration.domain.planning import make_plan, compile_point, includes, suggest_channels
from calibration.services.drivers import SimulatedCalibrator, SimulatedDUT
from calibration.services.engine import MeasurementEngine, State

class WorkflowTests(unittest.TestCase):
    def document(self, text):
        doc = Document()
        doc.options.dwell = .2
        doc.insert(doc.parse(text))
        return doc

    def execute(self, doc, start=0, cal=None, dut=None):
        cal, dut = cal or SimulatedCalibrator(), dut or SimulatedDUT()
        events = []
        engine = MeasurementEngine(cal, dut, events.append)
        engine.run(make_plan(doc.rows, start, doc.options, doc.model, dut.model), doc.options)
        return engine, events, cal, dut

    def test_dc_ac_voltage_runs_and_records_without_function_pause(self):
        doc = self.document('DC VOLT\n5 V\nAC전압\n60 Hz\t10 V')
        engine, events, cal, dut = self.execute(doc)
        self.assertEqual(engine.state, State.COMPLETE)
        self.assertEqual(dut.prepared, ['DCV', 'ACV'])
        self.assertEqual([e.measured for e in events if e.kind == 'result'], ['5 V', '10 V'])
        self.assertFalse(cal.active)

    def test_manual_confirmation_rechecks_output_and_rejects_other_rows(self):
        doc = self.document('1 V\n2 V')
        doc.options.wait_for_entry = True
        cal, events, accepted = SimulatedCalibrator(), [], []
        def handle(event):
            events.append(event)
            if event.kind == 'state' and event.state == State.MANUAL:
                accepted.append(engine.submit_entry('old-run', event.row_id, '111'))
                accepted.append(engine.submit_entry(event.run_id, doc.rows[1].uid, '222'))
                cal.active = False  # An external trip just before the operator confirms.
                accepted.append(engine.submit_entry(event.run_id, event.row_id, '0.999'))
                accepted.append(engine.submit_entry(event.run_id, event.row_id, '333'))
        engine = MeasurementEngine(cal, None, handle)
        engine.run(make_plan(doc.rows, 0, doc.options, doc.model), doc.options)
        self.assertEqual(accepted, [False, False, True, False])
        self.assertEqual(engine.state, State.ERROR)
        self.assertFalse(any(event.kind == 'result' for event in events))
        self.assertFalse(cal.active)

    def test_all_seven_types_resume_at_the_next_wiring_section(self):
        doc = self.document('DC전압\n1 V\nAC전압\n60 Hz\t1 V\nDC전류\n1 A\nAC전류\n60 Hz\t1 A\n'
                            'DC전력\n120 V / 1 A\nAC전력\n120 V / 1 A\t60 Hz\t120 W\n'
                            '역률\n120 V / 1 A\t60 Hz\t1\n120 V / 1 A\t60 Hz\t0.5\n120 V / 1 A\t60 Hz\t0.5')
        cal, dut = SimulatedCalibrator(), SimulatedDUT('PW3390')
        recorded, start = [], 0
        for _ in range(4):
            engine, events, _, _ = self.execute(doc, start, cal, dut)
            recorded.extend(e.row_id for e in events if e.kind == 'result')
            self.assertFalse(cal.active)
            if engine.state == State.COMPLETE:
                break
            self.assertEqual(engine.state, State.WIRING)
            start = next(i for i, row in enumerate(doc.rows) if row.uid == engine.current.uid)
        self.assertEqual(recorded, [row.uid for row in doc.rows])

    def test_selection_of_channel_two_never_restarts_channel_one(self):
        doc = self.document('1 V\n2 V\n3 V\n4 V\n5 V\n6 V')
        for row, channel in zip(doc.rows, (1, 2, 1, 2, 1, 2)):
            row.step.dut_channel = channel
        engine, events, cal, dut = self.execute(doc, 1, dut=SimulatedDUT('PW3390'))
        self.assertEqual(engine.state, State.COMPLETE)
        self.assertEqual([e.measured for e in events if e.kind == 'result'], ['2 V', '4 V', '6 V'])
        self.assertFalse(cal.active)

    def test_channel_reuse_suffix_is_a_separate_wiring_section(self):
        doc = self.document('1 V\n2 V\n1 V\n2 V')
        for i, row in enumerate(doc.rows):
            row.step.dut_channel = 3
            row.step.dut_channel_suffix = '@' if i >= 2 else ''
        engine, events, _, _ = self.execute(doc, 2, dut=SimulatedDUT('PW3390'))
        self.assertEqual([e.row_id for e in events if e.kind == 'result'], [row.uid for row in doc.rows[2:]])

    def test_exact_three_amp_changes_dmm_but_not_5560_post(self):
        doc = self.document('1 A\n2 A\n3 A\n3.1 A\n3.2 A')
        points = [compile_point(row, doc.options, '5560A', '34465A') for row in doc.rows]
        self.assertEqual([p.terminal for p in points], [3, 3, 10, 10, 10])
        self.assertEqual(points[0].wiring[1], points[3].wiring[1])
        self.assertNotEqual(points[3].wiring[1], points[4].wiring[1])
        engine, events, cal, _ = self.execute(doc)
        self.assertEqual(engine.state, State.WIRING)
        self.assertEqual(engine.current.uid, doc.rows[2].uid)
        self.assertEqual(engine.completed, 2)
        self.assertFalse(cal.active)

    def test_low_high_filters_and_turn_coil_use_actual_current(self):
        doc = self.document('0.1 A\n1 A\n3.2 A\n5 A\n2 A')
        doc.options.current_group = 'low'
        self.assertEqual([p.step.nominal_raw for p in make_plan(doc.rows, 0, doc.options, '5560A')], ['0.1 A', '1 A', '2 A'])
        doc.options.current_group = 'high'
        self.assertEqual([p.step.nominal_raw for p in make_plan(doc.rows, 2, doc.options, '5560A')], ['3.2 A', '5 A'])
        doc.options.coil, doc.options.turns = True, 50
        doc.rebuild_all()
        self.assertFalse(any(includes(row, doc.options, '5560A') for row in doc.rows))

    def test_dual_voltage_setting_is_used_and_range_blocks_oper(self):
        doc = self.document('1 A\n60 Hz\t1 A')
        doc.options.dual_current = True
        doc.options.dual_dc, doc.options.dual_ac = .1, .2
        points = make_plan(doc.rows, 0, doc.options, '5560A')
        self.assertTrue(all(point.dual for point in points))
        self.assertTrue(points[0].command.startswith('OUT 0.1 V'))
        self.assertTrue(points[1].command.startswith('OUT 0.2 V'))
        bad = self.document('100 Hz\t1 A')
        bad.options.dual_current = True
        engine, events, cal, _ = self.execute(bad)
        self.assertEqual(engine.state, State.ERROR)
        self.assertNotIn('OPER', cal.commands)

    def test_out_of_range_is_preserved_but_never_output(self):
        doc = self.document('1 V\n10000 A\n2 V')
        self.assertEqual(len(doc.rows), 3)
        self.assertEqual(doc.rows[1].step.func_type, 'OUT_OF_RANGE')
        engine, events, cal, dut = self.execute(doc)
        # Stop at the invalid row, with no next output or replay.
        self.assertEqual(engine.state, State.ERROR)
        self.assertEqual(len([e for e in events if e.kind == 'result']), 1)
        self.assertFalse(any('10000' in c or c == 'OUT 2 V, 0 HZ' for c in cal.commands))

    def test_zero_resistance_happens_once_before_zero_to_ten_megohms(self):
        doc = self.document('0 ohm\n10 ohm\n1 Mohm\n10 Mohm')
        doc.options.compensation = 'WIRE2'
        doc.rebuild_all()
        engine, events, cal, dut = self.execute(doc)
        self.assertEqual(engine.state, State.COMPLETE)
        self.assertEqual(dut.prepared.count('ZERO'), 1)
        self.assertEqual(len([e for e in events if e.kind == 'result']), 4)
        self.assertTrue(all('ZCOMP NONE' in cmd for cmd in cal.commands if 'OHM' in cmd))
        self.assertFalse(cal.active)

    def test_meter_voltage_to_current_requires_wiring_and_retains_next_row(self):
        doc = self.document('1 V\n1 A\n2 A')
        engine, events, cal, _ = self.execute(doc)
        self.assertEqual(engine.state, State.WIRING)
        self.assertEqual(engine.current.uid, doc.rows[1].uid)
        self.assertFalse(cal.active)
        engine, events, _, _ = self.execute(doc, 1)
        self.assertEqual(engine.state, State.COMPLETE)
        self.assertEqual(len([e for e in events if e.kind == 'result']), 2)

    def test_dut_failure_stops_no_result_no_next_row(self):
        class BrokenDUT(SimulatedDUT):
            def capture(self, *args, **kwargs):
                raise TimeoutError('test lost DUT connection')
        doc = self.document('1 V\n2 V')
        engine, events, cal, _ = self.execute(doc, dut=BrokenDUT())
        self.assertEqual(engine.state, State.ERROR)
        self.assertEqual(len([e for e in events if e.kind == 'result']), 0)
        self.assertEqual(cal.commands.count('OPER'), 1)
        self.assertFalse(cal.active)

    def test_late_sample_after_stop_is_never_recorded(self):
        class LateDUT(SimulatedDUT):
            callback = None
            def capture(self, point, **kwargs):
                self.callback()
                return super().capture(point, **kwargs)
        doc = self.document('1 V\n2 V')
        cal, dut, events = SimulatedCalibrator(), LateDUT(), []
        engine = MeasurementEngine(cal, dut, events.append)
        dut.callback = engine.request_stop
        engine.run(make_plan(doc.rows, 0, doc.options, '5560A', dut.model), doc.options)
        self.assertEqual(engine.state, State.STOPPED)
        self.assertFalse(any(e.kind == 'result' for e in events))
        self.assertFalse(cal.active)

    def test_hold_preserves_output_and_stop_confirms_standby(self):
        doc = self.document('1 V\n2 V')
        cal, dut, events = SimulatedCalibrator(), SimulatedDUT(), []
        engine = MeasurementEngine(cal, dut, events.append)
        thread = threading.Thread(target=engine.run, args=(make_plan(doc.rows, 0, doc.options, '5560A', dut.model), doc.options))
        thread.start()
        deadline = time.monotonic() + 2
        while engine.state != State.SETTLING and time.monotonic() < deadline:
            time.sleep(.005)
        engine.request_hold()
        while engine.state != State.HOLDING and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(engine.state, State.HOLDING)
        self.assertTrue(cal.active)
        self.assertFalse(any(e.kind == 'result' for e in events))
        engine.request_stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(engine.state, State.STOPPED)
        self.assertFalse(cal.active)

    def test_unknown_output_is_not_reported_as_confirmed_standby(self):
        class LostSource(SimulatedCalibrator):
            def operate(self):
                self.active = True
                raise ConnectionError('lost after OPER')
            def standby(self):
                if self.active:
                    raise ConnectionError('no response')
                super().standby()
        doc = self.document('1 V')
        engine, events, _, _ = self.execute(doc, cal=LostSource())
        self.assertEqual(engine.state, State.ERROR)
        self.assertIsNone(events[-1].output)
        self.assertIn('출력 상태 미확인', [e for e in events if e.kind == 'state'][-1].message)

    def test_stop_sends_standby_while_dut_read_is_still_blocked(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingDUT(SimulatedDUT):
            def capture(self, point, **kwargs):
                entered.set()
                release.wait(3)
                return super().capture(point, **kwargs)
        doc = self.document('1 V\n2 V')
        cal, dut, events = SimulatedCalibrator(), BlockingDUT(), []
        engine = MeasurementEngine(cal, dut, events.append)
        thread = threading.Thread(target=engine.run, args=(make_plan(doc.rows, 0, doc.options, '5560A', dut.model), doc.options))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(cal.active)
            engine.request_stop()
            engine._emergency_thread.join(1)
            self.assertFalse(cal.active)
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(engine.state, State.STOPPED)
        self.assertFalse(any(e.kind == 'result' for e in events))

    def test_project_round_trip_preserves_results_groups_and_channel_suffix(self):
        doc = self.document('1 kV\n3 A\n10 Mohm')
        doc.options.wait_for_entry = True
        doc.rows[1].step.dut_channel, doc.rows[1].step.dut_channel_suffix = 3, '@'
        doc.rows[0].step.measured_raw = '999.9991 V'
        doc.rows[1].step.breakpoint = True
        doc.rows.reverse()
        doc.renumber()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'measurement.calibration.json'
            doc.save(path)
            restored = Document.load(path)
        self.assertEqual([r.uid for r in restored.rows], [r.uid for r in doc.rows])
        self.assertEqual(restored.rows[-1].result(), '0.9999991 kV')
        self.assertEqual(restored.rows[1].step.channel_key, (3, '@'))
        self.assertTrue(restored.rows[1].step.breakpoint)
        self.assertEqual(restored.options.record_mode(False), 'entry')

    def test_channel_preview_detects_seven_types_and_extra_physical_reuse(self):
        doc = self.document('DC전류\n1 A\n2 A\n1 A\n2 A\n1 A\n2 A\nAC전류\n60 Hz\t1 A\n60 Hz\t2 A\n60 Hz\t1 A\n60 Hz\t2 A\n60 Hz\t1 A\n60 Hz\t2 A')
        assignments, descriptions = suggest_channels(doc.rows, (1, 2), extra_channel=2)
        self.assertEqual(len(assignments), 12)
        self.assertEqual(assignments[doc.rows[4].uid], (2, '@'))
        self.assertEqual(assignments[doc.rows[10].uid], (2, '@'))
        self.assertTrue(all(row.step.dut_channel is None for row in doc.rows))
