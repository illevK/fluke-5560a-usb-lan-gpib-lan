"""Bare V/I rows represent DC power points, without opening an instrument."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


class DCPowerPairTests(unittest.TestCase):
    def test_user_six_point_table(self):
        text = '\n'.join(('120 V / 1 A', '120 V / 5 A', '120 V / 10 A',
                          '240 V / 1 A', '240 V / 5 A', '240 V / 10 A'))
        steps = f.SmartClipboardSequenceParser.build_sequence_steps(text)
        self.assertEqual([s.func_type for s in steps], ['DCP'] * 6)
        self.assertEqual([s.nominal_raw for s in steps],
                         ['120 W', '600 W', '1200 W', '240 W', '1200 W', '2400 W'])
        self.assertEqual([s.vi_cond_raw for s in steps], text.splitlines())
        self.assertEqual([s.freq_cond_raw for s in steps], ['DC'] * 6)
        for step, current in zip(steps, (1, 5, 10, 1, 5, 10)):
            self.assertIn(f', {current} A, 0 HZ', step.scpi_cmd)

    def test_prefixed_signed_and_unicode_quantities(self):
        cases = [('0.12 kV / 1000 mA', '120 W'),
                 ('120 V / -1 A', '-120 W'),
                 ('−120 V / 1 A', '-120 W'),
                 ('-120 V / -1 A', '120 W'),
                 ('100 mV / 200 mA', '0.02 W'),
                 ('120 V / 0 A', '0 W'),
                 ('120V/1A', '120 W')]
        for text, expected in cases:
            with self.subTest(text=text):
                fields = f.SmartClipboardSequenceParser.parse_line_to_fields(text)
                self.assertEqual(fields[1:], ('DC', '1', expected))
                step = f.SmartClipboardSequenceParser.build_sequence_steps(text)[0]
                self.assertEqual(step.func_type, 'DCP')
                self.assertEqual(step.nominal_raw, expected)

    def test_coil_is_applied_once_to_dc_power_current(self):
        step = f.SmartClipboardSequenceParser.build_sequence_steps(
            '120 V / 100 A', use_coil=True, coil_turns=50)[0]
        self.assertEqual(step.nominal_raw, '12000 W')
        self.assertIn('OUT 120 V, 2 A, 0 HZ', step.scpi_cmd)

    def test_non_bare_rows_are_left_for_existing_parsers(self):
        parser = f.SmartClipboardSequenceParser
        for text in ('120 V / 1 A 60 Hz 120 W', '120 V / 1 A 120 W',
                     '120 V / 1 A\t0.01 W', 'prefix 120 V / 1 A',
                     '120 V / 1 A 60 Hz', '1 V'):
            with self.subTest(text=text):
                self.assertIsNone(parser.parse_dc_power_row(text))
        fields = parser.parse_line_to_fields('120 V / 1 A 60 Hz 120 W')
        self.assertEqual(fields, ('120 V / 1 A', '60 Hz', '1', '120 W'))
        power = parser.build_sequence_steps('120 V / 1 A 60 Hz 120 W')[0]
        self.assertEqual(power.func_type, 'ACP')
        self.assertIn(', 60 HZ;', power.scpi_cmd)
        pf = parser.build_sequence_steps('Lead\t308001\t\t60 Hz\t120 V\t10 A\t0.5')[0]
        self.assertEqual(pf.func_type, 'PF')
        self.assertIn('DPF 0.5, LEAD', pf.scpi_cmd)

    def test_invalid_or_unbounded_conditions_fail_before_output(self):
        parser = f.SmartClipboardSequenceParser
        for text in ('0 V / 1 A', '120 A / 1 V', '120 V / 1 V', '1e999 V / 1 A'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parser.parse_dc_power_row(text)
        with self.assertRaises(ValueError):
            parser.build_sequence_steps('120 V / 31 A')


if __name__ == '__main__':
    unittest.main(verbosity=2)
