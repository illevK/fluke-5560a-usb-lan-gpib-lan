"""All peers are in-memory or localhost. Never connect to physical equipment."""
import sys
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f
from test_communications import FakeInstrument
import test_gui_async as gui


class SerialPeer:
    def __init__(self, model='5522A', echo=True, missing=False, error='0,"No error"'):
        self.model, self.echo, self.missing, self.error = model, echo, missing, error
        self.is_open = True
        self.pending = bytearray()
        self.commands = []
        self.timeout = .2
        self.write_timeout = 1
        self.operating = False
        self.output_seen = False

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, size):
        if not self.pending:
            time.sleep(min(self.timeout, .01))
            return b''
        data = bytes(self.pending[:min(size, 3)])  # fragmented responses
        del self.pending[:len(data)]
        return data

    def write(self, data):
        command = data.decode('ascii').strip()
        self.commands.append(command)
        if self.echo:
            self.pending.extend((command+'\r\n').encode())
        if command.startswith('OUT '):
            self.output_seen = True
        if 'OPER' in command and '?' not in command:
            self.operating = True
        if command in ('STBY', '*RST'):
            self.operating = False
        response = {'*IDN?': f'FLUKE,{self.model},TEST,1.0', '*OPC?': '1',
                    'COMM_MODE? SERIAL': 'COMP', 'OPER?': '1' if self.operating else '0',
                    'ERR?': self.error, 'OUT?': '1 A, 60 HZ'}.get(command)
        if command == 'ERR?':
            self.error = '0,"No error"'
        if self.missing and self.output_seen and command == '*OPC?':
            response = None
        if response is not None:
            self.pending.extend((response+'\r\n').encode())
        if self.echo:
            self.pending.extend(b'=> ')
        return len(data)

    def close(self):
        self.is_open = False


class SerialTests(unittest.TestCase):
    def connect(self, peer, **kwargs):
        backend = Mock()
        backend.Serial.return_value = peer
        self.addCleanup(patch.stopall)
        patch.object(f, 'serial', backend).start()
        patch.object(f, 'HAS_PYSERIAL', True).start()
        mgr = f.Fluke5560AConnectionManager()
        self.addCleanup(lambda: mgr.disconnect(standby=False))
        mgr.scan_and_auto_connect('RS232', model=peer.model, serial_port='COM7',
                                 timeout_sec=.2, operation_timeout_sec=1, **kwargs)
        return mgr, backend

    def test_all_models_serial_identification_echo_fragmentation_and_output(self):
        for model in f.MODEL_PROFILES:
            with self.subTest(model=model):
                peer = SerialPeer(model)
                mgr, backend = self.connect(peer)
                self.assertEqual(mgr.profile.model, model)
                self.assertEqual(peer.commands[0], '*IDN?')
                self.assertEqual('COMM_MODE? SERIAL' in peer.commands, model == '5560A')
                mgr.send_command('OUT 1 A, 60 HZ; OPER')
                self.assertEqual(mgr.query('OPER?'), '1')
                mgr.disconnect()
                self.assertIn('STBY', peer.commands)
                self.assertIn('LOCAL', peer.commands)
                self.assertFalse(peer.is_open)
                self.assertEqual(backend.Serial.call_args.kwargs['port'], 'COM7')

    def test_serial_settings_pass_through_without_modifying_instrument(self):
        peer = SerialPeer('5080A', echo=False)
        mgr, backend = self.connect(peer, baudrate=19200, data_bits=7, parity='E', stop_bits=2, flow_control='RTS/CTS')
        settings = backend.Serial.call_args.kwargs
        self.assertEqual((settings['baudrate'], settings['bytesize'], settings['parity'], settings['stopbits']), (19200,7,'E',2))
        self.assertTrue(settings['rtscts'])
        self.assertFalse(settings['xonxoff'])
        self.assertFalse(any(c.startswith('SP_SET ') for c in peer.commands))

    def test_wrong_model_is_closed_without_remote_or_output(self):
        peer = SerialPeer('5080A')
        backend = Mock(Serial=Mock(return_value=peer))
        with patch.object(f, 'serial', backend), patch.object(f, 'HAS_PYSERIAL', True):
            mgr = f.Fluke5560AConnectionManager()
            with self.assertRaises(ConnectionError):
                mgr.scan_and_auto_connect('RS232', model='5522A', serial_port='COM7', timeout_sec=.2)
        self.assertEqual(peer.commands, ['*IDN?'])
        self.assertFalse(peer.is_open)

    def test_output_timeout_attempts_standby_closes_and_never_replays(self):
        peer = SerialPeer(missing=True)
        mgr, _ = self.connect(peer)
        with self.assertRaises(TimeoutError):
            mgr.send_command('OUT 1 A, 60 HZ; OPER')
        self.assertEqual(peer.commands.count('OUT 1 A, 60 HZ; OPER'), 1)
        self.assertEqual(peer.commands[-1], 'STBY')
        self.assertFalse(peer.is_open)
        self.assertFalse(mgr.info.connected)

    def test_serial_wait_can_be_cancelled(self):
        peer = SerialPeer(missing=True)
        mgr, _ = self.connect(peer)
        mgr.operation_timeout = 60
        failures = []
        def output():
            try: mgr.send_command('OUT 1 A, 60 HZ; OPER')
            except Exception as exc: failures.append(exc)
        thread = threading.Thread(target=output)
        thread.start()
        deadline = time.monotonic() + 1
        while not peer.output_seen and time.monotonic() < deadline: time.sleep(.005)
        mgr.request_abort()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(failures[0], f.OperationCancelled)
        self.assertEqual(peer.commands[-1], 'STBY')

    def test_serial_device_error_preserves_confirmed_standby_connection(self):
        peer = SerialPeer(error='4103,"Settling error"')
        mgr, _ = self.connect(peer)
        with self.assertRaises(f.InstrumentCommandError): mgr.send_command('OUT 1 A, 60 HZ; OPER')
        self.assertTrue(mgr.info.connected)
        self.assertEqual(mgr.query('OPER?'), '0')

    def test_missing_library_and_invalid_baud_explain_connection_failure(self):
        with patch.object(f, 'HAS_PYSERIAL', False):
            mgr = f.Fluke5560AConnectionManager()
            with self.assertRaisesRegex(ConnectionError, 'pyserial'):
                mgr.scan_and_auto_connect('RS232', serial_port='COM1')
        peer = SerialPeer()
        with self.assertRaises(ConnectionError): self.connect(peer, baudrate=115200)


class ModelTransportTests(unittest.TestCase):
    def test_5080_lan_does_not_send_5560_comm_mode(self):
        with FakeInstrument(identity='FLUKE,5080A,TEST,1.0', fragmented=True) as server:
            mgr = f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect('LAN', '127.0.0.1', server.port, model='5080A', timeout_sec=1)
            self.assertEqual(server.commands, ['*IDN?', 'REMOTE', '*OPC?'])
            mgr.send_command('OUT 1 A, 60 HZ; CUR_POST AUX; LCOMP ON; OPER')
            mgr.disconnect()
            self.assertFalse(any(c.startswith('COMM_MODE') for c in server.commands))

    def test_wrong_selected_model_over_lan_receives_only_idn(self):
        with FakeInstrument(identity='FLUKE,5080A,TEST,1.0') as server:
            mgr = f.Fluke5560AConnectionManager()
            with self.assertRaises(ConnectionError): mgr.scan_and_auto_connect('LAN', '127.0.0.1', server.port, model='5560A')
            self.assertEqual(server.commands, ['*IDN?'])

    def test_supported_native_visa_paths_assert_gpib_remote_without_serial_commands(self):
        for model, mode in [('5560A','USB'),('5560A','GPIB'),('5522A','GPIB'),('5520A','GPIB')]:
            with self.subTest(model=model, mode=mode):
                visa = Mock()
                resource = 'USB0::TEST::INSTR' if mode == 'USB' else 'GPIB1::8::INSTR'
                rm = visa.ResourceManager.return_value
                inst = rm.open_resource.return_value
                inst.query.side_effect = lambda cmd: {'*IDN?': f'FLUKE,{model},TEST,1.0', '*OPC?':'1', 'ERR?':'0,"No error"'}[cmd]
                with patch.object(f,'pyvisa',visa), patch.object(f,'HAS_PYVISA',True):
                    mgr=f.Fluke5560AConnectionManager()
                    mgr.scan_and_auto_connect(mode, model=model, visa_resource=resource)
                    mgr.send_command('OUT 1 A, 0 HZ')
                    self.assertNotIn('REMOTE', [c.args[0] for c in inst.write.call_args_list])
                    if mode == 'GPIB': inst.control_ren.assert_called_with(visa.constants.RENLineOperation.asrt_address)
                    mgr.disconnect(standby=False)
                inst.close.assert_called_once()
                rm.close.assert_called_once()

    def test_unsupported_interfaces_rejected_before_open(self):
        with patch.object(f.socket, 'create_connection') as tcp:
            for model, interface in [('5520A','LAN'), ('5522A','USB'), ('5080A','USB'), ('5080A','GPIB')]:
                with self.subTest(model=model, interface=interface), self.assertRaises(ValueError):
                    f.Fluke5560AConnectionManager().scan_and_auto_connect(interface, model=model)
            tcp.assert_not_called()

    def test_simulation_all_models_and_limits_before_output(self):
        for model in f.MODEL_PROFILES:
            with self.subTest(model=model):
                mgr=f.Fluke5560AConnectionManager()
                mgr.scan_and_auto_connect('SIMULATION', model=model)
                self.assertIn(model, mgr.query('*IDN?'))
                mgr.send_command('OUT 20 A, 0 HZ; OPER')
                self.assertEqual(mgr.query('OPER?'), '1')
                before=mgr.query('OUT?')
                with self.assertRaises(ValueError): mgr.send_command('OUT 31 A, 0 HZ; OPER')
                self.assertEqual(mgr.query('OUT?'), before)
                with self.assertRaises(ValueError): mgr.query('OUT 31 A; OPER; *OPC?')
                mgr.disconnect()


class ModelCommandTests(unittest.TestCase):
    def current(self, model, amps=100, hz=60, coil=True):
        return f.CalibratorCommandBuilder(model).build_current_cmd(mode='ACI',input_value=amps,unit='A',freq_hz=hz,use_coil=coil,coil_turns=50)[0]

    def test_coil_and_lcomp_current_and_power_for_all_models(self):
        for model in f.MODEL_PROFILES:
            with self.subTest(model=model):
                cmd=self.current(model)
                self.assertIn('OUT 2 A, 60 HZ',cmd)
                if model == '5560A': self.assertNotIn('LCOMP', cmd)
                else: self.assertIn('LCOMP ON', cmd)
                power=f.CalibratorCommandBuilder(model).build_power_cmd(mode='ACP',calc_method='VI',voltage_v=120,current_or_power_val=100,use_coil=True,coil_turns=50)
                self.assertEqual(power['actual_current_a'],2)
                self.assertEqual('LCOMP ON' in power['cmd'],model!='5560A')

    def test_lcomp_frequency_edges_and_noncoil_switch_off(self):
        for model in ('5522A','5520A'):
            self.assertIn('LCOMP ON',self.current(model,amps=10,hz=439))
            self.assertIn('LCOMP OFF',self.current(model,amps=10,hz=440))
            self.assertIn('LCOMP ON',self.current(model,amps=16.5,hz=999))
            self.assertIn('LCOMP OFF',self.current(model,amps=16.5,hz=1000))
        self.assertIn('LCOMP ON',self.current('5080A',hz=64))
        self.assertIn('LCOMP OFF',self.current('5080A',hz=65))
        self.assertIn('LCOMP OFF',self.current('5520A',amps=1,coil=False))

    def test_model_current_caps_and_terminal_selection(self):
        for model in ('5522A','5520A','5080A'):
            with self.subTest(model=model):
                self.assertIn('CUR_POST A20',self.current(model,amps=3,coil=False))
                self.assertIn('CUR_POST AUX',self.current(model,amps=2,coil=False))
                self.current(model,amps=20.5,coil=False)
                with self.assertRaises(ValueError): self.current(model,amps=20.5001,coil=False)
        self.current('5560A',amps=30,coil=False)
        with self.assertRaises(ValueError): f.MODEL_PROFILES['5560A'].validate_command('OUT 1 A, 60 HZ; CUR_POST A20')

    def test_fixed_resistors_and_model_compensation_limits(self):
        builder=f.CalibratorCommandBuilder('5080A')
        for value in f.RESISTORS_5080A:
            command,_=builder.build_resistance_cmd(value=value,unit='OHM',zcomp='WIRE4')
            self.assertIn('ZCOMP NONE' if value>=1e6 else 'ZCOMP WIRE4',command)
        with self.assertRaises(ValueError): builder.build_resistance_cmd(value=500,unit='OHM')
        for model in ('5520A','5522A'):
            command,_=f.CalibratorCommandBuilder(model).build_resistance_cmd(value=110000,unit='OHM',zcomp='WIRE4')
            self.assertIn('ZCOMP NONE',command)

    def test_parser_applies_coil_and_three_amp_groups_for_older_models(self):
        for model in ('5522A','5520A','5080A'):
            steps=f.SmartClipboardSequenceParser.build_sequence_steps('0.1 A\n1 A\n3 A\n5 A\n2 A',model=model)
            self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s,model=model)],[1,2,5])
            self.assertEqual([s.step_no for s in steps if f.SequenceCurrentFilter.includes(s,True,f.SequenceCurrentFilter.POSTS,model)],[3,4])
            step=f.SmartClipboardSequenceParser.build_Single_step_from_fields(1,'-','60 Hz','-','100 A',use_coil=True,coil_turns=50,model=model)
            self.assertIn('LCOMP ON',step.scpi_cmd)
            self.assertEqual(f.SequenceCurrentFilter.actual_current(step),2)

    def test_nonfinite_and_invalid_output_rejected(self):
        for command in ('OUT nan A, 60 HZ','OUT 1E999 V','OUT 1 V, -50 HZ','OUT 1001 A','OUT 1 V; OUT 2 A'):
            with self.subTest(command=command), self.assertRaises(ValueError): f.MODEL_PROFILES['5080A'].validate_command(command)


class ModelGuiTests(unittest.TestCase):
    setUp=gui.AsyncGuiTests.setUp
    tearDown=gui.AsyncGuiTests.tearDown
    pump_until=gui.AsyncGuiTests.pump_until

    def test_model_selection_changes_interfaces_rebuilds_and_rejects_connected_change(self):
        self.app.var_model.set('5522A')
        self.app._on_model_changed()
        self.assertEqual(tuple(self.app.combo_conn_mode['values']),('RS232','GPIB','AUTO','SIMULATION'))
        self.app._on_sim_connect_clicked()
        self.pump_until(lambda:not self.app._io_busy)
        self.assertIn('5522A',self.app.conn_mgr.info.idn_string)
        self.app.var_model.set('5080A')
        self.app._on_model_changed()
        self.assertEqual(self.app.var_model.get(),'5522A')
        self.assertEqual(self.app._selected_model(),'5522A')

    def test_model_change_invalidates_unsupported_old_rows(self):
        self.app.seq_steps=[f.SmartClipboardSequenceParser.build_Single_step_from_fields(1,'-','DC','-','500 OHM')]
        self.app.var_model.set('5080A')
        self.app._on_model_changed()
        self.assertEqual(self.app.seq_steps[0].func_type,'ERROR')
        self.assertEqual(self.app.seq_steps[0].scpi_cmd,'')
        self.app._start_filtered_sequence(0,0)
        self.assertTrue(self.reports)
        self.assertFalse(self.app._io_busy)
        self.app.var_model.set('5560A')
        self.app._on_model_changed()
        self.assertIn('OUT 500 OHM',self.app.seq_steps[0].scpi_cmd)


if __name__=='__main__': unittest.main(verbosity=2)
