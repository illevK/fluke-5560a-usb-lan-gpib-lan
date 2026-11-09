"""DUT protocol and GUI tests. No instrument, desktop input, or real clipboard."""
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


class FakeInstrument:
    def __init__(self, identity='YOKOGAWA,WT310E,123,1.00', bad_item=False, replies=None):
        self.identity, self.bad_item = identity, bad_item
        self.commands, self.items = [], {}
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
        if command.startswith(':NUMERIC:NORMAL:ITEM'):
            key, value = command.split(' ', 1)
            self.items[key] = value

    def query(self, command):
        self.commands.append(command)
        if self.fail:
            raise TimeoutError('test timeout')
        if command == '*IDN?':
            return self.identity
        if command in self.replies:
            return self.replies[command]
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
    def test_app_close_stops_calibrator_sequence_before_waiting_for_dut(self):
        order = []
        app = SimpleNamespace(_io_busy=False, _sequence_dut_reserved=lambda: False,
            _on_seq_stop_auto=lambda **_: order.append('stop sequence'),
            _dut_close_ready=lambda: order.append('close DUT') or False,
            after=Mock(), _on_close=Mock(), conn_mgr=Mock())
        f.Fluke5560AControllerApp._on_close(app)
        self.assertEqual(order, ['stop sequence', 'close DUT'])
        app.conn_mgr.disconnect.assert_not_called()
        app.after.assert_called_once_with(200, app._on_close)

    def make_session(self, instrument=None):
        instrument = instrument or FakeInstrument()
        session = f.DUTSession(lambda *_: instrument)
        session.connect(f.DUTSettings(address='GPIB0::4::INSTR'))
        return session, instrument

    def test_requested_and_related_model_identification(self):
        for model in ('510', '550', 'PPA510', 'PPA550', '1530', '2530', '2630', '3530', '4530', '5530'):
            self.assertEqual(f.identify_dut(f'NEWTONS4TH,{model},123,1.00').family, 'PPA')
        for model, channels in [('WT310', 1), ('WT310E', 1), ('WT310HC', 1), ('WT310EH', 1),
                                ('WT332E', 2), ('WT333', 3), ('760203', 3), ('WT500', 3), ('WT1806E', 6)]:
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

    def test_wt3000_family_idn_suffixes_and_element_counts(self):
        # Model fields follow IM 760301-17E and IM WT3001E-17EN, §6.24.
        for channels in range(1, 5):
            for model, expected, firmware in (
                    (f'76030{channels}-0{channels}-SV', f'WT3000 (76030{channels})', 'F4.01'),
                    (f'76030{channels}-{channels}0-MV', f'WT3000 (76030{channels})', 'F4.01'),
                    (f'WT300{channels}E-2A0-30A{channels}', f'WT300{channels}E', 'F6.01')):
                with self.subTest(model=model):
                    profile = f.identify_dut(f'YOKOGAWA,{model},0,{firmware}')
                    self.assertEqual((profile.family, profile.model, profile.channels), ('WT', expected, channels))
                    self.assertTrue(profile.wt3000_numeric)

    def test_wt3000_reads_selected_element_modes_and_checks_total_order(self):
        for identity in ('YOKOGAWA,760304-04-SV,0,F4.01', 'YOKOGAWA,WT3004E-2A0-30A4,0,F6.01'):
            with self.subTest(identity=identity):
                instrument = FakeInstrument(identity, replies={
                    ':INPUT:VOLTAGE:MODE:ELEMENT4?': 'mean',
                    ':INPUT:CURRENT:MODE:ELEMENT4?': 'DC',
                    ':NUMERIC:NORMAL:ITEM1?': 'U,4,TOTAL',
                    ':NUMERIC:NORMAL:ITEM2?': 'I,4,TOT',
                    ':NUMERIC:NORMAL:ITEM6?': 'LAMB,4,TOT',
                    ':NUMERIC:NORMAL:ITEM7?': 'FU,4',
                })
                session, _ = self.make_session(instrument)
                sample = session.read(4)
                self.assertEqual(sample.vi_modes, ('MEAN', 'DC'))
                self.assertEqual((sample.channel, sample.values['voltage'], sample.values['current']), (4, 120, 1))
                self.assertEqual(instrument.commands[4:13], [
                    ':INPUT:VOLTAGE:MODE:ELEMENT4?', ':INPUT:CURRENT:MODE:ELEMENT4?',
                    ':NUMERIC:NORMAL:ITEM1 U,4,TOTAL', ':NUMERIC:NORMAL:ITEM2 I,4,TOTAL',
                    ':NUMERIC:NORMAL:ITEM3 P,4,TOTAL', ':NUMERIC:NORMAL:ITEM4 S,4,TOTAL',
                    ':NUMERIC:NORMAL:ITEM5 Q,4,TOTAL', ':NUMERIC:NORMAL:ITEM6 LAMBDA,4,TOTAL',
                    ':NUMERIC:NORMAL:ITEM7 FU,4',
                ])
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
        self.assertFalse(any(':MODE:ELEMENT' in command for command in instrument.commands))
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
                self.assertEqual(sample.values['pf'], .5)
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
        self.assertEqual(sample.values, dict(voltage=120, current=1, power=60, apparent=120, reactive=103.923048, pf=.5, frequency=60))
        self.assertIn('순차', sample.note)
        self.assertEqual(instrument.commands, ['*IDN?', 'RESOLU,HIGH', 'POWER,PHASE1,RMS?', 'POWER,PHASE1,WATTS?'])

    def test_mapping_failure_and_timeout_close_stream_and_clear_sample(self):
        session, instrument = self.make_session(FakeInstrument(bad_item=True))
        with self.assertRaises(ValueError): session.read()
        self.assertTrue(instrument.closed)
        self.assertNotIn(':NUMERIC:NORMAL:VALUE?', instrument.commands)
        session, instrument = self.make_session()
        session.read()
        instrument.fail = True
        with self.assertRaises(TimeoutError): session.read()
        self.assertFalse(session.connected)
        self.assertIsNone(session.sample)

    def test_invalid_values_and_record_length(self):
        self.assertEqual(f.dut_numbers('NAN,INF,-INF,9.91E37,1', 5), [None, None, None, None, 1])
        for bad in ('1,2', '1,bad,3', ':HEADER 1,2,3'):
            with self.assertRaises(ValueError): f.dut_numbers(bad, 3)
        session, instrument = self.make_session()
        instrument.invalid = True
        self.assertIsNone(session.read().values['pf'])

    def test_bad_channel_sends_no_commands(self):
        session, instrument = self.make_session()
        with self.assertRaises(ValueError): session.read(2)
        self.assertEqual(instrument.commands, ['*IDN?'])

    def test_settings_and_demo_cannot_open_any_resource(self):
        factory = Mock(side_effect=AssertionError('demo must be offline'))
        session = f.DUTSession(factory)
        session.connect(f.DUTSettings(mode='DEMO'))
        self.assertTrue(session.read().simulated)
        factory.assert_not_called()
        for settings in (f.DUTSettings(timeout=float('nan')), f.DUTSettings(mode='PPA_LAN', address='192.168.1.3', port=0),
                         f.DUTSettings(mode='SERIAL', address='COM0'), f.DUTSettings(address='ASRL1::INSTR')):
            with self.assertRaises(ValueError): settings.validate()

    def test_cancellation_does_not_retry(self):
        session, instrument = self.make_session()
        session.cancel.set()
        with self.assertRaises(InterruptedError): session.read()
        self.assertTrue(instrument.closed)
        self.assertIsNone(session.sample)
        self.assertEqual(instrument.commands, ['*IDN?'])

    def test_wt_linked_read_discards_old_event_and_keeps_signed_dc(self):
        class UpdatingWT(FakeInstrument):
            settings = SimpleNamespace(timeout=1.0)
            events = iter(("1", "8", "1", "0", "1"))
            event_count = 0
            event_filter = "RISE"

            def write(self, command):
                super().write(command)
                if command.startswith(":STATUS:FILTER1 "):
                    self.event_filter = command.split()[1]

            def query(self, command):
                if command in (":HOLD?", ":MEASURE:MHOLD?", ":STATUS:FILTER1?", ":STATUS:EESR?", ":NUMERIC:NORMAL:VALUE?"):
                    self.commands.append(command)
                    if command in (":HOLD?", ":MEASURE:MHOLD?"):
                        return "0"
                    if command == ":STATUS:FILTER1?":
                        return self.event_filter
                    if command == ":STATUS:EESR?":
                        self.event_count += 1
                        return next(self.events)
                    if self.event_count != 5:
                        raise AssertionError("Numeric data read before both fresh updates")
                    return "120,1,60,120,0,0.5,60,-119.99,-0.9999"
                return super().query(command)

        session, instrument = self.make_session(UpdatingWT())
        sample = session.read(include_dc=True, fresh=True)
        self.assertEqual((sample.values["dc_voltage"], sample.values["dc_current"]), (-119.99, -0.9999))
        self.assertEqual(instrument.items[":NUMERIC:NORMAL:ITEM8"], "UDC,1")
        self.assertEqual(instrument.items[":NUMERIC:NORMAL:ITEM9"], "IDC,1")
        self.assertEqual(instrument.event_filter, "RISE")

    def test_wt_linked_read_refuses_held_data(self):
        for settings, message in (({":HOLD?": "1"}, "HOLD"), ({":HOLD?": "0", ":MEASURE:MHOLD?": "1"}, "MAX HOLD")):
            with self.subTest(settings=settings):
                session, instrument = self.make_session(FakeInstrument(replies=settings))
                with self.assertRaisesRegex(ValueError, message):
                    session.read(fresh=True)
                self.assertNotIn(":NUMERIC:NORMAL:VALUE?", instrument.commands)
                self.assertTrue(instrument.closed)

    def test_wt3000_signed_dc_only_when_element_mode_is_dc(self):
        session, _ = self.make_session(FakeInstrument("YOKOGAWA,760301,0,F4.01", replies={
            ":INPUT:VOLTAGE:MODE:ELEMENT1?": "RMS",
            ":INPUT:CURRENT:MODE:ELEMENT1?": "DC",
        }))
        sample = session.read(include_dc=True)
        self.assertNotIn("dc_voltage", sample.values)
        self.assertEqual(sample.values["dc_current"], 1)

    def test_ppa_linked_read_waits_for_new_data_without_trigger(self):
        class UpdatingPPA(FakeInstrument):
            settings = SimpleNamespace(timeout=1.0)
            flags = iter(("2", "3", "2", "3"))
            rms_values = iter(("60,1,0,2,0", "60,5,0,2,0", "60,120,-119.9,1,-0.999"))

            def query(self, command):
                if command in ("DAV?", "POWER,PHASE1,RMS?"):
                    self.commands.append(command)
                    return next(self.flags if command == "DAV?" else self.rms_values)
                return super().query(command)

        session, instrument = self.make_session(UpdatingPPA("NEWTONS4TH,550,1,1",
            replies={"COUPLI,PHASE1?": "0", "STATUS,CH1?": "6,300V,OK", "STATUS,CH2?": "3,10A,OVER"}))
        sample = session.read(include_dc=True, fresh=True)
        self.assertEqual((sample.values["voltage"], sample.values["dc_voltage"], sample.values["dc_current"]),
                         (120, -119.9, None))
        self.assertIsNone(sample.values["power"])
        self.assertEqual(instrument.commands[-10:],
            ["POWER,PHASE1,RMS?", "DAV?", "DAV?", "POWER,PHASE1,RMS?", "DAV?", "DAV?",
             "POWER,PHASE1,RMS?", "POWER,PHASE1,WATTS?", "STATUS,CH1?", "STATUS,CH2?"])

    def test_tcp_fragmented_crlf_and_consecutive_queries(self):
        sock = Mock()
        sock.recv.side_effect = [b'NEWTONS', b'4TH,550,1,1\r', b'\n60,120,', b'0,1,0\r\n']
        transport = f.DUTTransport(f.DUTSettings(mode='PPA_LAN', address='192.168.1.3', port=10001), threading.Event())
        with patch.object(f.socket, 'create_connection', return_value=sock): transport.open()
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
                connect.assert_called_once_with(('192.0.2.41', 10002), timeout=10.0)
                sock.sendall.assert_called_once_with(b'*IDN?\n')
                sock.close.assert_called_once()

    def test_serial_configuration_and_no_telnet_negotiation(self):
        serial_port = Mock(in_waiting=1)
        serial_port.write.return_value = 7
        serial_port.read.side_effect = [b'O', b'K\r\n']
        transport = f.DUTTransport(f.DUTSettings(mode='SERIAL', address='COM5', baudrate=38400, flow='RTS/CTS'), threading.Event())
        with patch.object(f.serial, 'Serial', return_value=serial_port) as constructor:
            transport.open()
        self.assertTrue(constructor.call_args.kwargs['rtscts'])
        self.assertEqual(transport.query('*IDN?'), 'OK')
        serial_port.write.assert_called_once_with(b'*IDN?\r\n')
        transport.close()

    def test_visa_vxi11_and_cleanup_on_failed_configuration(self):
        manager, resource = Mock(), Mock()
        manager.open_resource.return_value = resource
        resource.read.return_value = 'YOKOGAWA,WT310E,1,1\n'
        transport = f.DUTTransport(f.DUTSettings(mode='WT_LAN', address='192.168.1.3', eol='LF'), threading.Event())
        with patch.object(f.pyvisa, 'ResourceManager', return_value=manager): transport.open()
        manager.open_resource.assert_called_once_with('TCPIP0::192.168.1.3::inst0::INSTR', open_timeout=10000)
        self.assertEqual(transport.query('*IDN?'), 'YOKOGAWA,WT310E,1,1')
        transport.close()
        resource.close.assert_called_once()
        manager.close.assert_called_once()


class DUTGuiTests(unittest.TestCase):
    def setUp(self):
        with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]), patch.object(f, 'SAMPLE_POWER_TABLE', '1 V'):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.app.conn_mgr.send_command = Mock(side_effect=AssertionError('DUT must not operate calibrator'))

    def pump(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def tearDown(self):
        self.app._on_close()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                if not self.app.winfo_exists(): break
                self.app.update()
            except f.tk.TclError:
                break
            time.sleep(.01)

    def demo(self):
        self.app.var_dut_mode.set('미리보기 · 가상 DUT')
        self.app._dut_connect()
        self.pump(lambda: not self.app._dut_busy)

    def test_dut_cannot_open_the_connected_calibrator_endpoint(self):
        app = self.app
        with patch.object(app, '_dut_start') as start:
            for mode, address, resource, ip in (
                    ('USB / GPIB · VISA', 'USB0::0x0F7E::0x800A::7165657::INSTR', 'USB0::0x0F7E::0x800A::7165657::INSTR', ''),
                    ('COM · USB / RS232', 'COM5', 'COM5 / 9600 / 8N1 / NONE', ''),
                    ('LAN · PPA', '192.168.1.3', 'TCPIP::192.168.1.3::3490::SOCKET', '192.168.1.3')):
                app.conn_mgr.info = f.ConnectionInfo(True, 'test', resource, 'FLUKE,5560A,1,1', ip)
                app.var_dut_mode.set(mode)
                app.var_dut_address.set(address)
                app._dut_connect()
                self.assertIn('기준기로 연결한 주소', app.var_dut_status.get())
            start.assert_not_called()
            app.var_dut_address.set('192.168.1.4')
            app._dut_connect()
            start.assert_called_once()
        app.conn_mgr.info = f.ConnectionInfo()

    def test_demo_reads_and_preserves_calibrator_and_measurement_table(self):
        app = self.app
        app.seq_steps[0].measured_raw = 'user entry'
        self.demo()
        app._dut_start('read')
        self.pump(lambda: not app._dut_busy)
        self.assertIn('가상', app.dut_tree.item('voltage', 'values')[3])
        self.assertEqual(app.dut_tree.item('voltage', 'values')[1], '120')
        self.assertEqual(app.seq_steps[0].measured_raw, 'user entry')
        app.conn_mgr.send_command.assert_not_called()
        app._dut_received_monotonic -= 6
        self.pump(lambda: app._dut_old)
        self.assertIn('이전', app.dut_tree.item('voltage', 'values')[3])
        app._dut_disconnect()
        self.pump(lambda: not app._dut_busy)
        self.assertEqual(app.dut_tree.item('voltage', 'values')[1], '—')

    def test_auto_refresh_failure_stops_and_removes_old_values(self):
        self.demo()
        app = self.app
        app.var_dut_auto.set(True)
        app._dut_auto_changed()
        self.pump(lambda: app._dut_received_monotonic > 0)
        def fail(channel):
            app.dut_session.disconnect()
            raise TimeoutError('test disconnect')
        app.dut_session.read = fail
        app._dut_next_read = 0
        self.pump(lambda: not app.var_dut_auto.get())
        self.assertIn('test disconnect', app.var_dut_status.get())
        self.assertEqual(app.dut_tree.item('power', 'values')[1], '—')

    def test_disconnect_during_worker_discards_late_sample(self):
        self.demo()
        app = self.app
        started, release = threading.Event(), threading.Event()
        original = app.dut_session.read
        def slow_read(channel):
            sample = original(channel)
            started.set()
            release.wait(3)
            return sample
        app.dut_session.read = slow_read
        app._dut_start('read')
        self.assertTrue(started.wait(2))
        app._dut_disconnect()
        release.set()
        self.pump(lambda: not app._dut_busy)
        self.assertFalse(app.dut_session.connected)
        self.assertEqual(app.dut_tree.item('voltage', 'values')[1], '—')

    def test_copy_header_and_row_to_bottom_preserve_blanks(self):
        grid = self.app.excel_grid
        rows = [f.SmartClipboardSequenceParser.build_Single_step_from_fields(i, '-', 'DC', '-', '1 V') for i in (1,2,3)]
        rows[0].measured_raw, rows[2].measured_raw = '01.20', '3.0'
        self.app.seq_steps = rows
        grid.render_steps(rows)
        copied = []
        grid.clipboard_clear = lambda: None
        grid.clipboard_append = copied.append
        header_label = grid.header_frame.winfo_children()[9].winfo_children()[0]
        self.assertTrue(header_label.bind('<Button-1>'))
        # Check the binding target without needing a visible desktop window.
        callback = header_label.bind('<Button-1>').split()[1]
        self.assertIn('_copy_all_measurements', callback)
        grid._copy_all_measurements()
        grid.selected_row_idx = 1
        grid._copy_measurements_to_end()
        self.assertEqual(copied, ['01.20\r\n\r\n3.0\r\n', '\r\n3.0\r\n'])
        self.assertEqual([s.measured_raw for s in rows], ['01.20', '', '3.0'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
