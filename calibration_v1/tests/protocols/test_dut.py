"""v6.10.38 regression vectors, now exercising the v1 source modules only."""
from tests.api_compat import api as f
"""DUT protocol and GUI tests. No instrument, desktop input, or real clipboard."""
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

class FakeInstrument:

    def __init__(self, identity='YOKOGAWA,WT310E,123,1.00', bad_item=False, replies=None):
        self.identity, self.bad_item = (identity, bad_item)
        self.commands, self.items = ([], {})
        self.replies = replies or {}
        self.closed = False
        self.fail = False
        self.invalid = False

    def open(self):
        self.closed = False

    def close(self):
        self.closed = True

    def write(self, command):
        self.commands.append(command)
        if command.startswith((':NUMERIC:NORMAL:ITEM', ':DISPLAY:')):
            key, value = command.split(' ', 1)
            self.items[key] = value

    def query(self, command, *, timeout=None):
        self.commands.append(command)
        if self.fail:
            raise TimeoutError('test timeout')
        if command == '*IDN?':
            return self.identity
        if command in self.replies:
            return self.replies[command]
        if command.startswith(':DISPLAY:'):
            return self.items[command[:-1]]
        if command.startswith(':NUMERIC:NORMAL:ITEM'):
            return 'P,1' if self.bad_item else self.items[command[:-1]]
        if command == ':NUMERIC:NORMAL:VALUE?':
            return '120,1,60,120,103.923048,NAN,60' if self.invalid else '120,1,60,120,103.923048,0.5,60'
        if command.endswith(',RMS?'):
            return '60,120,0,1,0'
        if command.endswith(',WATTS?'):
            return '60,60,60,120,120,103.923048,103.923048,.5,.5,0,0'
        raise AssertionError(f'Unexpected command: {command}')

class DUTProtocolTests(unittest.TestCase):

    def test_keysight_dmm_reads_only_active_function_and_retains_raw_value(self):
        for identity in ('HEWLETT-PACKARD,34401A,0,1.0', 'Agilent Technologies,34401A,0,1.0',
                         'Keysight Technologies,34461A,MY123,A.03', 'Keysight Technologies,34465A,MY123,A.03'):
            with self.subTest(identity=identity):
                instrument = FakeInstrument(identity, replies={'FUNC?': '"VOLT:DC"', 'READ?': '+1.23456789E+02'})
                session, _ = self.make_session(instrument)
                session.verify_dmm_function('DCV')
                with self.assertRaisesRegex(ValueError, '불일치'):
                    session.verify_dmm_function('ACI')
                self.assertNotIn('READ?', instrument.commands)
                sample = session.read(1, fresh=True)
                self.assertEqual(sample.values['dc_voltage'], 123.456789)
                self.assertEqual(sample.raw_values['dc_voltage'], '+1.23456789E+02')
                self.assertNotIn('current', sample.values)
                self.assertEqual(instrument.commands[-4:], ['TRIG:SOUR IMM', 'TRIG:COUN 1', 'SAMP:COUN 1', 'READ?'])
                instrument.replies.update({'FUNC?': '"FRES"', 'READ?': '9.90000000E+37'})
                session.verify_dmm_function('OHM')
                self.assertIsNone(session.read(1).values['resistance'])
                session.disconnect()

    def test_34465_short_dc_function_replies_and_current_terminals(self):
        identity = 'Keysight Technologies,34465A,MY123,A.03'
        instrument = FakeInstrument(identity, replies={'FUNC?': '"VOLT"', 'READ?': '0.9998'})
        session, _ = self.make_session(instrument)
        self.assertEqual(session.read(1, fresh=True, expected_dmm_function='DCV').values['dc_voltage'], 0.9998)
        with self.assertRaisesRegex(ValueError, '측정표 AC 전압, 본체 DC 전압'):
            session.read(1, fresh=True, expected_dmm_function='ACV')
        self.assertEqual(instrument.commands.count('READ?'), 1)
        instrument.replies.update({'FUNC?': '"CURR"', 'SENS:CURR:DC:TERM?': '+3'})
        session.verify_dmm_function('DCI')
        session.verify_34465_current_terminal('DCI', 3)
        instrument.replies['ROUT:TERM?'] = 'FRON'
        with self.assertRaisesRegex(ValueError, '본체 3 A'):
            session.verify_34465_current_terminal('DCI', 10)
        instrument.replies['ROUT:TERM?'] = 'REAR'
        standby = Mock()
        with self.assertRaisesRegex(ValueError, '전면용'):
            session.verify_34465_current_terminal('DCI', 10, ensure_standby=standby)
        standby.assert_not_called()
        self.assertNotIn('SENS:CURR:DC:TERM 10', instrument.commands)
        instrument.replies.update({'FUNC?': '"CURR:AC"', 'SENS:CURR:AC:TERM?': '+10', 'ROUT:TERM?': 'REAR'})
        with self.assertRaisesRegex(ValueError, '전면용'):
            session.read(1, fresh=True, dmm_terminal=10, expected_dmm_function='ACI')
        self.assertEqual(instrument.commands.count('READ?'), 1)
        instrument.replies['ROUT:TERM?'] = 'FRON'
        self.assertEqual(session.read(1, fresh=True, dmm_terminal=10, expected_dmm_function='ACI').values['current'], 0.9998)

    def test_34401a_serial_uses_dtr_dsr_and_enters_and_releases_remote(self):
        port = Mock()
        reply = b'HEWLETT-PACKARD,34401A,0,1.0\r\n'
        port.in_waiting = len(reply)
        port.read.return_value = reply
        port.write.side_effect = len
        settings = f.DUTSettings(mode='SERIAL', address='COM8', baudrate=9600, data_bits=7, parity='E', stop_bits=2, flow='DTR/DSR', eol='LF', model_hint='34401A')
        with patch.object(f.serial, 'Serial', return_value=port) as open_serial:
            session = f.DUTSession()
            session.connect(settings)
            session.apply_measurement_defaults()
            session.disconnect()
        self.assertEqual([call.args[0] for call in port.write.call_args_list], [b'SYST:REM\n', b'*IDN?\n', b'SYST:LOC\n'])
        self.assertEqual({k: open_serial.call_args.kwargs[k] for k in ('bytesize', 'parity', 'stopbits', 'dsrdtr')}, {'bytesize': 7, 'parity': 'E', 'stopbits': 2, 'dsrdtr': True})

    def test_dmm_visa_usb_gpib_and_vxi11_use_the_same_measurement_protocol(self):
        usb = 'USB0::0x2A8D::0x0101::MY123::INSTR'
        gpib = 'GPIB0::22::INSTR'
        transports = [('VISA', usb, usb, model) for model in ('34461A', '34465A')]
        transports += [('GPIB', gpib, gpib, model) for model in ('34401A', '34461A', '34465A')]
        transports += [('VXI11', '192.0.2.41', 'TCPIP0::192.0.2.41::inst0::INSTR', model)
                       for model in ('34461A', '34465A')]
        for mode, address, resource_name, model in transports:
            with self.subTest(mode=mode, model=model):
                resource = Mock()
                resource.read.side_effect = (f'Keysight Technologies,{model},TEST,1.00', '"VOLT:AC"', '+1.234E+00')
                manager = Mock()
                manager.open_resource.return_value = resource
                with patch.object(f, 'SharedVisaManager', return_value=manager):
                    session = f.DUTSession()
                    try:
                        session.connect(f.DUTSettings(mode=mode, address=address, eol='LF'))
                        session.apply_measurement_defaults()
                        self.assertEqual(session.read().values['voltage'], 1.234)
                        self.assertEqual(resource.write.call_args.args, ('READ?',))
                    finally:
                        session.disconnect()
                manager.open_resource.assert_called_once_with(resource_name, open_timeout=10000)

    def make_session(self, instrument=None):
        instrument = instrument or FakeInstrument()
        session = f.DUTSession(lambda *_: instrument)
        session.connect(f.DUTSettings(address='GPIB0::4::INSTR'))
        return (session, instrument)

    def test_requested_and_related_model_identification(self):
        for model in ('510', '550', 'PPA510', 'PPA550', '1530', '2530', '2630', '3530', '4530', '5530', 'PPA 5511-HC'):
            self.assertEqual(f.identify_dut(f'NEWTONS4TH,{model},123,1.00').family, 'PPA')
        self.assertEqual(f.identify_dut('NEWTONS4TH,PPA5511,123,1').channels, 1)
        for model, channels in [('WT310', 1), ('WT310E', 1), ('WT310HC', 1), ('WT310EH', 1), ('WT332E', 2), ('WT333', 3), ('760203', 3), ('WT500', 3), ('WT1806E', 6)]:
            self.assertEqual(f.identify_dut(f'YOKOGAWA,{model},123,1').channels, channels)
        for identity in ('FLUKE,5560A,123,4', 'UNKNOWN,WT310E,123,1', 'YOKOGAWA,WT210,123,1', 'NEWTONS4TH,PSM1735,1,1', '120,1,60', ''):
            with self.assertRaises(ValueError):
                f.identify_dut(identity)

    def test_identification_rejects_other_devices_before_configuration(self):
        instrument = FakeInstrument('FLUKE,5560A,123,4')
        with self.assertRaises(ValueError):
            self.make_session(instrument)
        self.assertEqual(instrument.commands, ['*IDN?'])
        self.assertTrue(instrument.closed)

    def test_explicit_model_mismatch_closes_peer_before_initialization(self):
        instrument = FakeInstrument('Keysight Technologies,34465A,TEST,1')
        session = f.DUTSession(lambda *_: instrument)
        with self.assertRaisesRegex(ValueError, '34461A.*34465A'):
            session.connect(f.DUTSettings(mode='DMM_TCP', address='192.0.2.12', port=5025, model_hint='34461A'))
        self.assertEqual(instrument.commands, ['*IDN?'])
        self.assertTrue(instrument.closed)

    def test_wt3000_family_idn_suffixes_and_element_counts(self):
        for channels in range(1, 5):
            for model, expected, firmware in ((f'76030{channels}-0{channels}-SV', f'WT3000 (76030{channels})', 'F4.01'), (f'76030{channels}-{channels}0-MV', f'WT3000 (76030{channels})', 'F4.01'), (f'WT300{channels}E-2A0-30A{channels}', f'WT300{channels}E', 'F6.01')):
                with self.subTest(model=model):
                    profile = f.identify_dut(f'YOKOGAWA,{model},0,{firmware}')
                    self.assertEqual((profile.family, profile.model, profile.channels), ('WT', expected, channels))
                    self.assertTrue(profile.wt3000_numeric)

    def test_wt3000_reads_selected_element_modes_and_checks_total_order(self):
        for identity in ('YOKOGAWA,760304-04-SV,0,F4.01', 'YOKOGAWA,WT3004E-2A0-30A4,0,F6.01'):
            with self.subTest(identity=identity):
                instrument = FakeInstrument(identity, replies={':INPUT:VOLTAGE:MODE:ELEMENT4?': 'mean', ':INPUT:CURRENT:MODE:ELEMENT4?': 'DC', ':NUMERIC:NORMAL:ITEM1?': 'U,4,TOTAL', ':NUMERIC:NORMAL:ITEM2?': 'I,4,TOT', ':NUMERIC:NORMAL:ITEM6?': 'LAMB,4,TOT', ':NUMERIC:NORMAL:ITEM7?': 'FU,4'})
                session, _ = self.make_session(instrument)
                sample = session.read(4)
                self.assertEqual(sample.vi_modes, ('MEAN', 'DC'))
                self.assertEqual((sample.channel, sample.values['voltage'], sample.values['current']), (4, 120, 1))
                self.assertEqual(instrument.commands[4:13], [':INPUT:VOLTAGE:MODE:ELEMENT4?', ':INPUT:CURRENT:MODE:ELEMENT4?', ':NUMERIC:NORMAL:ITEM1 U,4,TOTAL', ':NUMERIC:NORMAL:ITEM2 I,4,TOTAL', ':NUMERIC:NORMAL:ITEM3 P,4,TOTAL', ':NUMERIC:NORMAL:ITEM4 S,4,TOTAL', ':NUMERIC:NORMAL:ITEM5 Q,4,TOTAL', ':NUMERIC:NORMAL:ITEM6 LAMBDA,4,TOTAL', ':NUMERIC:NORMAL:ITEM7 FU,4'])
                self.assertEqual(instrument.commands[-2:], [':NUMERIC:HOLD OFF', ':NUMERIC:NORMAL:VALUE?'])
                instrument.replies[':NUMERIC:NORMAL:ITEM2?'] = 'I,4,DC'
                instrument.commands.clear()
                with self.assertRaisesRegex(ValueError, 'I,4,TOTAL'):
                    session.read(4)
                self.assertNotIn(':NUMERIC:NORMAL:VALUE?', instrument.commands)
                self.assertTrue(instrument.closed)
                self.assertIsNone(session.sample)

    def test_wt5000_seventh_element_uses_modern_rms_items(self):
        session, instrument = self.make_session(FakeInstrument('YOKOGAWA,WT5000,1234567890,F3.51'))
        self.assertEqual(session.profile.channels, 7)
        sample = session.read(7)
        self.assertEqual(sample.channel, 7)
        self.assertIsNone(sample.vi_modes)
        self.assertIn(':NUMERIC:NORMAL:ITEM1 URMS,7', instrument.commands)
        self.assertIn(':NUMERIC:NORMAL:ITEM2 IRMS,7', instrument.commands)
        self.assertEqual(instrument.commands[-1], ':NUMERIC:NORMAL:VALUE?')
        self.assertFalse(any((':MODE:ELEMENT' in command for command in instrument.commands)))
        instrument.commands.clear()
        with self.assertRaises(ValueError):
            session.read(8)
        self.assertEqual(instrument.commands, [])
        session.disconnect()

    def test_wt_mapping_readback_and_values(self):
        for model in ('WT310', 'WT310E'):
            with self.subTest(model=model):
                session, instrument = self.make_session(FakeInstrument(f'YOKOGAWA,{model},123,1.00'))
                sample = session.read()
                self.assertEqual(sample.values['voltage'], 120)
                self.assertEqual(sample.values['current'], 1)
                self.assertEqual(sample.values['pf'], 0.5)
                self.assertEqual(sample.values['frequency'], 60)
                self.assertEqual(instrument.commands[-1], ':NUMERIC:NORMAL:VALUE?')
                self.assertIn(':NUMERIC:NORMAL:ITEM1 URMS,1', instrument.commands)
                self.assertIn(':NUMERIC:NORMAL:ITEM2 IRMS,1', instrument.commands)
                self.assertIn(':NUMERIC:NORMAL:ITEM6 LAMBDA,1', instrument.commands)
                self.assertIsNone(sample.vi_modes)
                self.assertFalse(sample.simulated)
                self.assertIs(sample, session.sample)
                session.disconnect()
                self.assertFalse(session.connected)
                self.assertIsNone(session.sample)

    def test_n4l_data_positions_are_not_confused_with_fundamentals(self):
        session, instrument = self.make_session(FakeInstrument('NEWTONS4TH,550,123,1'))
        sample = session.read(1)
        self.assertEqual(sample.values, dict(voltage=120, current=1, power=60, apparent=120, reactive=103.923048, pf=0.5, frequency=60))
        self.assertIn('순차', sample.note)
        self.assertEqual(instrument.commands, ['*IDN?', 'POWER,PHASE1,RMS?', 'POWER,PHASE1,WATTS?'])

    def test_kinetiq_serial_identification_and_channel_three_reading(self):
        for model in ('PPA2530', 'PPA2630'):
            with self.subTest(model=model):
                instrument = FakeInstrument(f'NEWTONS4TH,{model} KinetiQ,01234,1.00', replies={'CONFIG,13?': '3', 'CONFIG,28?': '0', 'CONFIG,29?': '0', 'CONFIG,40?': '0', 'COUPLI,PHASE1?': '0', 'COUPLI,PHASE3?': '0', 'DAV?': '11', 'POWER,PHASE3,RMS?': '60,120,12,1,.1,240,24,2,.2,480,48,3,.3', 'STATUS,CH5?': '1,1V,OK', 'STATUS,CH6?': '1,1A,OK'})
                session = f.DUTSession(lambda *_: instrument)
                instrument.settings = f.DUTSettings(mode='SERIAL', address='COM3', baudrate=19200, eol='CR')
                session.connect(instrument.settings)
                self.assertEqual((session.profile.model, session.available_channels), (model, (1, 2, 3)))
                self.assertTrue(session.supports_fresh_capture)
                self.assertIn('FAST', session.apply_measurement_defaults())
                instrument.commands.clear()
                data = session.read(3, include_dc=True, fresh=True).values
                self.assertEqual(tuple((data[key] for key in ('voltage', 'dc_voltage', 'current', 'dc_current', 'power'))), (480, 48, 3, 0.3, 60))
                self.assertEqual(instrument.commands, ['COUPLI,PHASE3?'] + ['DAV?', 'POWER,PHASE3,RMS?'] * 3 + ['DAV?', 'POWER,PHASE3,WATTS?', 'STATUS,CH5?', 'STATUS,CH6?'])
                session.disconnect()

    def test_new_ppa_model_must_verify_common_records_before_connecting(self):
        identity = 'NEWTONS4TH,PPA9999,123,1'
        session, instrument = self.make_session(FakeInstrument(identity))
        self.assertFalse(session.supports_fresh_capture)
        self.assertIn('공통 조회', session.apply_measurement_defaults())
        self.assertEqual(session.read().values['power'], 60)
        with self.assertRaisesRegex(ValueError, '공통 조회'):
            session.read(fresh=True)
        queries = ['POWER,PHASE1,RMS?', 'POWER,PHASE1,WATTS?']
        self.assertEqual(instrument.commands, ['*IDN?'] + queries * 2)
        invalid = FakeInstrument(identity, replies={'POWER,PHASE1,RMS?': 'UNSUPPORTED'})
        with self.assertRaises(ValueError):
            self.make_session(invalid)
        self.assertTrue(invalid.closed)

    def test_mapping_failure_and_timeout_close_stream_and_clear_sample(self):
        session, instrument = self.make_session(FakeInstrument(bad_item=True, replies={':INPUT:MODE?': 'RMS'}))
        with self.assertRaises(ValueError):
            session.read()
        self.assertTrue(instrument.closed)
        self.assertNotIn(':NUMERIC:NORMAL:VALUE?', instrument.commands)
        session, instrument = self.make_session()
        session.read()
        instrument.fail = True
        with self.assertRaises(TimeoutError):
            session.read()
        self.assertFalse(session.connected)
        self.assertIsNone(session.sample)

    def test_data_not_ready_retains_connected_dut_and_can_read_later(self):
        instrument = FakeInstrument('NEWTONS4TH,PPA2530 KinetiQ,TEST,1.0', replies={'DAV?': '10'})
        instrument.settings = SimpleNamespace(timeout=0.02)
        session, _ = self.make_session(instrument)
        with self.assertRaises(f.DUTDataNotReadyError):
            session.read()
        self.assertTrue(session.connected)
        self.assertFalse(instrument.closed)
        self.assertIsNone(session.recovery_target)
        self.assertFalse(any((command.startswith('POWER') for command in instrument.commands)))
        instrument.replies['DAV?'] = '11'
        self.assertEqual(session.read().values['voltage'], 120)

    def test_dut_recovery_rejects_different_unit_and_uses_a_new_socket(self):
        identity = b'NEWTONS4TH,PPA2530 KinetiQ,TEST,1.0\r\n'
        stale, wrong, restored = (Mock(), Mock(), Mock())
        stale.recv.side_effect = [identity, ConnectionResetError('reset')]
        wrong.recv.return_value = identity.replace(b'TEST', b'OTHER')
        restored.recv.return_value = identity
        settings = f.DUTSettings(mode='PPA_LAN', address='192.0.2.6', source_ip='192.0.2.7')
        with patch.object(f.socket, 'create_connection', side_effect=(stale, wrong, restored)):
            session = f.DUTSession()
            session.connect(settings)
            with self.assertRaises(ConnectionResetError):
                session.check_connection()
            stale.close.assert_called_once()
            self.assertTrue(session.recovery_pending)
            self.assertIn('*IDN?', session.last_io_failure)
            self.assertIn('통신 끊김', session.last_io_failure)
            with self.assertRaisesRegex(ValueError, '식별 정보'):
                session.recover_connection()
            self.assertFalse(session.connected)
            wrong.sendall.assert_called_once_with(b'*IDN?\r\n')
            self.assertEqual(session.recover_connection(), identity.decode().strip())
            self.assertEqual(session.settings, settings)
            restored.sendall.assert_called_once_with(b'*IDN?\r\n')
            session.disconnect()

    def test_invalid_values_and_record_length(self):
        self.assertEqual(f.dut_numbers('NAN,INF,-INF,9.91E37,1', 5), [None, None, None, None, 1])
        for bad in ('1,2', '1,bad,3', ':HEADER 1,2,3'):
            with self.assertRaises(ValueError):
                f.dut_numbers(bad, 3)
        session, instrument = self.make_session()
        instrument.invalid = True
        self.assertIsNone(session.read().values['pf'])

    def test_bad_channel_sends_no_commands(self):
        session, instrument = self.make_session()
        with self.assertRaises(ValueError):
            session.read(2)
        self.assertEqual(instrument.commands, ['*IDN?'])

    def test_settings_and_demo_cannot_open_any_resource(self):
        factory = Mock(side_effect=AssertionError('demo must be offline'))
        session = f.DUTSession(factory)
        session.connect(f.DUTSettings(mode='DEMO'))
        self.assertTrue(session.read().simulated)
        factory.assert_not_called()
        for settings in (f.DUTSettings(timeout=float('nan')), f.DUTSettings(mode='PPA_LAN', address='192.168.1.3', port=0), f.DUTSettings(mode='SERIAL', address='COM0'), f.DUTSettings(address='ASRL1::INSTR')):
            with self.assertRaises(ValueError):
                settings.validate()

    def test_cancellation_does_not_retry(self):
        session, instrument = self.make_session()
        session.cancel.set()
        with self.assertRaises(InterruptedError):
            session.read()
        self.assertTrue(instrument.closed)
        self.assertIsNone(session.sample)
        self.assertEqual(instrument.commands, ['*IDN?'])

    def test_wt_linked_read_discards_old_event_and_keeps_signed_dc(self):

        class UpdatingWT(FakeInstrument):
            settings = SimpleNamespace(timeout=1.0)
            events = iter(('1', '8', '1', '0', '1'))
            event_count = 0
            event_filter = ':STATUS:FILT1 NEV'

            def write(self, command):
                super().write(command)
                if command.startswith(':STATUS:FILTER1 '):
                    self.event_filter = command.split()[1]

            def query(self, command):
                if command in (':HOLD?', ':MEASURE:MHOLD?', ':STATUS:FILTER1?', ':STATUS:EESR?', ':NUMERIC:NORMAL:VALUE?'):
                    self.commands.append(command)
                    if command in (':HOLD?', ':MEASURE:MHOLD?'):
                        return '0'
                    if command == ':STATUS:FILTER1?':
                        return self.event_filter
                    if command == ':STATUS:EESR?':
                        self.event_count += 1
                        return next(self.events)
                    if self.event_count != 5:
                        raise AssertionError('Numeric data read before both fresh updates')
                    return '120,1,60,120,0,0.5,60,-119.99,-0.9999'
                return super().query(command)
        session, instrument = self.make_session(UpdatingWT())
        sample = session.read(include_dc=True, fresh=True)
        self.assertEqual((sample.values['dc_voltage'], sample.values['dc_current']), (-119.99, -0.9999))
        self.assertEqual(instrument.items[':NUMERIC:NORMAL:ITEM8'], 'UDC,1')
        self.assertEqual(instrument.items[':NUMERIC:NORMAL:ITEM9'], 'IDC,1')
        self.assertEqual(instrument.event_filter, 'NEVER')

    def test_hioki_models_confirm_autorange_sync_and_read_fresh_rms_and_dc(self):
        for variant, channels in (('PW3390-03', 4), ('PW6001-04', 4), ('PW6001-16', 6)):
            with self.subTest(model=variant):
                query = '*WAI;:MEAS? Urms2,Irms2,P2,S2,Q2,PF2,FREQ2,Udc2,Idc2'
                profile_replies = {':MODE?': 'TYPE1', ':RATE?': '50ms'}
                for channel in range(1, channels + 1):
                    profile_replies.update({f':WIRING{channel}?': '1P2W', f':VOLTAGE{channel}:MEAN?': 'OFF', f':CURRENT{channel}:MEAN?': 'OFF', f':VOLTAGE{channel}:AUTO?': 'ON', f':CURRENT{channel}:AUTO?': 'ON', f':SOURCE{channel}?': f'U{channel}'})
                instrument = FakeInstrument(f'HIOKI,{variant},123,V1.00', replies={**profile_replies, ':RS232:ANSWER?': 'OFF', ':HEADER?': 'OFF', ':HOLD?': 'OFF', query: '1.20000E+02,1.00000E+00,60,120,+9999.9E+99,0.5,60,-0.0123,-0.002'})
                session, _ = self.make_session(instrument)
                self.assertTrue(session.supports_fresh_capture)
                self.assertIn('U/I 정류 RMS', session.apply_measurement_defaults())
                self.assertNotIn(':MODE TYPE1', instrument.commands)
                self.assertEqual(session.input_channels, tuple(range(1, channels + 1)))
                for channel in session.input_channels:
                    for command in (f':VOLTAGE{channel}:MEAN OFF', f':CURRENT{channel}:MEAN OFF', f':VOLTAGE{channel}:AUTO ON', f':CURRENT{channel}:AUTO ON', f':SOURCE{channel} U{channel}'):
                        self.assertIn(command, instrument.commands)
                self.assertEqual(':RATE 50MS' in instrument.commands, variant.startswith('PW6001'))
                value = session.read(2, include_dc=True, fresh=True)
                self.assertEqual(value.raw_values['voltage'], '1.20000E+02')
                self.assertEqual(value.values['dc_voltage'], -0.0123)
                self.assertEqual(value.values['pf'], 0.5)
                self.assertIsNone(value.values['reactive'])
                self.assertEqual(instrument.commands[-2:], [':HOLD?', query])

    def test_hioki_switches_grouped_wiring_before_setting_all_channels(self):
        for variant, grouped_mode in (('PW3390-01', 'TYPE8'), ('PW6001-04', 'TYPE5')):
            with self.subTest(model=variant):
                replies = {':RS232:ANSWER?': ':RS232C:ANSWER ON;000', ':RS232:ANSWER OFF;:RS232:ANSWER?': 'OFF', ':HEADER?': 'OFF', ':MODE?': grouped_mode, ':RATE?': '50ms', ':HOLD?': 'PEAK'}
                for channel in range(1, 5):
                    replies.update({f':WIRING{channel}?': '1P2W', f':VOLTAGE{channel}:MEAN?': 'OFF', f':CURRENT{channel}:MEAN?': 'OFF', f':VOLTAGE{channel}:AUTO?': 'ON', f':CURRENT{channel}:AUTO?': 'ON', f':SOURCE{channel}?': f'U{channel}'})
                instrument = FakeInstrument(f'HIOKI,{variant},123,V1.00', replies=replies)
                write = instrument.write

                def switch_wiring(command):
                    write(command)
                    if command == ':MODE TYPE1':
                        replies[':MODE?'] = 'TYPE1'
                with patch.object(instrument, 'write', side_effect=switch_wiring):
                    session, _ = self.make_session(instrument)
                    self.assertIn('1P2W 독립 4CH', session.apply_measurement_defaults())
                self.assertIn(':RS232:ANSWER OFF;:RS232:ANSWER?', instrument.commands)
                changed = instrument.commands.index(':MODE TYPE1')
                self.assertEqual(instrument.commands[changed + 1], ':MODE?')
                for channel in range(1, 5):
                    for command in (f':VOLTAGE{channel}:AUTO ON', f':CURRENT{channel}:AUTO ON', f':SOURCE{channel} U{channel}'):
                        self.assertIn(command, instrument.commands[changed + 2:])
                with self.assertRaisesRegex(ValueError, 'PEAK HOLD'):
                    session.read(1, fresh=True)
                self.assertFalse(any((':MEAS?' in command for command in instrument.commands)))

    def test_hioki_rejects_failed_wiring_verification_before_channel_settings(self):
        for variant, mode, final_channel in (('PW3390-03', 'TYPE8', '1P2W'), ('PW6001-04', 'TYPE1', '1P3W')):
            with self.subTest(model=variant):
                instrument = FakeInstrument(f'HIOKI,{variant},123,V1.00', replies={':RS232:ANSWER?': 'OFF', ':HEADER?': 'OFF', ':MODE?': mode, ':WIRING1?': '1P2W', ':WIRING2?': '1P2W', ':WIRING3?': '1P2W', ':WIRING4?': final_channel})
                session, _ = self.make_session(instrument)
                with self.assertRaisesRegex(ValueError, ':MODE TYPE1|1P2W 독립 결선'):
                    session.apply_measurement_defaults()
                self.assertFalse(any((':AUTO' in command or ':SOURCE' in command for command in instrument.commands)))
                self.assertEqual(session.input_channels, ())

    def test_hioki_rejects_unconfirmed_current_rectifier_setting(self):
        instrument = FakeInstrument('HIOKI,PW3390-02,TEST,V1.00', replies={':RS232:ANSWER?': 'OFF', ':HEADER?': 'OFF', ':MODE?': 'TYPE1', ':VOLTAGE1:MEAN?': 'OFF', ':CURRENT1:MEAN?': 'ON'})
        session, _ = self.make_session(instrument)
        with self.assertRaisesRegex(ValueError, 'CURRENT1:MEAN OFF'):
            session.apply_measurement_defaults()
        self.assertFalse(session.input_channels)

    def test_hioki_lan_checks_model_against_the_selected_port(self):
        for port, variant in ((3390, 'PW3390-03'), (23, 'PW6001-14')):
            with self.subTest(port=port):
                identity = f'HIOKI,{variant},123,V1.00'
                sock = Mock()
                sock.recv.return_value = (identity + '\r\n').encode('ascii')
                settings = f.DUTSettings(mode='HIOKI_LAN', address='192.0.2.43', port=port)
                with patch.object(f.socket, 'create_connection', return_value=sock) as open_socket:
                    session = f.DUTSession()
                    self.assertEqual(session.connect(settings), identity)
                    session.disconnect()
                    wrong_model = 'PW6001-14' if port == 3390 else 'PW3390-03'
                    sock.recv.return_value = f'HIOKI,{wrong_model},123,V1.00\r\n'.encode('ascii')
                    with self.assertRaisesRegex(ValueError, 'LAN 포트'):
                        session.connect(settings)
                    self.assertTrue(all((call.args[0] == ('192.0.2.43', port) for call in open_socket.call_args_list)))
                self.assertEqual(sock.sendall.call_count, 2)
                self.assertTrue(all((call.args[0] == b'*IDN?\r\n' for call in sock.sendall.call_args_list)))

    def test_wt310e_connection_defaults_accept_actual_readbacks(self):
        session, instrument = self.make_session(FakeInstrument(replies={':COMMUNICATE:HEADER?': '0', ':RATE?': '100.0E-03', ':INPUT:MODE?': 'RMS', ':INPUT:SYNCHRONIZE?': 'VOLT', ':INPUT:VOLTAGE:AUTO?': '1', ':INPUT:CURRENT:AUTO?': '1'}))
        self.assertEqual(instrument.commands, ['*IDN?'])
        self.assertIn('본체 전압/전류/전력/역률', session.apply_measurement_defaults())
        self.assertEqual([instrument.items[f':DISPLAY:NORMAL:ITEM{i}'] for i in range(1, 5)], ['U,1', 'I,1', 'P,1', 'LAMBDA,1'])
        for command in (':RATE 100MS', ':INPUT:MODE RMS', ':INPUT:SYNCHRONIZE VOLTAGE', ':INPUT:VOLTAGE:AUTO ON', ':INPUT:CURRENT:AUTO ON'):
            self.assertIn(command, instrument.commands)

    def test_wt5000_defaults_configure_installed_elements(self):
        replies = {f':SYSTEM:ELEMENT{n}:MODEL?': 'NONE' for n in range(1, 8)}
        replies.update({':SYSTEM:ELEMENT2:MODEL?': '"760901"', ':COMMUNICATE:HEADER?': '0', ':UPDATE:MODE?': 'CONS', ':UPDATE:METHOD?': 'SSP', ':UPDATE:RATE?': '500.0E-03', ':INPUT:VOLTAGE:AUTO:ELEMENT2?': '1', ':INPUT:CURRENT:AUTO:ELEMENT2?': '1', ':INPUT:SYNCHRONIZE:ELEMENT2?': 'U2'})
        session, instrument = self.make_session(FakeInstrument('YOKOGAWA,WT5000,1,1', replies=replies))
        self.assertIn('500 ms', session.apply_measurement_defaults())
        self.assertEqual([instrument.items[f':DISPLAY:NUMERIC1:NORMAL:VAL4:ITEM{i}'] for i in range(1, 5)], ['URMS,2', 'IRMS,2', 'P,2', 'LAMBDA,2'])
        self.assertIn(':DISPLAY:NUMERIC1:NORMAL:FORMAT VAL4', instrument.commands)
        self.assertIn(':DISPLAY:CONFIG NUMERIC', instrument.commands)
        self.assertEqual(session.input_channels, (2,))
        self.assertIn(':INPUT:SYNCHRONIZE:ELEMENT2 U2', instrument.commands)
        self.assertNotIn(':INPUT:VOLTAGE:AUTO:ELEMENT1 ON', instrument.commands)
        with self.assertRaisesRegex(ValueError, '2'):
            session.read(1)

    def test_ppa_defaults_verify_model_specific_reference(self):
        for model, reference in (('550', 38), ('2530', 40), ('PPA5511', 38)):
            with self.subTest(model=model):
                session, instrument = self.make_session(FakeInstrument(f'NEWTONS4TH,{model},1,1', replies={'CONFIG,13?': '3', 'CONFIG,28?': '0', 'CONFIG,29?': '0', f'CONFIG,{reference}?': '0', 'COUPLI,PHASE1?': '0'}))
                self.assertIn('FAST', session.apply_measurement_defaults())
                self.assertIn('FQREF,VOLTAGE', instrument.commands)
                self.assertIn('CONFIG,28,0', instrument.commands)
                self.assertIn('CONFIG,29,0', instrument.commands)

    def test_wt_linked_read_refuses_held_data(self):
        for settings, message in (({':HOLD?': '1'}, 'HOLD'), ({':HOLD?': '0', ':MEASURE:MHOLD?': '1'}, 'MAX HOLD')):
            with self.subTest(settings=settings):
                session, instrument = self.make_session(FakeInstrument(replies=settings))
                with self.assertRaisesRegex(ValueError, message):
                    session.read(fresh=True)
                self.assertNotIn(':NUMERIC:NORMAL:VALUE?', instrument.commands)
                self.assertTrue(instrument.closed)

    def test_wt3000_signed_dc_only_when_element_mode_is_dc(self):
        session, _ = self.make_session(FakeInstrument('YOKOGAWA,760301,0,F4.01', replies={':INPUT:VOLTAGE:MODE:ELEMENT1?': 'RMS', ':INPUT:CURRENT:MODE:ELEMENT1?': 'DC'}))
        sample = session.read(include_dc=True)
        self.assertNotIn('dc_voltage', sample.values)
        self.assertEqual(sample.values['dc_current'], 1)

    def test_ppa_linked_read_waits_for_new_data_without_trigger(self):

        class UpdatingPPA(FakeInstrument):
            settings = SimpleNamespace(timeout=1.0)
            flags = iter(('2', '3', '2', '3'))
            rms_values = iter(('60,1,0,2,0', '60,5,0,2,0', '60,120,-119.9,1,-0.999'))

            def query(self, command):
                if command in ('DAV?', 'POWER,PHASE1,RMS?'):
                    self.commands.append(command)
                    return next(self.flags if command == 'DAV?' else self.rms_values)
                return super().query(command)
        session, instrument = self.make_session(UpdatingPPA('NEWTONS4TH,PPA5511,1,1', replies={'COUPLI,PHASE1?': '0', 'STATUS,CH1?': '6,300V,OK', 'STATUS,CH2?': '3,10A,OVER'}))
        sample = session.read(include_dc=True, fresh=True)
        self.assertEqual((sample.values['voltage'], sample.values['dc_voltage'], sample.values['dc_current']), (120, -119.9, None))
        self.assertIsNone(sample.values['power'])
        self.assertEqual(instrument.commands[-10:], ['POWER,PHASE1,RMS?', 'DAV?', 'DAV?', 'POWER,PHASE1,RMS?', 'DAV?', 'DAV?', 'POWER,PHASE1,RMS?', 'POWER,PHASE1,WATTS?', 'STATUS,CH1?', 'STATUS,CH2?'])

    def test_tcp_fragmented_crlf_and_consecutive_queries(self):
        sock = Mock()
        sock.recv.side_effect = [b'NEWTONS', b'4TH,550,1,1\r', b'\n60,120,', b'0,1,0\r\n']
        transport = f.DUTTransport(f.DUTSettings(mode='PPA_LAN', address='192.168.1.3', port=10001), threading.Event())
        with patch.object(f.socket, 'create_connection', return_value=sock):
            transport.open()
        self.assertEqual(transport.query('*IDN?'), 'NEWTONS4TH,550,1,1')
        self.assertEqual(transport.query('POWER,PHASE1,RMS?'), '60,120,0,1,0')
        self.assertEqual(sock.sendall.call_args_list[0].args, (b'*IDN?\r\n',))
        transport.close()
        sock.close.assert_called_once()

    def test_wt5000_socket_uses_fixed_port_and_lf_and_rejects_other_models(self):
        settings = f.DUTSettings(mode='WT5000_TCP', address='192.0.2.41', port=10001, eol='CRLF')
        for model in ('WT5000', 'WT310E'):
            with self.subTest(model=model):
                session = f.DUTSession()
                sock = Mock()
                identity = f'YOKOGAWA,{model},1234567890,F3.51'
                sock.recv.side_effect = [b'YOKOGAWA,', f'{model},1234567890,F3.51\n'.encode('ascii')]
                with patch.object(f.socket, 'create_connection', return_value=sock) as connect:
                    if model == 'WT5000':
                        self.assertEqual(session.connect(settings), identity)
                        self.assertTrue(session.connected)
                        session.disconnect()
                    else:
                        with self.assertRaisesRegex(ValueError, 'WT5000'):
                            session.connect(settings)
                        self.assertFalse(session.connected)
                        self.assertIsNone(session.transport)
                connect.assert_called_once_with(('192.0.2.41', 10002), timeout=10.0, source_address=None)
                sock.sendall.assert_called_once_with(b'*IDN?\n')
                sock.close.assert_called_once()

    def test_serial_configuration_and_no_telnet_negotiation(self):
        serial_port = Mock(in_waiting=1)
        serial_port.write.return_value = 7
        serial_port.read.side_effect = [b'*ID', b'N?\r\nO', b'K\r\n']
        transport = f.DUTTransport(f.DUTSettings(mode='SERIAL', address='COM5', baudrate=38400, flow='RTS/CTS'), threading.Event())
        with patch.object(f.serial, 'Serial', return_value=serial_port) as constructor:
            transport.open()
        self.assertTrue(constructor.call_args.kwargs['rtscts'])
        self.assertEqual(transport.query('*IDN?'), 'OK')
        serial_port.write.assert_called_once_with(b'*IDN?\r\n')
        transport.close()

    def test_gpib_readback_accepts_eoi_and_releases_only_this_dut(self):
        manager, resource = (Mock(), Mock())
        manager.open_resource.return_value = resource
        resource.read.return_value = 'NEWTONS4TH,PPA5511,123,1.00\r'
        settings = f.DUTSettings(mode='GPIB', address='GPIB0::5::INSTR', eol='LF')
        with patch.object(f.pyvisa, 'ResourceManager', return_value=manager):
            session = f.DUTSession()
            try:
                self.assertEqual(session.connect(settings), 'NEWTONS4TH,PPA5511,123,1.00')
                self.assertIsNone(resource.read_termination)
                self.assertEqual(resource.write_termination, '\n')
                resource.control_ren.assert_not_called()
                resource.read.side_effect = ('3\r', '0\r', '0\r', '0\r', '0\r')
                self.assertIn('FAST', session.apply_measurement_defaults())
                resource.control_ren.assert_called_once_with(f.pyvisa.constants.RENLineOperation.asrt_address)
            finally:
                session.disconnect()
        self.assertEqual([call.args[0] for call in resource.control_ren.call_args_list], [f.pyvisa.constants.RENLineOperation.asrt_address, f.pyvisa.constants.RENLineOperation.address_gtl])
        resource.close.assert_called_once()

    def test_visa_vxi11_and_cleanup_on_failed_configuration(self):
        manager, resource = (Mock(), Mock())
        manager.open_resource.return_value = resource
        resource.read.return_value = 'YOKOGAWA,WT310E,1,1\n'
        transport = f.DUTTransport(f.DUTSettings(mode='WT_LAN', address='192.168.1.3', eol='LF'), threading.Event())
        with patch.object(f.pyvisa, 'ResourceManager', return_value=manager):
            transport.open()
        manager.open_resource.assert_called_once_with('TCPIP0::192.168.1.3::inst0::INSTR', open_timeout=10000)
        self.assertEqual(transport.query('*IDN?'), 'YOKOGAWA,WT310E,1,1')
        transport.close()
        resource.close.assert_called_once()
        manager.close.assert_called_once()
