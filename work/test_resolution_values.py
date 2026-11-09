"""Measurement resolution arithmetic; no GUI, clipboard, or instrument access."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'outputs'))
import fluke_5560a_controller as f

# Allows this independent test file to run before the generated app is rebuilt.
namespace = dict(vars(f))
exec((ROOT / 'work/resolution_values.pyfrag').read_text(encoding='utf-8'), namespace)
Resolution = namespace['MeasurementResolution']


class ResolutionValueTests(unittest.TestCase):
    def test_user_power_examples(self):
        self.assertEqual(Resolution.format_value('1234.56 W', '0.0001 kW', '1200 W', 'DCP'), '1.2346 kW')
        readings = ['120.004 W', '600.005 W', '1200.06 W', '240.004 W', '1200.06 W', '2400.04 W']
        resolutions = ['0.01 W', '0.01 W', '0.0001 kW', '0.01 W', '0.0001 kW', '0.0001 kW']
        expected = ['120.00 W', '600.01 W', '1.2001 kW', '240.00 W', '1.2001 kW', '2.4000 kW']
        self.assertEqual([Resolution.format_value(value, resolution, '1200 W', 'DCP')
                          for value, resolution in zip(readings, resolutions)], expected)

    def test_half_up_increment_rounding_and_zero(self):
        for raw, increment, expected in [('1.005 W', '0.01 W', '1.01 W'),
                                         ('-1.005 W', '0.01 W', '-1.01 W'),
                                         ('1.01 W', '0.02 W', '1.02 W'),
                                         ('1.03 W', '0.02 W', '1.04 W'),
                                         ('-0.004 W', '0.01 W', '0.00 W'),
                                         ('1250 W', '100 W', '1300 W')]:
            with self.subTest(raw=raw, increment=increment):
                self.assertEqual(Resolution.format_value(raw, increment), expected)

    def test_decimal_places_are_from_value_not_redundant_zeroes(self):
        self.assertEqual(Resolution.format_value('1.2345', '0.0100 W'), '1.23 W')
        self.assertEqual(Resolution.format_value('1234.5', '1e-4 kW'), '1234.5000 kW')
        self.assertEqual(Resolution.format_value('1,234.567 W', '.01 W'), '1234.57 W')

    def test_all_dimensions_and_si_prefixes(self):
        for base in ('V', 'A', 'W', 'OHM', 'HZ'):
            for prefix in ('p', 'n', 'u', 'm', '', 'k', 'M', 'G', 'T'):
                unit = prefix + base
                with self.subTest(unit=unit):
                    self.assertEqual(Resolution.format_value('1.2345 ' + unit, '.001 ' + unit, '1 ' + base), '1.235 ' + unit)
        self.assertEqual(Resolution.format_value('1 µV', '.001 mV', '1 V'), '0.001 mV')
        self.assertEqual(Resolution.format_value('1 μA', '0.1 uA', '1 A'), '1.0 uA')
        self.assertEqual(Resolution.format_value('1 mV', '0.000001 V', '1 V'), '0.001000 V')
        self.assertEqual(Resolution.format_value('1 MV', '1 kV', '1 V'), '1000 kV')

    def test_bare_readings_use_resolution_unit_and_pf_is_unitless(self):
        self.assertEqual(Resolution.format_value('1.23456', '0.0001 kW', '1200 W'), '1.2346 kW')
        self.assertEqual(Resolution.format_value('0.49995', '.0001', '.5 LEAD', 'PF'), '0.5000')
        self.assertEqual(Resolution.format_value('0.49995 PF', '.0001 PF', '.5 LEAD', 'PF'), '0.5000')

    def test_invalid_resolutions_and_dimension_mismatch(self):
        for resolution in ('0 W', '-.01 W', 'NaN W', 'inf W', '1e999 W', '1e-999 W', '0.1 banana', ''):
            if resolution == '':
                self.assertIsNone(Resolution.validate(resolution, '1 W', 'DCP'))
            else:
                with self.subTest(resolution=resolution), self.assertRaises(ValueError):
                    Resolution.validate(resolution, '1 W', 'DCP')
        for resolution, nominal, function in [('.01 A', '1 V', 'DCV'), ('.01', '1 W', 'DCP'), ('.01 W', '.5 LEAD', 'PF')]:
            with self.subTest(resolution=resolution), self.assertRaises(ValueError):
                Resolution.validate(resolution, nominal, function)
        for raw in ('1 V', '1 banana', 'NaN', '+inf W', '1e999 W', '1 W extra'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                Resolution.format_value(raw, '.01 W', '1 W', 'DCP')

    def test_blank_resolution_raw_retention_and_operator_notes(self):
        for raw in ('001.2300', ' 1.2345 W ', 'OL', '재측정'):
            self.assertEqual(Resolution.format_value(raw, ''), raw)
        self.assertEqual(Resolution.format_value('', '.01 W'), '')
        self.assertEqual(Resolution.format_value('   ', '.01 W'), '')
        self.assertEqual(Resolution.format_value('재측정', '.01 W'), '재측정')
        self.assertEqual(Resolution.format_value('OL', '.01 W'), 'OL')
        raw = '1234.56 W'
        Resolution.format_value(raw, '.0001 kW')
        self.assertEqual(raw, '1234.56 W')


if __name__ == '__main__':
    unittest.main(verbosity=2)
