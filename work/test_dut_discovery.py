
"""DUT discovery tests without a live instrument or network requests."""
import struct
import sys
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
    def test_usb_com_discovery_works_without_visa_and_skips_calibrator_port(self):
        identity = 'NEWTONS4TH,PPA5511,TEST,1.00'
        ports = [SimpleNamespace(device='COM3', vid=0x0403),
                 SimpleNamespace(device='COM4', vid=0x0403),
                 SimpleNamespace(device='COM7', vid=None)]
        handles = []

        def open_port(address, **options):
            self.assertEqual(address, 'COM3')
            port = Mock()
            handles.append(port)
            if options['rtscts']:
                port.write.side_effect = TimeoutError('no handshake')
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
                                         include_serial=True)
        self.assertEqual([item.identity for item in devices], [identity])
        settings = devices[0].settings
        self.assertEqual((settings.address, settings.baudrate, settings.flow, settings.eol), ('COM3', 19200, 'NONE', 'CR'))
        self.assertEqual(len(handles), 2)
        for port in handles:
            port.write.assert_called_once_with(b'*IDN?\r')
            port.close.assert_called_once()

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

    def test_vxi11_port_reply_checks_transaction_and_tcp_port(self):
        xid = 3575
        make_reply = lambda xid, status, port: struct.pack('!7I', xid, 1, 0, 0, 0, status, port)
        self.assertTrue(f._dut_vxi11_port_reply(make_reply(xid, 0, 1024), xid))
        for reply in (make_reply(xid + 1, 0, 1024), make_reply(xid, 1, 1024),
                      make_reply(xid, 0, 0), make_reply(xid, 0, 65536), b'partial'):
            self.assertFalse(f._dut_vxi11_port_reply(reply, xid))


class DiscoveryUiTests(unittest.TestCase):
    def setUp(self):
        with patch.object(f.AutoNetworkAligner, 'list_ethernet_interfaces', return_value=[]):
            self.app = f.Fluke5560AControllerApp()
        self.app.withdraw()
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

    def test_first_dut_tab_visit_searches_and_connects_only_candidate(self):
        app = self.app
        with (patch.object(f, 'discover_duts', return_value=([self.usb], [])) as discover,
              patch.object(app.dut_session, 'connect', side_effect=self.connect_result) as connect):
            app.update()
            discover.assert_not_called()
            self.assertIn('DUT 연결', [app.notebook.tab(tab, 'text') for tab in app.notebook.tabs()])
            app.notebook.select(app.tab_dut)
            self.wait(lambda: not app._dut_busy and app.dut_session.connected)
            discover.assert_called_once()
            self.assertTrue(discover.call_args.kwargs['include_serial'])
            self.assertEqual(connect.call_args.args[0].address, WT_USB)
            self.assertEqual(app.var_dut_identity.get(), WT_ID)
            app.conn_mgr.send_command.assert_not_called()

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

