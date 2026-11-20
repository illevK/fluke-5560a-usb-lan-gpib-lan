
"""DUT discovery tests without a live instrument or network requests."""
import base64
import re
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


WT_USB = 'USB0::0x0B21::0x0025::WT310E_TEST::INSTR'
WT_LAN = '192.168.1.24'
WT_ID = 'YOKOGAWA,WT310E,WT310E_TEST,F1.01'
WT_LAN_ID = 'YOKOGAWA,WT310E,WT310E_LAN,F1.01'


def candidate(settings, identity):
    return f.DUTDiscoveryCandidate(settings, f.identify_dut(identity), identity)


class DiscoveryTests(unittest.TestCase):
    def test_34465a_socket_auto_uses_entered_ip_and_wired_source(self):
        host, source = "192.168.10.7", "192.168.10.100"
        adapters = [{"wired": True, "state": "Up", "addresses": [{"ip": source, "prefix": 24}]}]
        socket_peer = Mock()
        socket_peer.recv.return_value = b"Keysight Technologies,34465A,MY123,A.03\n"
        def open_socket(address, **options):
            if address != (host, 5025):
                raise ConnectionRefusedError()
            self.assertEqual(options["source_address"], (source, 0))
            return socket_peer
        ports = [SimpleNamespace(device="COM11", vid=0x067B)]
        with (patch.object(f, "HAS_PYVISA", False),
              patch.object(f.socket, "create_connection", side_effect=open_socket),
              patch.object(f.serial.tools.list_ports, "comports", return_value=ports),
              patch.object(f.serial, "Serial") as open_com):
            found, notes = f.discover_duts(threading.Event(), include_usb=False, include_serial=True,
                                         lan_ip=host, adapters=adapters)
        self.assertIn("COM 자동 탐색을 생략했습니다", " ".join(notes))
        open_com.assert_not_called()
        self.assertEqual([(item.profile.model, item.settings.mode) for item in found], [("34465A", "DMM_TCP")])
        socket_peer.sendall.assert_called_once_with(b"*IDN?\n")

    def test_direct_lan_picks_matching_ethernet_or_plans_device_subnet_alias(self):
        adapters = [{"idx": "17", "state": "Up", "wired": True,
                     "addresses": [{"ip": "192.168.10.100", "prefix": 24}]},
                    {"idx": "3", "state": "Up", "wired": False,
                     "addresses": [{"ip": "223.0.1.25", "prefix": 24}]}]
        self.assertEqual(f._dut_direct_lan_plan("192.168.10.3", "255.255.255.0", adapters),
                         ("192.168.10.100", None, ()))
        source, adapter, candidates = f._dut_direct_lan_plan("223.0.1.8", "255.255.255.0", adapters)
        self.assertEqual((source, adapter, candidates[0]), ("", 17, "223.0.1.100"))
        with self.assertRaisesRegex(ValueError, "PC 주소"):
            f._dut_direct_lan_plan("223.0.1.25", "255.255.255.0", adapters)

    def test_windows_direct_route_uses_only_the_dut_destination_and_active_store(self):
        with (patch.object(f.sys, "platform", "win32"),
              patch.object(f.subprocess, "run", side_effect=[
                  SimpleNamespace(returncode=0, stdout=b""),
                  SimpleNamespace(returncode=0, stdout=b""),
                  SimpleNamespace(returncode=0, stdout=b"223.0.1.100\r\n"),
              ]) as run):
            self.assertEqual(f._dut_configure_host_route("223.0.1.8", 17, ("223.0.1.100",)), "223.0.1.100")
        launcher = run.call_args_list[1].args[0][-1]
        encoded = re.search(r"'-EncodedCommand','([A-Za-z0-9+/=]+)'", launcher)[1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("$destination = '223.0.1.8/32'", script)
        self.assertIn("-PrefixLength 32 -PolicyStore ActiveStore", script)
        self.assertIn("New-NetRoute -DestinationPrefix $destination -InterfaceIndex $nicIndex", script)
        self.assertNotIn("DefaultGateway", script)

    def test_direct_discovery_binds_aligned_ip_or_skips_target_on_failure(self):
        host = "223.0.1.8"
        sock = Mock()
        sock.recv.return_value = b"HIOKI,PW3390-02,TEST,V1.01\r\n"
        with (patch.object(f, "HAS_PYVISA", False),
              patch.object(f, "align_dut_direct_lan", return_value="223.0.1.100") as align,
              patch.object(f.socket, "create_connection", return_value=sock) as connect):
            found, _ = f.discover_duts(threading.Event(), include_usb=False, lan_ip=host, adapters=[], direct_mask="24")
            self.assertEqual(found[0].profile.model, "PW3390")
            connect.assert_called_once_with((host, 3390), timeout=1.0, source_address=("223.0.1.100", 0))
            align.side_effect = ConnectionError("승인 취소")
            connect.reset_mock()
            found, notes = f.discover_duts(threading.Event(), include_usb=False, lan_ip=host, adapters=[], direct_mask="24")
            self.assertFalse(found)
            self.assertIn("승인 취소", notes[-1])
            connect.assert_not_called()

    def test_pw3390_serial_auto_uses_documented_baud_rates_without_flow_control(self):
        identity = "HIOKI,PW3390-02,TEST,V1.01"
        for speed in (38400, 19200, 9600):
            with self.subTest(speed=speed):
                reply = (identity + "\r\n").encode("ascii")

                def open_port(address, **options):
                    port = Mock()
                    port.in_waiting = len(reply)
                    port.read.return_value = reply
                    def write(data):
                        if options["baudrate"] == speed and not options["rtscts"] and not options["xonxoff"] and data == b"*IDN?\r\n":
                            return len(data)
                        raise TimeoutError("serial layout mismatch")
                    port.write.side_effect = write
                    return port

                ports = [SimpleNamespace(device="COM11", vid=0x067B)]
                with (patch.object(f, "HAS_PYVISA", False),
                      patch.object(f.serial.tools.list_ports, "comports", return_value=ports),
                      patch.object(f.serial, "Serial", side_effect=open_port)):
                    found, _ = f.discover_duts(threading.Event(), include_lan=False, include_serial=True)
                self.assertEqual([item.identity for item in found], [identity])
                self.assertEqual((found[0].settings.baudrate, found[0].settings.flow, found[0].settings.eol),
                                 (speed, "NONE", "CRLF"))

    def test_usb_com_discovery_works_without_visa_and_skips_calibrator_port(self):
        identity = 'NEWTONS4TH,PPA5511,TEST,1.00'
        ports = [SimpleNamespace(device='COM3', vid=0x0403),
                 SimpleNamespace(device='COM4', vid=0x0403),
                 SimpleNamespace(device='COM7', vid=None, hwid='BTHENUM\\7')]
        handles = []

        def open_port(address, **options):
            self.assertEqual(address, 'COM3')
            port = Mock()
            handles.append(port)
            if options['baudrate'] != 19200 or options['rtscts']:
                port.write.side_effect = OSError('no handshake')
            else:
                port.write.side_effect = len
                reply = (identity + '\r\n').encode('ascii')
                port.in_waiting = len(reply)
                port.read.return_value = reply
            return port

        with (patch.object(f, 'HAS_PYVISA', False),
              patch.object(f.serial.tools.list_ports, 'comports', return_value=ports),
              patch.object(f.serial, 'Serial', side_effect=open_port)):
            devices, _ = f.discover_duts(threading.Event(), excluded_resource='COM4 / 9600 / 8N1 / NONE',
                                         include_lan=False, include_serial=True)
        self.assertEqual([item.identity for item in devices], [identity])
        settings = devices[0].settings
        self.assertEqual((settings.address, settings.baudrate, settings.flow, settings.eol), ('COM3', 19200, 'NONE', 'CR'))
        self.assertEqual(len(handles), 3)
        for port, terminator in zip(handles, (b'\r\n', b'\r', b'\r')):
            port.write.assert_called_once_with(b'*IDN?' + terminator)
            port.close.assert_called_once()

    def test_pw3390_retries_identification_at_same_baud_rate(self):
        identity = "HIOKI,PW3390-02,TEST,V1.01"
        transport = Mock()
        transport.query.side_effect = [TimeoutError("first identification lost"), identity]
        settings = f.DUTSettings(mode="SERIAL", address="COM11", baudrate=38400, flow="NONE", eol="CRLF")
        session = f.DUTSession(Mock(return_value=transport))

        self.assertEqual(session.connect(settings), identity)
        self.assertEqual(transport.open.call_count, 2)
        self.assertEqual(transport.close.call_count, 1)
        self.assertEqual([args.args for args in transport.query.call_args_list], [("*IDN?",)] * 2)

    def test_pw3390_entered_address_probes_wired_tcp_without_visa(self):
        host = "192.168.10.3"
        source = "192.168.10.100"
        identity = "HIOKI,PW3390-03,TEST,V2.01"
        adapters = [{"state": "Up", "wired": False, "addresses": [{"ip": "192.168.10.99", "prefix": 24}]},
                    {"state": "Up", "wired": True, "addresses": [{"ip": source, "prefix": 24}]}]
        sock = Mock()
        sock.recv.return_value = (identity + "\r\n").encode("ascii")
        with (patch.object(f, "HAS_PYVISA", False),
              patch.object(f.socket, "create_connection", return_value=sock) as connect):
            found, _ = f.discover_duts(threading.Event(), adapters=adapters, lan_ip=host)
        connect.assert_called_once_with((host, 3390), timeout=1.0, source_address=(source, 0))
        sock.sendall.assert_called_once_with(b"*IDN?\r\n")
        sock.close.assert_called_once()
        self.assertEqual([item.identity for item in found], [identity])

    def test_pw3390_factory_address_needs_matching_wired_subnet_and_excludes_calibrator(self):
        adapters = [{"state": "Up", "wired": False, "addresses": [{"ip": "192.168.1.99", "prefix": 24}]},
                    {"state": "Up", "wired": True, "addresses": [{"ip": "192.168.10.100", "prefix": 24}]}]
        with (patch.object(f, "HAS_PYVISA", False),
              patch.object(f.socket, "create_connection", side_effect=TimeoutError) as connect):
            f.discover_duts(threading.Event(), adapters=adapters)
            connect.assert_not_called()
            adapters[1]["addresses"].append({"ip": "192.168.1.100", "prefix": 24})
            f.discover_duts(threading.Event(), adapters=adapters, excluded_ip="192.168.1.1")
            connect.assert_not_called()
            f.discover_duts(threading.Event(), adapters=adapters)
            connect.assert_called_once_with(("192.168.1.1", 3390), timeout=1.0, source_address=("192.168.1.100", 0))

    def test_pw3390_tcp_timeout_is_reported_in_search_status(self):
        with (patch.object(f, "HAS_PYVISA", False),
              patch.object(f.socket, "create_connection", side_effect=TimeoutError)):
            found, notes = f.discover_duts(threading.Event(), adapters=[], lan_ip="192.168.10.3")
        self.assertFalse(found)
        self.assertIn("TCP 192.168.10.3:3390 연결 시간 초과", notes[-1])
        self.assertIn("같은 서브넷", notes[-1])

    def test_only_verified_usb_or_lan_candidates_are_returned(self):
        calibrator = 'TCPIP0::192.168.1.21::inst0::INSTR'
        fluke_usb = 'USB0::0x0F7E::0x800A::7165657::INSTR'
        unknown_usb = 'USB0::0x1234::0x0040::UNKNOWN::INSTR'
        listing = [WT_USB, WT_USB.lower(), fluke_usb, unknown_usb, calibrator,
                   'TCPIP0::CALIBRATOR.local::inst0::INSTR', 'GPIB0::4::INSTR',
                   'TCPIP0::192.168.1.25::3490::SOCKET',
                   f'TCPIP0::{WT_LAN}::inst0::INSTR']
        manager = Mock()
        manager.list_resources.return_value = listing
        sessions = []

        def make_probe():
            probe = Mock()
            sessions.append(probe)

            def identify(settings):
                self.assertEqual(settings.timeout, 1)
                identities = {WT_USB: WT_ID, WT_LAN: WT_LAN_ID}
                if settings.address not in identities:
                    self.assertEqual(settings.address, unknown_usb)
                    raise ValueError('unrecognized instrument')
                identity = identities[settings.address]
                probe.profile = f.identify_dut(identity)
                return identity

            probe.connect.side_effect = identify
            return probe

        with (patch.object(f, 'HAS_PYVISA', True),
              patch.object(f, 'SharedVisaManager', return_value=manager),
              patch.object(f, 'DUTSession', side_effect=make_probe),
              patch.object(f, '_dut_discover_vxi11', return_value=['192.168.1.21', WT_LAN])):
            devices, notes = f.discover_duts(threading.Event(), excluded_resource=calibrator, adapters=[])
        self.assertEqual([device.identity for device in devices], [WT_ID, WT_LAN_ID])
        self.assertEqual(len(sessions), 3)
        for probe in sessions:
            probe.disconnect.assert_called_once()
        manager.close.assert_called_once()
        self.assertTrue(any('unrecognized instrument' in note for note in notes))

    def test_dut_gpib_auto_excludes_calibrator_and_probes_unique_addresses(self):
        manager = Mock()
        manager.list_resources.return_value = ["GPIB::4::INSTR", "GPIB0::05::INSTR",
                                               "GPIB0::5::INSTR", "GPIB0::INTFC"]
        probe = Mock()
        probe.profile = f.identify_dut(WT_ID)
        probe.connect.return_value = WT_ID
        with (patch.object(f, "HAS_PYVISA", True),
              patch.object(f, "SharedVisaManager", return_value=manager),
              patch.object(f, "DUTSession", return_value=probe) as session):
            found, _ = f.discover_duts(threading.Event(), excluded_resource="GPIB0::4::INSTR",
                                        include_usb=False, include_lan=False, include_gpib=True)
        session.assert_called_once_with()
        probe.disconnect.assert_called_once_with()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].settings.mode, "GPIB")
        self.assertEqual(found[0].settings.address, "GPIB0::05::INSTR")

    def test_vxi11_port_reply_checks_transaction_and_tcp_port(self):
        xid = 3575
        make_reply = lambda xid, status, port: struct.pack('!7I', xid, 1, 0, 0, 0, status, port)
        self.assertTrue(f._dut_vxi11_port_reply(make_reply(xid, 0, 1024), xid))
        for reply in (make_reply(xid + 1, 0, 1024), make_reply(xid, 1, 1024),
                      make_reply(xid, 0, 0), make_reply(xid, 0, 65536), b'partial'):
            self.assertFalse(f._dut_vxi11_port_reply(reply, xid))


class DiscoveryUiTests(unittest.TestCase):
    def test_manual_hioki_serial_sets_pw3390_defaults_and_omits_bluetooth(self):
        app = self.app
        app.var_dut_mode.set("RS232 · Hioki PW3390")
        app._dut_mode_changed(object())
        ports = [SimpleNamespace(device="COM9", vid=None, hwid="BTHENUM\\1"),
                 SimpleNamespace(device="COM11", vid=0x067B)]
        with patch.object(f.serial.tools.list_ports, "comports", return_value=ports):
            app.btn_dut_scan.invoke()
            self.wait(lambda: not app._dut_busy)
        settings = app._dut_settings()
        self.assertEqual((settings.address, settings.baudrate, settings.data_bits, settings.parity,
                          settings.stop_bits, settings.flow, settings.eol),
                         ("COM11", 38400, 8, "N", 1, "NONE", "CRLF"))

    def setUp(self):
        with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
        self.app.var_dut_direct.set(False)
        self.app.conn_mgr.send_command = Mock(side_effect=AssertionError('discovery must not operate calibrator'))
        self.usb = candidate(f.DUTSettings(mode='VISA', address=WT_USB, eol='LF'), WT_ID)
        self.lan = candidate(f.DUTSettings(mode='WT_LAN', address=WT_LAN, eol='LF'), WT_LAN_ID)

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def tearDown(self):
        self.app._on_close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if not self.app.winfo_exists():
                    break
                self.app.update()
            except f.tk.TclError:
                break
            time.sleep(.01)

    def connect_result(self, settings):
        self.app.dut_session.profile = self.usb.profile
        self.app.dut_session.identity = WT_ID if settings.mode == 'VISA' else WT_LAN_ID
        return self.app.dut_session.identity

    def test_connect_button_searches_and_connects_only_candidate(self):
        app = self.app
        with (patch.object(f, 'discover_duts', return_value=([self.usb], [])) as discover,
              patch.object(app.dut_session, 'connect', side_effect=self.connect_result) as connect):
            app.update()
            discover.assert_not_called()
            self.assertIn('DUT 연결', [app.notebook.tab(tab, 'text') for tab in app.notebook.tabs()])
            app.notebook.select(app.tab_dut)
            app.update()
            discover.assert_not_called()
            app.btn_dut_connect.invoke()
            self.wait(lambda: not app._dut_busy and app.dut_session.connected)
            discover.assert_called_once()
            self.assertTrue(discover.call_args.kwargs['include_serial'])
            self.assertTrue(discover.call_args.kwargs['include_gpib'])
            self.assertEqual(connect.call_args.args[0].address, WT_USB)
            self.assertEqual(app.var_dut_identity.get(), WT_ID)
            app.conn_mgr.send_command.assert_not_called()

    def test_pw3390_ip_field_feeds_auto_search_and_remembers_success(self):
        app = self.app
        host = "192.168.10.3"
        device = candidate(f.DUTSettings(mode="HIOKI_LAN", address=host, port=3390),
                           "HIOKI,PW3390-03,TEST,V2.01")

        def connect(settings):
            app.dut_session.settings = settings
            app.dut_session.profile = device.profile
            app.dut_session.identity = device.identity
            return device.identity

        app.var_dut_lan_ip.set(host)
        app.var_dut_direct.set(True)
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(f.Fluke5560AConnectionManager, "_connection_history_path", return_value=Path(folder) / "connections.json"),
              patch.object(f, "discover_duts", return_value=([device], [])) as discover,
              patch.object(f, "align_dut_direct_lan", return_value="192.168.10.100") as align,
              patch.object(app.dut_session, "connect", side_effect=connect),
              patch.object(app.dut_session, "apply_measurement_defaults", return_value="")):
            self.assertEqual(app.dut_details.winfo_manager(), "")
            app.btn_dut_details.invoke()
            self.assertEqual(app.dut_lan_row.winfo_manager(), "pack")
            app.btn_dut_connect.invoke()
            self.wait(lambda: not app._dut_busy and app.dut_session.connected)
            self.assertEqual(discover.call_args.kwargs["lan_ip"], host)
            self.assertEqual(discover.call_args.kwargs["direct_mask"], "255.255.255.0")
            self.assertEqual(align.call_args.args[:2], (host, "255.255.255.0"))
            reloaded = f.Fluke5560AConnectionManager()
            self.assertEqual(reloaded._connection_history["DUT-LAN"]["ip"], host)
            self.assertIn(f"{host}:3390", app.var_dut_identity.get())

    def test_no_device_opens_lan_settings(self):
        app = self.app
        with patch.object(f, 'discover_duts', return_value=([], [])):
            app.btn_dut_connect.invoke()
            self.wait(lambda: not app._dut_busy)
        self.assertEqual(app.dut_details.winfo_manager(), "pack")
        self.assertIn("LAN 설정", app.var_dut_status.get())

    def test_multiple_devices_require_selection_then_recheck_identity(self):
        app = self.app
        with (patch.object(f, 'discover_duts', return_value=([self.usb, self.lan], [])),
              patch.object(app.dut_session, 'connect', side_effect=self.connect_result) as connect,
              patch.object(app.dut_session, 'apply_measurement_defaults', return_value='100 ms') as defaults):
            app._dut_scan()
            self.wait(lambda: not app._dut_busy)
            self.assertEqual(len(app._dut_candidates), 2)
            self.assertEqual(app.var_dut_address.get(), '')
            app._dut_connect()
            connect.assert_not_called()
            app.var_dut_address.set(next(label for label in app._dut_candidates if WT_LAN in label))
            connect.side_effect = None
            connect.return_value = WT_ID
            app._dut_connect()
            self.wait(lambda: not app._dut_busy)
            self.assertFalse(app.dut_session.connected)
            self.assertIn('검색 때 확인한 장비와 다릅니다', app.var_dut_status.get())
            defaults.assert_not_called()
            connect.side_effect = self.connect_result
            app._dut_connect()
            self.wait(lambda: not app._dut_busy)
            app.var_dut_address.set(next(label for label in app._dut_candidates if WT_LAN in label))
            app._dut_connect()
            self.wait(lambda: not app._dut_busy)
            self.assertEqual(connect.call_args.args[0].address, WT_LAN)
            self.assertEqual(app.var_dut_identity.get(), WT_LAN_ID)
            defaults.assert_called_once()

    def test_com_discovery_selection_preserves_negotiated_settings(self):
        app = self.app
        identity = 'NEWTONS4TH,PPA5511,TEST,1.00'
        com = candidate(f.DUTSettings(mode='SERIAL', address='COM3', baudrate=19200, flow='NONE', eol='CR'),
                        identity)

        def connect_result(_settings):
            app.dut_session.profile = com.profile
            app.dut_session.identity = identity
            return identity

        with (patch.object(f, 'discover_duts', return_value=([com], [])),
              patch.object(app.dut_session, 'connect', side_effect=connect_result) as connect):
            app._dut_scan()
            self.wait(lambda: connect.called and not app._dut_busy)
            self.assertEqual(app.var_dut_address.get(), 'PPA5511 · COM · COM3')
            self.assertEqual(connect.call_args.args[0], com.settings)


if __name__ == '__main__':
    unittest.main()

