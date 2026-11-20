"""Connection policies and extension contracts without opening physical ports."""
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import tempfile

from calibration.domain.document import Document
from calibration.domain.planning import make_plan
from calibration.domain.types import ConnectionInfo
from calibration.instruments import catalog
from calibration.instruments.dut import DUTProfile, DUTSettings, DUTSession, identify_dut, DUTConfigurationError
from calibration.services.connections import DeviceHub, ConnectionSettings, CandidateChoice
from calibration.services.drivers import DriverRegistry, DUTAdapter, REGISTRY


class ConnectionTests(TestCase):
    def test_calibrator_auto_keeps_selected_model_if_wired_lan_is_unavailable(self):
        with patch('calibration.services.connections.align_dut_direct_lan', side_effect=ConnectionError('케이블 해제')), \
                patch.object(self.hub.fluke.manager, 'scan_and_auto_connect') as scan, \
                patch.object(self.hub.fluke, 'output_state', return_value=False):
            self.hub.connect_calibrator(ConnectionSettings(model='5730A', address='169.254.1.1'))
            self.assertEqual(scan.call_args.kwargs['lan_unavailable'], '케이블 해제')
            self.assertEqual(scan.call_args.kwargs['model'], '5730A')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict('os.environ', {'CALIBRATION_DATA_DIR': self.folder.name})
        self.env.start()
        self.hub = DeviceHub()
        self.hub.log = Mock()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.env.stop)

    def test_same_endpoint_is_rejected_for_serial_usb_gpib_and_lan(self):
        pairs = [('SERIAL', 'COM11', 'COM11', ''),
                 ('SERIAL', 'COM11', 'COM11 / 9600 / 8N1 / NONE', ''),
                 ('GPIB', 'GPIB0::5::INSTR', 'GPIB::5::INSTR', ''),
                 ('VISA', 'USB0::0x1234::0x0042::SERIAL::INSTR', 'USB0::4660::66::SERIAL::INSTR', ''),
                 ('HIOKI_LAN', '192.168.10.3', 'TCPIP::192.168.10.3::3490::SOCKET', '192.168.10.3')]
        for mode, address, resource, ip in pairs:
            with self.subTest(mode=mode):
                self.hub.fluke.manager.info = ConnectionInfo(True, 'TEST', resource, 'FLUKE,5560A,TEST,1', ip)
                with self.assertRaises(ValueError):
                    self.hub.check_dut_endpoint(DUTSettings(mode=mode, address=address))

    def fake_connect(self, settings, **kwargs):
        s = self.hub.session
        s.identity = 'Keysight Technologies,34465A,TEST,1'
        s.profile, s.settings, s.transport = identify_dut(s.identity), settings, Mock()
        return s.identity

    def test_direct_lan_passes_bound_wired_address_to_transport(self):
        with patch('calibration.services.connections.align_dut_direct_lan', return_value='192.168.10.100') as align, \
                patch.object(self.hub.session, 'connect', side_effect=self.fake_connect) as connect:
            self.hub.connect_dut(ConnectionSettings(mode='LAN', address='192.168.10.3', lan_protocol='DMM_TCP', port=5025))
        align.assert_called_once()
        self.assertEqual(connect.call_args.args[0].source_ip, '192.168.10.100')
        self.assertEqual(self.hub.dut.model, '34465A')
        self.assertIn('자동 선택', self.hub.defaults)

    def test_manual_serial_preserves_selected_parameters(self):
        with patch.object(self.hub.session, 'connect', side_effect=self.fake_connect) as connect:
            self.hub.connect_dut(ConnectionSettings(mode='RS232', address='COM11', baudrate=38400, parity='E', stop_bits=2))
        actual = connect.call_args.args[0]
        self.assertEqual((actual.mode, actual.address, actual.baudrate, actual.parity, actual.stop_bits),
                         ('SERIAL', 'COM11', 38400, 'E', 2))

    def test_auto_never_selects_arbitrary_device_when_multiple_reply(self):
        choices = [SimpleNamespace(identity='A'), SimpleNamespace(identity='B')]
        with patch('calibration.services.connections.discover_duts', return_value=(choices, [])), \
                patch.object(self.hub.session, 'connect') as connect:
            with self.assertRaises(CandidateChoice):
                self.hub.connect_dut(ConnectionSettings())
        connect.assert_not_called()

    def test_common_ppa_retains_read_only_support_but_blocks_output_workflow(self):
        s = DUTSession()
        s.profile = identify_dut('N4L,PPA999,TEST,1')
        s.settings = DUTSettings(mode='VISA', address='USB0::TEST::INSTR')
        s.transport = Mock()
        driver = DUTAdapter(s)
        self.assertIn('공통 조회', driver.initialize())
        doc = Document()
        doc.insert(doc.parse('1 V'))
        point = make_plan(doc.rows, 0, doc.options, doc.model)[0]
        with self.assertRaises(DUTConfigurationError):
            driver.prepare(point, Mock())
        s.transport.write.assert_not_called()

    def test_new_family_initializes_through_its_driver_without_wt_commands(self):
        class NewDriver:
            def __init__(self, session):
                self.session = session
            def initialize(self):
                return 'CUSTOM defaults verified'
        with patch.object(catalog, 'IDENTIFIERS', []), patch.object(catalog, 'LAN_PROBES', []), \
                patch.object(REGISTRY, 'registrations', []):
            REGISTRY.register('test meter', lambda p: p.family == 'TEST', NewDriver,
                identify=lambda identity: DUTProfile('TEST', 'METER', 2) if identity.startswith('TEST,') else None,
                lan_probes=(('SCPI_TCP', 12345, 'LF'),))
            s = DUTSession()
            s.profile = identify_dut('TEST,METER,SERIAL,1')
            s.transport = Mock()
            self.assertEqual(DUTAdapter(s).initialize(), 'CUSTOM defaults verified')
            self.assertEqual(catalog.LAN_PROBES, [('SCPI_TCP', 12345, 'LF')])
            s.transport.write.assert_not_called()

    def test_bad_extension_cannot_leave_partial_registration(self):
        registry = DriverRegistry()
        with patch.object(catalog, 'IDENTIFIERS', []), patch.object(catalog, 'LAN_PROBES', []):
            with self.assertRaises(ValueError):
                registry.register('bad', lambda p: True, object, identify=lambda s: None,
                                  lan_probes=(('SCPI_TCP', 0, 'LF'),))
            self.assertEqual((registry.registrations, catalog.IDENTIFIERS, catalog.LAN_PROBES), ([], [], []))
