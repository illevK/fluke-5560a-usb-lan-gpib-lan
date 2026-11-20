"""v6.10.38 regression vectors, now exercising the v1 source modules only."""
from tests.api_compat import api as f
"""Only local peers/simulation are used; no real calibrator output."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.protocols import test_communications as peers

def current_step(value, number=1, unit='A', coil=False, turns=50):
    return f.SmartClipboardSequenceParser.build_Single_step_from_fields(number, '-', 'DC', '-', f'{value} {unit}', use_coil=coil, coil_turns=turns)

def power_step(current, number=1, coil=False, turns=50):
    return f.SmartClipboardSequenceParser.build_Single_step_from_fields(number, f'120 V / {current} A', '50 Hz', '0.5', f'{60 * current} W', use_coil=coil, coil_turns=turns)

class GroupPolicyTests(unittest.TestCase):

    def test_5560a_dual_current_uses_specified_low_voltage_ranges(self):
        profile = f.MODEL_PROFILES['5560A']
        self.assertEqual(profile.dual_current_command('OUT -2 A, 0 HZ'), 'OUT 0.01 V, -2 A, 0 HZ')
        self.assertEqual(profile.dual_current_command('OUT 1.2001e-3 A, 45 HZ'), 'OUT 0.033 V, 1.2001e-3 A, 45 HZ; WAVE SINE,SINE')
        self.assertEqual(profile.dual_current_command('OUT 20.5 A, 65 HZ'), 'OUT 0.033 V, 20.5 A, 65 HZ; WAVE SINE,SINE')
        for command in ('OUT 0.0012 A, 60 HZ', 'OUT 20.5001 A, 60 HZ', 'OUT 1 A, 44.9 HZ', 'OUT 1 A, 65.1 HZ'):
            self.assertIsNone(profile.dual_current_command(command))
        self.assertIsNone(f.MODEL_PROFILES['5522A'].dual_current_command('OUT 1 A, 0 HZ'))

    def test_requested_order_and_inclusive_three_amp_boundary(self):
        steps = [current_step(v, i + 1) for i, v in enumerate((0.1, 1, 3, 5, 2))]
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s)], [1, 2, 5])
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s, True)], [3, 4])

    def test_physical_terminal_boundary_is_strictly_above_3_1(self):
        values = (2.999, 3, 3.1, 3.1001, -3.1, -5)
        self.assertEqual([f.SequenceCurrentFilter.includes(current_step(v), True, f.SequenceCurrentFilter.POSTS) for v in values], [False, False, False, True, False, True])

    def test_units_signs_and_scientific_notation(self):
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(-3000, unit='mA'), True))
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(100000, unit='uA')))
        step = current_step(1)
        step.scpi_cmd = 'OUT -3e0 A, 0 HZ'
        self.assertEqual(f.SequenceCurrentFilter.actual_current(step), 3)

    def test_power_uses_current_not_watts_and_includes_pf_calculation(self):
        steps = [power_step(v, i + 1) for i, v in enumerate((0.1, 1, 3, 5, 2))]
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s)], [1, 2, 5])
        self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s, True)], [3, 4])

    def test_coil_current_and_power_use_actual_output(self):
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(100, coil=True)))
        self.assertTrue(f.SequenceCurrentFilter.includes(current_step(150, coil=True), True))
        self.assertTrue(f.SequenceCurrentFilter.includes(power_step(100, coil=True)))
        self.assertTrue(f.SequenceCurrentFilter.includes(power_step(150, coil=True), True))

    def test_noncurrent_runs_in_normal_group_only_and_invalid_current_is_rejected(self):
        step = f.SequenceStep(1, '-', 'DC', '-', '100 V', 'DCV', 'OUT 100 V, 0 HZ', '100 V')
        self.assertTrue(f.SequenceCurrentFilter.includes(step))
        self.assertFalse(f.SequenceCurrentFilter.includes(step, True))
        step.func_type = 'DCI'
        with self.assertRaises(ValueError):
            f.SequenceCurrentFilter.includes(step)
