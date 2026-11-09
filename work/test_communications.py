import importlib.util
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f

IDN = 'FLUKE,5560A,TEST-1234,4.0'


class FakeInstrument:
    """A real local TCP peer so tests exercise stream framing and resource cleanup."""
    def __init__(self, handler=None, identity=IDN, fragmented=False, mode='COMP'):
        self.identity = identity
        self.handler = handler
        self.fragmented = fragmented
        self.mode = mode
        self.commands = []
        self.errors = []
        self.connections = []
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        for conn in self.connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.socket.close()
        self.thread.join(2)

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _addr = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections.append(conn)
            conn.settimeout(.1)
            pending = b''
            try:
                while not self.stop.is_set():
                    try:
                        data = conn.recv(1024)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    pending += data
                    while b'\n' in pending or b'\r' in pending:
                        positions = [pos for pos in (pending.find(b'\r'), pending.find(b'\n')) if pos >= 0]
                        end = min(positions)
                        raw, pending = pending[:end], pending[end + 1:]
                        if not raw:
                            continue
                        command = raw.decode('ascii')
                        self.commands.append(command)
                        if self.handler:
                            self.handler(conn, command)
                            continue
                        if command == 'COMM_MODE TELNET, COMP':
                            self.mode = 'COMP'
                        response = {'*IDN?': self.identity, 'COMM_MODE? TELNET': self.mode,
                                    '*OPC?': '1', 'ERR?': '0,"No error"', 'OPER?': '0',
                                    'OUT?': '1.000 V,0 HZ'}.get(command)
                        if response is not None:
                            payload = (response + '\r\n').encode('ascii')
                            if self.fragmented:
                                for byte in payload:
                                    conn.sendall(bytes((byte,)))
                                    time.sleep(.001)
                            else:
                                conn.sendall(payload)
            except (OSError, UnicodeError):
                pass
            finally:
                conn.close()


class TransportTests(unittest.TestCase):
    def test_connect_fragmented_identity_then_repeated_queries(self):
        with FakeInstrument(fragmented=True) as server:
            mgr = f.Fluke5560AConnectionManager()
            info = mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port, timeout_sec=1)
            self.assertTrue(info.connected)
            self.assertEqual(info.idn_string, IDN)
            for _ in range(3):
                self.assertEqual(mgr.query('OPER?'), '0')
                self.assertEqual(mgr.query('OUT?'), '1.000 V,0 HZ')
            mgr.disconnect(standby=False)
            self.assertNotIn('OPER',server.commands)
            self.assertNotIn('COMM_MODE TELNET, COMP',server.commands)

    def test_terminal_mode_changes_only_after_verified_identity(self):
        with FakeInstrument(mode='TERM') as server:
            mgr=f.Fluke5560AConnectionManager()
            self.assertTrue(mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port,timeout_sec=1).connected)
            self.assertEqual(server.commands[:4],['*IDN?','COMM_MODE? TELNET','COMM_MODE TELNET, COMP','COMM_MODE? TELNET'])
            mgr.disconnect(standby=False)

    def test_wrong_fluke_model_cannot_connect_or_receive_control(self):
        for identity in ('FLUKE,8846A,TEST,1.0','ACME,5560A,TEST,1.0'):
            with self.subTest(identity=identity), FakeInstrument(identity=identity) as server:
                mgr = f.Fluke5560AConnectionManager()
                result = mgr._try_lan_socket('127.0.0.1',server.port,allow_open_port_fallback=True)
                self.assertFalse(result.connected)
                self.assertEqual(server.commands,['*IDN?'])

    def test_open_port_without_response_is_not_connected(self):
        with FakeInstrument(handler=lambda *_: None) as server:
            mgr=f.Fluke5560AConnectionManager()
            self.assertFalse(mgr._try_lan_socket('127.0.0.1',server.port,timeout_sec=.2,
                                                 allow_open_port_fallback=True).connected)
            self.assertIn('TimeoutError',mgr.last_error)
            self.assertIsNone(mgr._lan_sock)

    def test_readonly_diagnosis_sends_only_identity(self):
        with FakeInstrument() as server:
            mgr=f.Fluke5560AConnectionManager()
            mgr.diagnose_lan('127.0.0.1',server.port,timeout=1)
            self.assertIn('[진단 성공]',mgr.last_error)
            self.assertEqual(server.commands,['*IDN?'])
            self.assertFalse(mgr.info.connected)

    def test_incremental_telnet_negotiation_and_subnegotiation(self):
        sock=Mock()
        transport=f.TelnetLineTransport(sock)
        raw=b'\xff\xfb\x01\xff\xfd\x03\xff\xfa\x18ignored\xff\xfftext\xff\xf0FLUKE,5560A,TEST,4\r\n'
        for byte in raw:
            transport._feed(bytes((byte,)))
        self.assertEqual(bytes(transport.buffer),b'FLUKE,5560A,TEST,4\r\n')
        self.assertEqual([c.args[0] for c in sock.sendall.call_args_list], [b'\xff\xfe\x01',b'\xff\xfc\x03'])

    def test_echo_prompt_and_delayed_line_termination(self):
        def handler(conn, command):
            conn.sendall(b'=> *IDN?\r\nFLUKE,5560A,TEST,')
            time.sleep(.08)
            conn.sendall(b'4.0\r\n=>')
        with FakeInstrument(handler=handler) as server:
            with socket.create_connection(('127.0.0.1',server.port)) as sock:
                self.assertEqual(f.TelnetLineTransport(sock,1).query('*IDN?',identity=True), IDN.replace('TEST-1234','TEST'))

    def test_cr_lf_and_crlf_replies(self):
        for terminator in (b'\r',b'\n',b'\r\n'):
            def handler(conn, command):
                conn.sendall(b'1'+terminator)
            with self.subTest(terminator=terminator), FakeInstrument(handler=handler) as server:
                with socket.create_connection(('127.0.0.1',server.port)) as sock:
                    tr=f.TelnetLineTransport(sock,1)
                    self.assertEqual(tr.query('*OPC?'),'1')
                    self.assertEqual(tr.query('*OPC?'),'1')

    def test_eof_and_partial_response_never_report_success(self):
        def handler(conn,command):
            conn.sendall(IDN.encode())
            conn.shutdown(socket.SHUT_WR)
        with FakeInstrument(handler=handler) as server:
            mgr=f.Fluke5560AConnectionManager()
            self.assertFalse(mgr._try_lan_socket('127.0.0.1',server.port,timeout_sec=.3).connected)

    def test_timeout_drops_session_to_prevent_stale_response(self):
        with FakeInstrument() as server:
            mgr=f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port,timeout_sec=.2)
            with self.assertRaises(TimeoutError):
                mgr.query('MISSING?')
            self.assertFalse(mgr.info.connected)
            self.assertIsNone(mgr._lan_sock)

    def test_command_ack_and_error_queue(self):
        with FakeInstrument() as server:
            mgr=f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port,timeout_sec=1)
            mgr.send_command('STBY')
            self.assertEqual(server.commands[-3:],['STBY','*OPC?','ERR?'])
            mgr.disconnect(standby=False)

    def test_parallel_queries_remain_serialized(self):
        with FakeInstrument(fragmented=True) as server:
            mgr=f.Fluke5560AConnectionManager()
            mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port,timeout_sec=1)
            results={}
            threads=[threading.Thread(target=lambda c=c: results.__setitem__(c,mgr.query(c))) for c in ('OPER?','OUT?')]
            for t in threads: t.start()
            for t in threads: t.join(2)
            self.assertEqual(results,{'OPER?':'0','OUT?':'1.000 V,0 HZ'})
            mgr.disconnect(standby=False)

    def test_graceful_disconnect_and_reconnect(self):
        with FakeInstrument() as server:
            mgr=f.Fluke5560AConnectionManager()
            for _ in range(3):
                mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port,timeout_sec=1)
                mgr.disconnect()
                self.assertFalse(mgr.info.connected)
                self.assertEqual(server.commands[-5:],['STBY','*OPC?','ERR?','LOCAL','*OPC?'])


class AdapterAndVisaTests(unittest.TestCase):
    def test_padded_ip_and_invalid_addresses(self):
        self.assertEqual(f.AutoNetworkAligner.normalize_ipv4('169.254.001.001'),'169.254.1.1')
        for invalid in ('0.0.0.0','999.1.1.1','1.2.3','234.5.6.1','1.2.3.4; echo bad'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                f.AutoNetworkAligner.normalize_ipv4(invalid)

    def test_source_binding_uses_real_mask_and_rejects_down_adapter(self):
        adapters=[dict(idx='17',name='Ethernet',state='Up',addresses=[dict(ip='169.254.99.5',prefix=16)])]
        with patch.object(f.AutoNetworkAligner,'list_ethernet_interfaces',return_value=adapters):
            self.assertEqual(f.AutoNetworkAligner.select_source_ip('17','169.254.1.1'),'169.254.99.5')
            adapters[0]['state']='Disconnected'
            with self.assertRaises(ConnectionError):
                f.AutoNetworkAligner.select_source_ip('17','169.254.1.1')

    def test_usb_failure_closes_each_handle_and_reports_error(self):
        rm=Mock()
        rm.list_resources.return_value=('USB0::A::INSTR',)
        inst=rm.open_resource.return_value
        inst.query.side_effect=RuntimeError('read timeout')
        visa=Mock()
        visa.ResourceManager.return_value=rm
        with patch.object(f,'pyvisa',visa),patch.object(f,'HAS_PYVISA',True):
            mgr=f.Fluke5560AConnectionManager()
            self.assertFalse(mgr._try_visa_auto_scan('USB',1,1).connected)
            inst.close.assert_called_once()
            rm.close.assert_called_once()
            self.assertIn('read timeout',mgr.last_error)

    def test_gpib_honors_address_and_never_sends_serial_remote(self):
        rm=Mock()
        rm.list_resources.return_value=('GPIB0::5::INSTR','GPIB0::8::INSTR')
        inst=rm.open_resource.return_value
        inst.query.return_value=IDN
        visa=Mock()
        visa.ResourceManager.return_value=rm
        with patch.object(f,'pyvisa',visa),patch.object(f,'HAS_PYVISA',True):
            mgr=f.Fluke5560AConnectionManager()
            self.assertTrue(mgr._try_visa_auto_scan('GPIB',8,1).connected)
            rm.open_resource.assert_called_once_with('GPIB0::8::INSTR',open_timeout=1000)
            inst.write.assert_not_called()
            mgr.disconnect(standby=False)
            inst.close.assert_called_once()


class SlowOutputTests(unittest.TestCase):
    def peer(self, delay=0, missing=False, error='0,"No error"'):
        state = {'output': False, 'error': error}
        def handler(conn, command):
            if command.startswith('OUT '):
                state['output'] = True
            if command == 'STBY':
                state['output'] = False
            if command == '*OPC?' and state['output']:
                if missing:
                    return
                time.sleep(delay)
            response = {'*IDN?': IDN, 'COMM_MODE? TELNET': 'COMP', '*OPC?': '1',
                        'OPER?': '1' if state['output'] else '0', 'OUT?': '100 V,0 HZ'}.get(command)
            if command == 'ERR?':
                response = state['error']
                state['error'] = '0,"No error"'
            if response is not None:
                conn.sendall((response + '\r\n').encode())
        return FakeInstrument(handler=handler)

    def connect(self, server, timeout=.2, operation_timeout=1):
        mgr=f.Fluke5560AConnectionManager()
        mgr.scan_and_auto_connect('LAN','127.0.0.1',server.port,timeout_sec=timeout,
                                  operation_timeout_sec=operation_timeout)
        return mgr

    def test_reply_after_original_three_second_limit_is_accepted_once(self):
        with self.peer(delay=3.4) as server:
            mgr=self.connect(server,timeout=3,operation_timeout=5)
            mgr.send_command('OUT 100 V, 0 HZ; OPER')
            self.assertTrue(mgr.info.connected)
            self.assertEqual(mgr.query('OUT?'),'100 V,0 HZ')
            self.assertEqual(server.commands.count('OUT 100 V, 0 HZ; OPER'),1)
            self.assertEqual(server.commands.count('*OPC?'),2)  # handshake + output
            self.assertTrue(any('안정화 대기' in x for x in list(mgr.events.queue)))
            self.assertEqual(mgr._transport.timeout,3)
            mgr.disconnect(standby=False)

    def test_final_timeout_attempts_standby_before_closing_without_replay(self):
        with self.peer(missing=True) as server:
            mgr=self.connect(server)
            with self.assertRaises(TimeoutError):
                mgr.send_command('OUT 1 A, 50 HZ; OPER')
            self.assertFalse(mgr.info.connected)
            self.assertEqual(server.commands[-1],'STBY')
            self.assertEqual(server.commands.count('OUT 1 A, 50 HZ; OPER'),1)
            self.assertIn('출력 차단 확인은 되지 않았',mgr.last_error)
            self.assertIn('OUT 1 A',mgr.last_error)

    def test_abort_interrupts_lan_wait_and_sends_standby(self):
        with self.peer(missing=True) as server:
            mgr=self.connect(server,operation_timeout=60)
            failures=[]
            def output():
                try: mgr.send_command('OUT 1 V; OPER')
                except Exception as exc: failures.append(exc)
            thread=threading.Thread(target=output)
            thread.start()
            deadline=time.monotonic()+1
            while server.commands.count('*OPC?') < 2 and time.monotonic()<deadline:
                time.sleep(.01)
            started=time.monotonic()
            mgr.request_abort()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic()-started,1)
            self.assertIsInstance(failures[0],f.OperationCancelled)
            self.assertEqual(server.commands[-1],'STBY')
            self.assertFalse(mgr.info.connected)

    def test_reported_device_error_retains_confirmed_standby_connection(self):
        with self.peer(error='4103,"Output settling timed out"') as server:
            mgr=self.connect(server)
            with self.assertRaises(f.InstrumentCommandError):
                mgr.send_command('OUT 1 V; OPER')
            self.assertTrue(mgr.info.connected)
            self.assertEqual(mgr.query('OPER?'),'0')
            self.assertIn('4103',mgr.last_error)
            mgr.disconnect(standby=False)

    def test_opc_timeout_does_not_extend_ordinary_query_timeout(self):
        with self.peer(delay=.3) as server:
            mgr=self.connect(server)
            mgr.send_command('OUT 1 V; OPER')
            started=time.monotonic()
            with self.assertRaises(TimeoutError): mgr.query('MISSING?')
            self.assertLess(time.monotonic()-started,.8)

    def test_visa_uses_operation_timeout_and_restores_read_timeout(self):
        mgr=f.Fluke5560AConnectionManager()
        inst=Mock(timeout=3000)
        observed=[]
        def query(command):
            observed.append((command,inst.timeout))
            return '1' if command=='*OPC?' else '0,"No error"'
        inst.query.side_effect=query
        mgr._visa_inst=inst
        mgr.info=f.ConnectionInfo(True,'USB (USBTMC)','USB0::TEST::INSTR',IDN)
        mgr.send_command('OUT 1 V; OPER')
        self.assertEqual(observed,[('*OPC?',60000),('ERR?',3000)])
        self.assertEqual(inst.timeout,3000)
        mgr.disconnect(standby=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
