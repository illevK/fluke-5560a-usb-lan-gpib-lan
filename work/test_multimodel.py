"""All peers are in-memory or localhost. Never connect to physical equipment."""
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f
from test_communications import FakeInstrument
import test_gui_async as gui


class SerialPeer:
    def __init__(self, model='5522A', echo=True, missing=False, error='0,"No error"', *,
                 recovery_reply=True, late_completion=False):
        self.model, self.echo, self.missing = model, echo, missing
        self.errors = [] if error == '0,"No error"' else [error]
        self.recovery_reply, self.late_completion = recovery_reply, late_completion
        self.trip = None
        self.current_post = 'AUX'
        self.ignore_post_change = False
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
        for part in command.split(';'):
            part = part.strip()
            if part.startswith('CUR_POST ') and not self.ignore_post_change:
                self.current_post = part.split()[1]
        if command.startswith('OUT ') and self.trip:
            self.errors.append(self.trip)
            self.operating = False
            self.trip = None
        if command in ('STBY', '*RST'):
            self.operating = False
            if self.late_completion:
                self.pending.extend(b'1\r\n')
        response = {'*IDN?': f'FLUKE,{self.model},TEST,1.0', '*OPC?': '1',
                    'COMM_MODE? SERIAL': 'COMP', 'OPER?': '1' if self.operating else '0',
                    'CUR_POST?': self.current_post, 'OUT?': '1 A, 60 HZ',
                    'FUNC?': 'ACI'}.get(command)
        if command == 'ERR?':
            response = self.errors.pop(0) if self.errors else '0,"No error"'
        if command == '*IDN?' and self.output_seen and not self.recovery_reply:
            response = None
        if self.missing and self.output_seen and command == '*OPC?' and self.operating:
            response = None
        if response is not None:
            self.pending.extend((response+'\r\n').encode())
        if self.echo:
            self.pending.extend(b'=> ')
        return len(data)

    def close(self):
        self.is_open = False


class SerialTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(f.Fluke5560AConnectionManager, "_connection_history_path",
                                       return_value=folder / "connections.json"))

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

    def test_5560_serial_rechecks_multiple_startup_errors_before_output(self):
        peer = SerialPeer("5560A", echo=False)
        mgr, _ = self.connect(peer)
        peer.errors = ['-410,"interrupted"', '-410,"interrupted"', '-361,"serial framing"']
        mgr.send_command("OUT 1 V, 0 HZ")
        index = peer.commands.index("OUT 1 V, 0 HZ")
        self.assertEqual(peer.commands[index - 3:index], ["*IDN?", "OPER?", "ERR?"])
        peer.errors = ['-361,"serial framing"']
        with self.assertRaisesRegex(f.InstrumentCommandError, "케이블"):
            mgr.send_command("OUT 2 V, 0 HZ", check_before=True)
        self.assertNotIn("OUT 2 V, 0 HZ", peer.commands)
        self.assertTrue(mgr.info.connected)

    def test_5560_serial_does_not_clear_a_following_protection_fault(self):
        peer = SerialPeer("5560A", echo=False)
        mgr, _ = self.connect(peer)
        peer.errors = ['-361,"serial framing"', '4102,"compliance limit"']
        with self.assertRaisesRegex(f.InstrumentCommandError, "4102"):
            mgr.send_command("OUT 1 V, 0 HZ")
        self.assertFalse(peer.output_seen)

    def test_5560_serial_stops_if_transport_error_never_clears(self):
        peer = SerialPeer("5560A", echo=False)
        mgr, _ = self.connect(peer)
        peer.errors = ['-361,"serial framing"'] * 17
        with self.assertRaisesRegex(f.InstrumentCommandError, "-361"):
            mgr.send_command("OUT 1 V, 0 HZ")
        self.assertFalse(peer.output_seen)
        self.assertIn("STBY", peer.commands)

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

    def test_5560a_numbered_serial_prompt_and_computer_mode_transition(self):
        peer = SerialPeer("5560A", echo=False)
        mode = "TERM"
        prompt_no = 4
        peer.pending.extend(b"3> ")

        def write(data):
            nonlocal mode, prompt_no
            self.assertTrue(data.endswith(b"\n") and b"\r" not in data)
            command = data.decode("ascii").strip()
            peer.commands.append(command)
            was_terminal = mode == "TERM"
            if was_terminal:
                peer.pending.extend(data)
            if command == "COMM_MODE SERIAL, COMP":
                mode = "COMP"
            replies = {"*IDN?": "FLUKE,5560A,TEST,1.0", "COMM_MODE? SERIAL": mode,
                       "*OPC?": "1", "OPER?": "0", "ERR?": '0,"No Error"', "FUNC?": "DCV"}
            if command in replies:
                peer.pending.extend((replies[command] + "\r\n").encode("ascii"))
            if was_terminal:
                peer.pending.extend(f"Fault -410: 488.2 interrupted query\r\n{prompt_no}> ".encode("ascii"))
                prompt_no += 1
            return len(data)

        peer.write = write
        mgr, _ = self.connect(peer)
        self.assertTrue(mgr.info.connected)
        self.assertEqual(peer.commands[:6], ["*IDN?", "COMM_MODE? SERIAL", "COMM_MODE SERIAL, COMP",
                                             "COMM_MODE? SERIAL", "REMOTE", "*OPC?"])
        self.assertEqual(mgr.query("OPER?"), "0")
        mgr.disconnect()
        self.assertIn("LOCAL", peer.commands)

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
        peer = SerialPeer(missing=True, recovery_reply=False)
        mgr, _ = self.connect(peer)
        with self.assertRaises(TimeoutError):
            mgr.send_command('OUT 1 A, 60 HZ; OPER')
        self.assertEqual(peer.commands.count('OUT 1 A, 60 HZ; OPER'), 1)
        self.assertEqual(peer.commands[-2:], ['STBY', '*IDN?'])
        self.assertFalse(peer.is_open)
        self.assertFalse(mgr.info.connected)

    def test_serial_wait_can_be_cancelled(self):
        peer = SerialPeer(missing=True, late_completion=True)
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
        self.assertIsInstance(failures[0], f.InstrumentCommandError)
        self.assertEqual(peer.commands[-4:], ['STBY', '*IDN?', 'OPER?', 'ERR?'])
        self.assertTrue(mgr.info.connected)
        self.assertTrue(peer.is_open)

    def test_serial_device_error_preserves_confirmed_standby_connection(self):
        peer = SerialPeer(error='4103,"Settling error"')
        mgr, _ = self.connect(peer)
        with self.assertRaises(f.InstrumentCommandError): mgr.send_command('OUT 1 A, 60 HZ; OPER')
        self.assertTrue(mgr.info.connected)
        self.assertEqual(mgr.query('OPER?'), '0')

    def test_legacy_initial_query_error_and_protection_reuse_same_com_port(self):
        for model, fault_code in (('5522A', 1500), ('5520A', 1503), ('5080A', 1502)):
            with self.subTest(model=model):
                peer = SerialPeer(model, error='+1308,"Interrupted query"')
                peer.trip = f'{fault_code},"Protection trip"'
                mgr, _ = self.connect(peer)
                output = 'OUT 1 A, 60 HZ; CUR_POST AUX; OPER'
                with self.assertRaisesRegex(f.InstrumentCommandError, str(fault_code)):
                    mgr.send_command(output, check_before=True)
                start = peer.commands.index(output)
                self.assertEqual(peer.commands[start-3:start], ['ERR?', 'OPER?', 'ERR?'])
                self.assertEqual(peer.commands[start+1:], ['*OPC?', 'ERR?', 'OPER?'])
                self.assertIs(mgr._serial_port, peer)
                self.assertTrue(mgr.info.connected)
                self.assertNotIn('STBY', peer.commands)
                mgr.send_command(output, check_before=True)
                self.assertEqual(mgr.query('OPER?'), '1')
                mgr.disconnect()
                self.assertLess(peer.commands.index('STBY'), peer.commands.index('LOCAL'))
                self.assertFalse(peer.is_open)

    def test_legacy_serial_terminal_fault_keeps_confirmed_session(self):
        peer = SerialPeer('5522A', late_completion=True)
        write = peer.write

        def terminal_write(data):
            if data.startswith(b'OUT '):
                peer.pending.extend(b'!> Compliance voltage exceeded\r\n')
            return write(data)

        peer.write = terminal_write
        mgr, _ = self.connect(peer)
        output = 'OUT 1 A, 60 HZ; CUR_POST AUX; OPER'
        with self.assertRaisesRegex(f.InstrumentCommandError, 'Compliance voltage exceeded'):
            mgr.send_command(output)
        self.assertEqual(peer.commands[-4:], ['STBY', '*IDN?', 'OPER?', 'ERR?'])
        self.assertEqual(peer.commands.count(output), 1)
        self.assertTrue(mgr.info.connected)
        self.assertEqual(mgr.query('OPER?'), '0')

    def test_legacy_query_interruption_does_not_hide_following_protection_fault(self):
        peer = SerialPeer('5520A', error='1308,"Interrupted query"')
        peer.errors.append('1500,"Compliance voltage exceeded"')
        mgr, _ = self.connect(peer)
        with self.assertRaisesRegex(f.InstrumentCommandError, '1500'):
            mgr.send_command('OUT 1 A, 60 HZ; CUR_POST AUX; OPER', check_before=True)
        self.assertFalse(any(c.startswith('OUT ') for c in peer.commands))
        self.assertTrue(mgr.info.connected)
        self.assertNotIn('*CLS', peer.commands)
        mgr.disconnect()
        self.assertIn('LOCAL', peer.commands)

    def test_missing_library_and_invalid_baud_explain_connection_failure(self):
        with patch.object(f, 'HAS_PYSERIAL', False):
            mgr = f.Fluke5560AConnectionManager()
            with self.assertRaisesRegex(ConnectionError, 'pyserial'):
                mgr.scan_and_auto_connect('RS232', serial_port='COM1')
        peer = SerialPeer()
        with self.assertRaises(ConnectionError): self.connect(peer, baudrate=115200)


class AutoConnectionTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Path(folder) / "connections.json"
        self.enterContext(patch.object(f.Fluke5560AConnectionManager, "_connection_history_path",
                                       return_value=self.settings))
        self.enterContext(patch.object(f.AutoNetworkAligner, "list_ethernet_interfaces", return_value=[]))

    def test_auto_skips_bluetooth_probes_multiple_serial_ports_and_remembers_success(self):
        def port(name, description, hwid, vid=None):
            return SimpleNamespace(device=name, description=description, hwid=hwid, vid=vid)

        ports = [port("COM9", "Bluetooth", "BTHENUM\\1"),
                 port("COM10", "Bluetooth", "BTHENUM\\2"),
                 port("COM11", "USB converter", "USB VID:PID=067B:23A3", 0x067B),
                 port("COM7", "USB converter", "USB VID:PID=0403:6001", 0x0403),
                 port("COM3", "Serial port", "ACPI\\1")]
        wrong = SerialPeer("5080A")
        right = SerialPeer("5560A")
        wrong_port = Mock(side_effect=lambda **kwargs: {"COM7": right, "COM11": wrong}[kwargs["port"]])
        backend = Mock(Serial=wrong_port)
        backend.tools.list_ports.comports.return_value = ports

        with patch.object(f, "serial", backend), patch.object(f, "HAS_PYSERIAL", True):
            mgr = f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect("AUTO", timeout_sec=.2)
            self.assertTrue(mgr.info.resource_name.startswith("COM7 /"))
            self.assertEqual(wrong.commands, ["*IDN?"])
            self.assertFalse(wrong.is_open)
            self.assertEqual([call.kwargs["port"] for call in backend.Serial.call_args_list], ["COM11", "COM7"])
            mgr.disconnect(standby=False)

            backend.Serial.reset_mock()
            backend.Serial.side_effect = lambda **_: SerialPeer("5560A")
            mgr = f.Fluke5560AConnectionManager()
            with patch.object(mgr, "_try_lan_socket") as lan:
                mgr.scan_and_auto_connect("AUTO", "169.254.1.1", timeout_sec=.2)
                lan.assert_not_called()
            backend.Serial.assert_called_once()
            self.assertEqual(backend.Serial.call_args.kwargs["port"], "COM7")
            mgr.disconnect(standby=False)

    def test_auto_uses_saved_lan_address_when_entered_endpoint_is_unavailable(self):
        with FakeInstrument() as server:
            mgr = f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect("LAN", "127.0.0.1", server.port, timeout_sec=.3)
            mgr.disconnect(standby=False)

            mgr = f.Fluke5560AConnectionManager()
            try_lan = mgr._try_lan_socket

            def probe(ip, port, *args, **kwargs):
                if port == 3490:
                    mgr.last_error = "Entered address is unavailable"
                    return f.ConnectionInfo()
                return try_lan(ip, port, *args, **kwargs)

            with patch.object(mgr, "_try_lan_socket", side_effect=probe) as attempt:
                info = mgr.scan_and_auto_connect("AUTO", "169.254.1.1", 3490, timeout_sec=.3)
            self.assertEqual(info.discovered_port, server.port)
            self.assertEqual([call.args[:2] for call in attempt.call_args_list],
                             [("169.254.1.1", 3490), ("127.0.0.1", server.port)])
            mgr.disconnect(standby=False)

    def test_auto_identifies_legacy_calibrator_on_com_without_preselecting_model(self):
        peer = SerialPeer("5522A")
        backend = Mock(Serial=Mock(return_value=peer))
        backend.tools.list_ports.comports.return_value = [
            SimpleNamespace(device="COM5", description="USB converter", hwid="USB", vid=0x0403)
        ]
        mgr = f.Fluke5560AConnectionManager()
        with (patch.object(f, "serial", backend), patch.object(f, "HAS_PYSERIAL", True),
              patch.object(f, "HAS_PYVISA", False)):
            info = mgr.scan_and_auto_connect("AUTO", model="AUTO", timeout_sec=.2)
        self.assertEqual(mgr.profile.model, "5522A")
        self.assertIn("COM5", info.resource_name)
        self.assertEqual(peer.commands, ["*IDN?", "REMOTE", "*OPC?"])
        mgr.disconnect(standby=False)

class ModelTransportTests(unittest.TestCase):
    def test_5080_lan_does_not_send_5560_comm_mode(self):
        with FakeInstrument(identity='FLUKE,5080A,TEST,1.0', fragmented=True) as server:
            mgr = f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect('LAN', '127.0.0.1', server.port, model='5080A', timeout_sec=1)
            self.assertEqual(server.commands, ['*IDN?', 'REMOTE', '*OPC?'])
            mgr.send_command('OUT 1 A, 60 HZ; CUR_POST AUX; LCOMP ON; OPER')
            mgr.disconnect()
            self.assertFalse(any(c.startswith('COMM_MODE') for c in server.commands))

    def test_5080_lan_timeout_recovers_with_fresh_identity_and_reconnects(self):
        peer = SerialPeer('5080A', echo=False, missing=True, late_completion=True,
                          error='1308,"Interrupted query"')

        def handle(conn, command):
            peer.write((command + '\r\n').encode())
            if peer.pending:
                conn.sendall(peer.pending)
                peer.pending.clear()

        mgr = f.Fluke5560AConnectionManager()
        self.addCleanup(lambda: mgr.disconnect(standby=False))
        with FakeInstrument(handler=handle) as server:
            mgr.scan_and_auto_connect('LAN', '127.0.0.1', server.port, model='5080A',
                                      timeout_sec=.2, operation_timeout_sec=1)
            first = mgr._lan_sock
            output = 'OUT 1 A, 60 HZ; CUR_POST AUX; OPER'
            with self.assertRaisesRegex(f.InstrumentCommandError, '5080A.*STBY'):
                mgr.send_command(output)
            self.assertEqual(peer.commands[-4:], ['STBY', '*IDN?', 'OPER?', 'ERR?'])
            self.assertIs(mgr._lan_sock, first)
            self.assertEqual(peer.commands.count(output), 1)
            peer.late_completion = False
            mgr.disconnect()
            self.assertIn('LOCAL', peer.commands)
            mgr.scan_and_auto_connect('LAN', '127.0.0.1', server.port, model='5080A', timeout_sec=.2)
            self.assertTrue(mgr.info.connected)
            mgr.disconnect(standby=False)

    def test_legacy_gpib_handles_pending_query_and_compliance(self):
        for model in ('5522A', '5520A'):
            with self.subTest(model=model):
                visa = Mock()
                inst = visa.ResourceManager.return_value.open_resource.return_value
                errors = iter(['1308,"Interrupted query"', '0,"No error"',
                               '1500,"Compliance voltage exceeded"', '0,"No error"'])

                def reply(command):
                    if command == 'ERR?':
                        return next(errors)
                    return {'*IDN?': f'FLUKE,{model},TEST,1.0', '*OPC?': '1', 'OPER?': '0'}[command]

                inst.query.side_effect = reply
                with patch.object(f, 'pyvisa', visa), patch.object(f, 'HAS_PYVISA', True):
                    mgr = f.Fluke5560AConnectionManager()
                    mgr.scan_and_auto_connect('GPIB', model=model, visa_resource='GPIB0::4::INSTR')
                    output = 'OUT 1 A, 60 HZ; CUR_POST AUX; OPER'
                    with self.assertRaisesRegex(f.InstrumentCommandError, '1500'):
                        mgr.send_command(output)
                    inst.write.assert_called_once_with(output)
                    self.assertTrue(mgr.info.connected)
                    mgr.disconnect()
                    inst.control_ren.assert_called_with(visa.constants.RENLineOperation.address_gtl)

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

    def test_auto_connect_updates_model_and_preserves_measurements(self):
        app = self.app
        step, = f.SmartClipboardSequenceParser.build_sequence_steps("500 OHM")
        step.measured_raw = "500.1 OHM"
        app.seq_steps = [step]
        app.excel_grid.render_steps(app.seq_steps)
        info = f.ConnectionInfo(True, "RS232", "COM5 / 9600 / 8N1", "FLUKE,5080A,TEST,1.0")

        def connect(**arguments):
            self.assertEqual(arguments["model"], "AUTO")
            app.conn_mgr.profile = f.MODEL_PROFILES["5080A"]
            app.conn_mgr.info = info
            return info

        try:
            with (patch.object(app.conn_mgr, "scan_and_auto_connect", side_effect=connect),
                  patch.object(app.conn_mgr, "query", return_value="0")):
                app.btn_connect.invoke()
                self.pump_until(lambda: not app._io_busy)
            self.assertEqual(app.var_model.get(), "5080A")
            self.assertEqual(app.seq_steps[0].func_type, "ERROR")
            self.assertEqual(app.seq_steps[0].measured_raw, "500.1 OHM")
            self.assertIn("COM5", app.var_idn_display.get())
        finally:
            app.conn_mgr.disconnect(standby=False)

    def test_auto_failure_offers_lan_ip_settings(self):
        app = self.app
        with patch.object(app.conn_mgr, "scan_and_auto_connect", side_effect=ConnectionError("no devices")):
            app.btn_connect.invoke()
            self.pump_until(lambda: not app._io_busy)
        self.assertTrue(app._connection_details_open)
        self.assertIn("LAN IP", app.var_idn_display.get())

    def test_legacy_sequence_checks_current_and_power_posts_before_oper(self):
        for model in ('5522A', '5520A', '5080A'):
            with self.subTest(model=model):
                peer = SerialPeer(model)
                self.app.var_model.set(model)
                self.app._on_model_changed()
                with patch.object(f, 'HAS_PYSERIAL', True), patch.object(f, 'serial', Mock(Serial=Mock(return_value=peer))):
                    self.app.conn_mgr.scan_and_auto_connect('RS232', model=model, serial_port='COM7', timeout_sec=.2)
                    for text, post in (('2 A', 'AUX'), ('120 V / 3 A', 'A20')):
                        step, = f.SmartClipboardSequenceParser.build_sequence_steps(text, model=model)
                        self.assertIn('CUR_POST ' + post, step.scpi_cmd)
                        self.app.seq_steps = [step]
                        self.app.excel_grid.render_steps(self.app.seq_steps)
                        with patch.object(self.app, '_step_in_current_group', return_value=True):
                            self.assertTrue(self.app._execute_sequence_step(0))
                            self.pump_until(lambda: not self.app._io_busy)
                        index = peer.commands.index(step.scpi_cmd)
                        oper = peer.commands.index('OPER', index)
                        self.assertIn('CUR_POST?', peer.commands[index+1:oper])
                        self.assertEqual(peer.current_post, post)
                    peer.ignore_post_change = True
                    step, = f.SmartClipboardSequenceParser.build_sequence_steps('1 A', model=model)
                    self.app.seq_steps = [step]
                    self.app.excel_grid.render_steps(self.app.seq_steps)
                    with patch.object(self.app, '_step_in_current_group', return_value=True):
                        self.assertTrue(self.app._execute_sequence_step(0))
                        self.pump_until(lambda: not self.app._io_busy)
                    self.assertEqual(peer.commands.count('OPER'), 2)
                    self.assertLess(peer.commands.index('STBY', oper), peer.commands.index(step.scpi_cmd))
                    self.assertIn('전류 단자 불일치', self.app.var_active_row_banner.get())
                    self.assertFalse(peer.operating)
                    self.assertTrue(self.app.conn_mgr.info.connected)
                    self.app.conn_mgr.disconnect()

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
