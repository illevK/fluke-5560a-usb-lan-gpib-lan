"""v6.10.38 regression vectors, now exercising the v1 source modules only."""
from tests.api_compat import api as f
"""SI conversion and result-entry regressions. No physical instrument or desktop input."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

class UnitTests(unittest.TestCase):

    def step(self, nominal, freq='DC', vi='-', model='5560A', **kwargs):
        return f.SmartClipboardSequenceParser.build_Single_step_from_fields(1, vi, freq, '1', nominal, model=model, **kwargs)

    def test_measurement_uses_exact_nominal_units_without_rounding(self):
        cases = (('599.8 W', '0.6 kW', '0.5998 kW'), ('1.234567890123456789012345678901234E+00 A', '2 mA', '1234.567890123456789012345678901234 mA'), ('9.999E-10 V', '1 nV', '0.9999 nV'), ('1,200,000 Ω', '1 MΩ', '1.200000 MΩ'), ('1 MΩ', '1 mΩ', '1000000000 mΩ'), ('0.5', '0.5 LEAD', '0.5'), ('', '1 V', ''), ('1 A', '1 V', '1 A'))
        for raw, nominal, expected in cases:
            with self.subTest(raw=raw, nominal=nominal):
                self.assertEqual(f.SIUnits.format_measurement(raw, nominal), expected)

    def test_all_prefixes_and_dimensions(self):
        factors = [('p', 1e-12), ('n', 1e-09), ('u', 1e-06), ('m', 0.001), ('', 1), ('k', 1000), ('M', 1000000.0), ('G', 1000000000.0), ('T', 1000000000000.0)]
        for base in ('V', 'A', 'W', 'OHM', 'HZ'):
            for prefix, factor in factors:
                with self.subTest(base=base, prefix=prefix):
                    text = f'{1 / factor:.12g} {prefix}{base}'
                    parsed = f.SmartClipboardSequenceParser.parse_value_and_unit(text)
                    self.assertIsNotNone(parsed)
                    self.assertAlmostEqual(f.SIUnits.to_base(*parsed, base), 1)
                    if base == 'HZ':
                        step = self.step('1 V', freq=text)
                    elif base == 'W':
                        step = self.step(text, vi='1 V / 1 A')
                    else:
                        step = self.step(text)
                    values = f.MODEL_PROFILES['5560A'].output_values(step.scpi_cmd.split(';')[0])
                    self.assertAlmostEqual(values['A' if base == 'W' else base], 1)

    def test_kilovolt_output_on_every_model_and_large_values_rejected(self):
        for model in f.MODEL_PROFILES:
            self.assertEqual(self.step('1 kV', model=model).scpi_cmd, 'OUT 1000 V, 0 HZ')
            if model == '5730A':
                self.assertEqual(self.step('1.1 kV', model=model).scpi_cmd, 'OUT 1100 V, 0 HZ')
            else:
                with self.assertRaises(ValueError):
                    self.step('1.1 kV', model=model)
            with self.assertRaises(ValueError):
                self.step('1 MV', model=model)
            with self.assertRaises(ValueError):
                self.step('1 TA', model=model)
            with self.assertRaises(ValueError):
                self.step('1 TOHM', model=model)

    def test_case_micro_symbols_numbers_and_unicode(self):
        cases = {'1 mV': 0.001, '1 MV': 1000000.0, '1 μV': 1e-06, '1 µV': 1e-06, '1 uV': 1e-06, '1\xa0kV': 1000, '−.5 kV': -500, '1,000 mV': 1, '1e-3 kV': 1, '1. kV': 1000, '1 밀리V': 0.001}
        for text, expected in cases.items():
            with self.subTest(text=text):
                value, unit = f.SmartClipboardSequenceParser.parse_value_and_unit(text)
                self.assertAlmostEqual(f.SIUnits.to_base(value, unit, 'V'), expected)
        self.assertEqual(f.SIUnits.to_base(1, 'mΩ', 'OHM'), 0.001)
        self.assertEqual(f.SIUnits.to_base(1, 'MΩ', 'OHM'), 1000000.0)
        for invalid in ('nan V', 'inf V', '1e999 V', '1,00 V', '1 qV', '1 mmV'):
            self.assertIsNone(f.SmartClipboardSequenceParser.parse_value_and_unit(invalid))

    def test_spreadsheet_tokens_power_frequency_and_coil(self):
        for text, command in [('1 kV', 'OUT 1000 V, 0 HZ'), ('1e3 mV', 'OUT 1 V, 0 HZ'), ('−.5 V', 'OUT -0.5 V, 0 HZ')]:
            rows = f.SmartClipboardSequenceParser.build_sequence_steps(text)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].scpi_cmd, command)
        step = self.step('1 kW', '50 kHz', '1 kV / 1 A')
        self.assertIn('OUT 1000 V, 1 A, 50000 HZ', step.scpi_cmd)
        coil = self.step('1 kA', '50 Hz', use_coil=True, coil_turns=50)
        self.assertIn('OUT 20 A', coil.scpi_cmd)
        self.assertEqual(self.step('1 nA').scpi_cmd, 'OUT 1e-09 A, 0 HZ')
        with self.assertRaises(ValueError):
            self.step('1 V', '-1 Hz')

class PowerFactorTests(unittest.TestCase):
    SAMPLE = '\n'.join(('1\t308001\t\t60 Hz\t120 V\t10 A\t1', 'Lead\t308001\t\t60 Hz\t120 V\t10 A\t0.5', 'Lag\t308001\t\t60 Hz\t120 V\t10 A\t0.5') * 5)
    UNSIGNED_SAMPLE = '\n'.join((f'{label}\t\t{frequency} Hz\t{voltage} V\t1 A\t{target}' for frequency, voltage in ((50, 120), (60, 240)) for label, target in ((f'{voltage} V / 1 A', '1'), ('', '0.5'), ('', '0.5'))))

    def test_unsigned_trio_uses_position_to_assign_directions(self):
        parser = f.SmartClipboardSequenceParser
        steps = parser.build_sequence_steps(self.UNSIGNED_SAMPLE)
        self.assertEqual([row.nominal_raw for row in steps], ['1 LEAD', '0.5 LEAD', '0.5 LAG'] * 2)
        text = '\n'.join((f'50 Hz 120 V 1 A {target}' for target in ('1', '.500', '0.5')))
        self.assertEqual([row.nominal_raw for row in parser.build_sequence_steps(text)], ['1 LEAD', '0.5 LEAD', '0.5 LAG'])

    def test_pf_triplet_requires_matching_conditions_and_unsigned_values(self):
        parser = f.SmartClipboardSequenceParser
        for values, frequencies, expected in ((('1', '0.5', '0.5'), (50, 60, 60), ['1 LEAD', '0.5 LAG', '0.5 LAG']), (('1', '0.5 LAG', '-0.5'), (60, 60, 60), ['1 LEAD', '0.5 LAG', '0.5 LEAD']), (('1', '+0.5', '0.5'), (60, 60, 60), ['1 LEAD', '0.5 LAG', '0.5 LAG'])):
            text = '\n'.join((f'{hz} Hz\t120 V\t1 A\t{value}' for hz, value in zip(frequencies, values)))
            self.assertEqual([row.nominal_raw for row in parser.build_sequence_steps(text)], expected)

    def test_exact_user_table_all_rows_direction_and_reference_number(self):
        for model in f.MODEL_PROFILES:
            if not f.MODEL_PROFILES[model].supports_power:
                from calibration.domain.document import Document
                document = Document()
                document.model = model
                rows = document.parse(self.SAMPLE)
                self.assertEqual(len(rows), 15)
                self.assertTrue(all(not row.step.scpi_cmd and row.step.func_type == 'OUT_OF_RANGE' for row in rows))
                continue
            steps = f.SmartClipboardSequenceParser.build_sequence_steps(self.SAMPLE, model=model)
            self.assertEqual(len(steps), 15)
            for index, step in enumerate(steps):
                expected = ('1 LEAD', '0.5 LEAD', '0.5 LAG')[index % 3]
                self.assertEqual(step.func_type, 'PF')
                self.assertEqual(step.nominal_raw, expected)
                self.assertEqual(step.pf_cond_raw, expected)
                self.assertIn('OUT 120 V, 10 A, 60 HZ', step.scpi_cmd)
                self.assertIn('DPF ' + expected.replace(' ', ', '), step.scpi_cmd)
                self.assertNotIn('308001', step.scpi_cmd)
                self.assertTrue(f.SequenceCurrentFilter.includes(step, True, model=model))
                self.assertFalse(f.SequenceCurrentFilter.includes(step, False, model=model))

    def test_direction_notation_and_signed_legacy(self):
        for text, display in [('0.5 lead', '0.5 LEAD'), ('0.5 LAG', '0.5 LAG'), ('1', '1 LEAD'), ('1lead', '1 LEAD'), ('Lead .5', '0.5 LEAD'), ('-0.5', '0.5 LEAD'), ('0.5', '0.5 LAG'), ('-0.5 (0.5 LEAD)', '0.5 LEAD')]:
            self.assertEqual(f.SmartClipboardSequenceParser.parse_power_factor(text)[2], display)
        for text in ('0', '1.1', '308001', '0.5 lead lag', 'nan', '0.5 LGA'):
            with self.assertRaises(ValueError):
                f.SmartClipboardSequenceParser.parse_power_factor(text)

    def test_existing_power_goal_and_new_coil_pf(self):
        step = f.SmartClipboardSequenceParser.build_Single_step_from_fields(1, '120 V / 10 A', '60 Hz', '0.5 Lead', '600 W')
        self.assertIn('DPF 0.5, LEAD', step.scpi_cmd)
        self.assertEqual(step.func_type, 'ACP')
        coil = f.SmartClipboardSequenceParser.build_sequence_steps(self.SAMPLE, use_coil=True, coil_turns=50)[1]
        self.assertIn('OUT 120 V, 0.2 A, 60 HZ; DPF 0.5, LEAD', coil.scpi_cmd)
        self.assertTrue(f.SequenceCurrentFilter.includes(coil, False))

    def test_direction_column_and_tail_conflict_rejected(self):
        line = 'Lead\t308001\t\t60 Hz\t120 V\t10 A\t0.5 Lag'
        with self.assertRaises(ValueError):
            f.SmartClipboardSequenceParser.build_sequence_steps(line)
