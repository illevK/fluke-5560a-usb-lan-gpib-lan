"""Real adapters with memory SCPI peers: verify cross-device command ordering."""
import os
import tempfile
import unittest
from unittest.mock import patch
from calibration.domain.document import Document
from calibration.domain.planning import make_plan
from calibration.domain.types import ConnectionInfo
from calibration.domain.commands import MODEL_PROFILES
from calibration.instruments.fluke import Fluke5560AConnectionManager
from calibration.instruments.dut import DUTSession, DUTSettings, identify_dut
from calibration.services.drivers import FlukeAdapter, DUTAdapter
from calibration.services.engine import MeasurementEngine, State
from calibration.services.connections import DeviceHub, ConnectionSettings
from calibration.instruments.discovery import DUTDiscoveryCandidate
from tests.protocols.test_dut import FakeInstrument

class CalibratorPeer:
    def __init__(self, transcript, model):
        self.log, self.model = transcript, model
        self.active, self.zcomp, self.post = False, 'WIRE4', 'AUX'
        self.values = {'V': 0, 'HZ': 0}
        self.protection = False
        self.errors = []

    def write(self, command):
        self.log.append(('CAL', command))
        for part in command.split(';'):
            part = part.strip()
            if part.startswith('OUT '):
                self.values = MODEL_PROFILES[self.model].output_values(part)
            elif part == 'OPER':
                self.active = True
                if self.protection:
                    self.active = False
                    self.errors.append('4008,"Compliance"' if self.model == '5560A' else '1500,"Compliance"')
            elif part == 'STBY':
                self.active = False
            elif part.startswith('ZCOMP '):
                self.zcomp = part.split()[1]
            elif part.startswith('CUR_POST '):
                self.post = part.split()[1]

    def query(self, command, **kwargs):
        self.log.append(('CAL?', command))
        if command == 'OUT?':
            if 'OHM' in self.values:
                if self.model == '5730A':
                    return f'{self.values["OHM"]},OHM,0'
                return f'{self.values["OHM"]},OHM,0,0,0'
            return f'{self.values.get("V", 0)},V,{self.values.get("A", 0)},A,{self.values.get("HZ", 0)}'
        if command == 'ERR?':
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if self.model == '5730A' and command in ('ISR?', 'FAULT?'):
            return str(2048 + int(self.active)) if command == 'ISR?' else '0'
        return {'OPER?': str(int(self.active)), '*OPC?': '1', 'ZCOMP?': self.zcomp, 'CUR_POST?': self.post,
                '*IDN?': f'FLUKE,{self.model},TEST,1.0', 'FUNC?': 'DC_POWER' if 'A' in self.values and 'V' in self.values else 'DCV'}[command]

class DMMPeer:
    def __init__(self, transcript, model='34465A'):
        self.log, self.model = transcript, model
        self.identity = f'Keysight Technologies,{model},TEST,1.0'
        self.function = 'VOLT:DC'
        self.terminals = {'DC': '3', 'AC': '3'}
        self.null = '0'
        self.null_value = '0'
        self.settings = DUTSettings(mode='VISA', address='USB0::TEST::INSTR')

    def write(self, command):
        self.log.append(('DUT', command))
        if command.startswith('FUNC '):
            self.function = command.split(' ', 1)[1].strip('"')
        elif command.startswith('SENS:CURR:') and ':TERM ' in command:
            self.terminals[command.split(':')[2]] = command.split(' ')[1]
        elif command.startswith('RES:NULL:STAT '):
            self.null = '1' if command.endswith('ON') else '0'
        elif command.startswith('RES:NULL:VAL '):
            self.null_value = command.split(' ', 1)[1]

    def query(self, command, **kwargs):
        self.log.append(('DUT?', command))
        if command == 'FUNC?':
            return '"' + self.function + '"'
        if command.endswith('RANG:AUTO?'):
            return '1'
        if command.startswith('SENS:CURR:') and command.endswith('TERM?'):
            return self.terminals[command.split(':')[2]]
        return {'*IDN?': self.identity, 'ROUT:TERM?': 'FRON', 'READ?': '+1.000000001E+00',
            'RES:NULL:STAT?': self.null, 'RES:NULL:VAL:AUTO?': '0', 'RES:NULL:VAL?': self.null_value}[command]

    def close(self):
        pass

class DeviceWorkflowTests(unittest.TestCase):
    def peers(self, model='5560A', meter='34465A'):
        transcript = []
        manager = Fluke5560AConnectionManager()
        manager.profile = MODEL_PROFILES[model]
        manager.info = ConnectionInfo(True, 'TEST', 'MEMORY', f'FLUKE,{model},TEST,1.0')
        manager._transport = CalibratorPeer(transcript, model)
        manager._first_command_check = True
        session = DUTSession()
        session.transport = DMMPeer(transcript, meter)
        session.settings = session.transport.settings
        session.identity = session.transport.identity
        session.profile = identify_dut(session.identity)
        return FlukeAdapter(manager), DUTAdapter(session), transcript

    def execute(self, text, model='5560A', meter='34465A'):
        doc = Document()
        doc.model = model
        doc.options.dwell = .2
        doc.insert(doc.parse(text))
        cal, dut, transcript = self.peers(model, meter)
        events = []
        engine = MeasurementEngine(cal, dut, events.append)
        engine.run(make_plan(doc.rows, 0, doc.options, model, meter), doc.options)
        return engine, events, cal, dut, transcript

    def test_dc_ac_meter_configuration_is_verified_before_oper(self):
        for meter in ('34465A', '34461A', '34401A'):
            with self.subTest(meter=meter):
                engine, events, cal, dut, log = self.execute('1 V\n60 Hz\t2 V', meter=meter)
                self.assertEqual(engine.state, State.COMPLETE, [e.message for e in events if e.kind == 'state'])
                change = log.index(('DUT', 'FUNC "VOLT:AC"'))
                oper = next(i for i in range(change, len(log)) if log[i] == ('CAL', 'OPER'))
                self.assertIn(('CAL?', 'OPER?'), log[:change])
                self.assertIn(('DUT?', 'SENS:VOLT:AC:RANG:AUTO?'), log[change:oper])

    def test_selected_wt_and_ppa_connect_then_record_voltage_and_power(self):
        wt = FakeInstrument('YOKOGAWA,WT310E,TEST,1', replies={':COMMUNICATE:HEADER?': '0',
            ':RATE?': '0.1', ':INPUT:MODE?': 'RMS', ':INPUT:SYNCHRONIZE?': 'VOLT',
            ':INPUT:VOLTAGE:AUTO?': '1', ':INPUT:CURRENT:AUTO?': '1', ':HOLD?': '0',
            ':MEASURE:MHOLD?': '0', ':STATUS:FILTER1?': 'FALL', ':STATUS:EESR?': '1'})
        ppa = FakeInstrument('NEWTONS4TH,PPA2530 KinetiQ,TEST,1', replies={'CONFIG,13?': '3',
            'CONFIG,28?': '0', 'CONFIG,29?': '0', 'CONFIG,40?': '0', 'COUPLI,PHASE1?': '0',
            'DAV?': '3', 'STATUS,CH1?': '1,300V,OK', 'STATUS,CH2?': '1,10A,OK'})
        cases = [('WT', wt, DUTSettings(mode='VISA', address='USB0::TEST::INSTR')),
                 ('PPA', ppa, DUTSettings(mode='PPA_LAN', address='192.0.2.8', port=10001))]
        for family, peer, settings in cases:
            with self.subTest(family=family), tempfile.TemporaryDirectory() as folder, \
                    patch.dict(os.environ, {'CALIBRATION_DATA_DIR': folder}):
                hub = DeviceHub()
                peer.settings = settings
                hub.session = DUTSession(lambda *_: peer)
                candidate = DUTDiscoveryCandidate(settings, identify_dut(peer.identity), peer.identity)
                with patch('calibration.services.connections.discover_duts', return_value=([candidate], [])) as search:
                    hub.connect_dut(ConnectionSettings(model=family))
                self.assertEqual(search.call_args.kwargs['model_filter'], family)
                self.assertEqual(hub.dut.model, candidate.profile.model)
                doc = Document()
                doc.options.dwell = .2
                doc.insert(doc.parse('60 Hz\t120 V\n120 V / 1 A\t60 Hz\t120 W'))
                cal, _, _ = self.peers()
                events = []
                engine = MeasurementEngine(cal, hub.dut, events.append)
                engine.run(make_plan(doc.rows, 0, doc.options, cal.model, hub.dut.model), doc.options)
                self.assertEqual(engine.state, State.COMPLETE, [e.message for e in events])
                self.assertEqual([e.measured for e in events if e.kind == 'result'], ['120 V', '60 W'])
                self.assertFalse(cal.output_state())
                hub.disconnect('dut')

    def test_exact_three_amps_sets_ten_amp_terminal_before_output(self):
        for meter in ('34461A', '34465A'):
            with self.subTest(meter=meter):
                engine, events, cal, dut, log = self.execute('3 A', meter=meter)
                self.assertEqual(engine.state, State.COMPLETE, [e.message for e in events if e.kind == 'state'])
                configure = log.index(('DUT', 'SENS:CURR:DC:TERM 10'))
                out = next(i for i, value in enumerate(log) if value[0] == 'CAL' and value[1].startswith('OUT '))
                self.assertLess(configure, out)
                self.assertIn(('DUT?', 'ROUT:TERM?'), log[:configure])

    def test_34461a_two_wire_null_and_ten_amp_limit(self):
        engine, events, cal, dut, log = self.execute('10 Mohm', meter='34461A')
        self.assertEqual(engine.state, State.COMPLETE, [e.message for e in events])
        self.assertIn(('DUT', 'RES:NULL:STAT ON'), log)
        self.assertIn(('CAL', 'OUT 10000000 OHM; ZCOMP NONE'), log)
        engine, events, cal, dut, log = self.execute('11 A', meter='34461A')
        self.assertEqual(engine.state, State.ERROR)
        self.assertNotIn(('CAL', 'OPER'), log)

    def test_resistance_zero_and_off_compensation_on_all_supported_sources(self):
        for model in MODEL_PROFILES:
            with self.subTest(model=model):
                engine, events, cal, dut, log = self.execute('0 ohm\n10 Mohm', model)
                self.assertEqual(engine.state, State.COMPLETE, [e.message for e in events if e.kind == 'state'])
                outputs = [command for who, command in log if who == 'CAL' and command.startswith('OUT ')]
                expected = (['OUT 0 OHM; *WAI; EXTSENSE OFF; *WAI; RCOMP OFF'] * 2 + ['OUT 10000000 OHM; *WAI; EXTSENSE OFF']
                            if model == '5730A' else ['OUT 0 OHM; ZCOMP NONE'] * 2 + ['OUT 10000000 OHM; ZCOMP NONE'])
                self.assertEqual(outputs, expected)
                self.assertEqual(sum(who == 'DUT' and command.startswith('RES:NULL:VAL ') for who, command in log), 1)
                null_index = next(i for i, value in enumerate(log) if value[0] == 'DUT' and value[1].startswith('RES:NULL:VAL '))
                self.assertIn(('CAL', 'STBY'), log[:null_index])
                self.assertFalse(cal.output_state())

    def test_5730a_ac_current_uses_normal_post_before_output_and_records_dut(self):
        engine, events, cal, dut, log = self.execute('60 Hz\t0.5 A\n60 Hz\t1 A', '5730A')
        self.assertEqual(engine.state, State.COMPLETE)
        self.assertEqual(len([e for e in events if e.kind == 'result' and e.measured]), 2)
        self.assertLess(log.index(('CAL?', 'CUR_POST?')), log.index(('CAL', 'OPER')))
        self.assertFalse(cal.output_state())

    def test_protection_error_does_not_drop_working_connection(self):
        cal, dut, log = self.peers()
        cal.manager._transport.protection = True
        doc = Document()
        doc.options.dwell = .2
        doc.insert(doc.parse('1 V\n2 V'))
        engine = MeasurementEngine(cal, dut)
        engine.run(make_plan(doc.rows, 0, doc.options, '5560A', '34465A'), doc.options)
        self.assertEqual(engine.state, State.ERROR)
        self.assertTrue(cal.connected)
        self.assertFalse(cal.output_state())
        self.assertEqual(sum(who == 'CAL' and cmd == 'OPER' for who, cmd in log), 1)
