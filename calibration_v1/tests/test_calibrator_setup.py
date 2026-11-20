"""Operator setup, remembering confirmed transports and retrying from the main window."""
import os
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from calibration.domain.commands import MODEL_PROFILES
from calibration.domain.connection_guide import connection_guide, supported_modes
from calibration.instruments.transport import CalibratorRecoveryTarget
from calibration.services.connections import DeviceHub, ConnectionSettings
from calibration.services.storage import data_dir, write_json
from calibration.ui.dialogs import ConnectionDialog
from calibration.ui.window import MainWindow
from tests.protocols.test_communications import FakeInstrument


class CalibratorSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'CALIBRATION_DATA_DIR': folder}))

    def test_supported_modes_and_guides_for_every_source(self):
        dialog = ConnectionDialog(ConnectionSettings(), 'calibrator')
        for model, profile in MODEL_PROFILES.items():
            dialog.model.setCurrentIndex(dialog.model.findData(model))
            self.assertEqual(tuple(dialog.mode.itemData(i) for i in range(dialog.mode.count())), ('AUTO', *profile.interfaces))
            for mode in supported_modes(model):
                with self.subTest(model=model, mode=mode):
                    dialog.mode.setCurrentIndex(dialog.mode.findData(mode))
                    self.assertEqual((dialog.panel_guide.text(), dialog.pc_guide.text()), connection_guide(model, mode))
                    self.assertTrue(dialog.form.isRowVisible(dialog.port) == (mode in ('AUTO', 'LAN') and 'LAN' in profile.interfaces))
                    if mode == 'RS232' and model in ('5520A', '5522A'):
                        self.assertIn('SERIAL 1 FROM HOST', dialog.panel_guide.text())
                        self.assertIn('HOST serial', dialog.panel_guide.text())
                    if mode == 'USB':
                        self.assertIn('가상 COM' if model == '5730A' else 'NI-VISA', dialog.pc_guide.text())
                    if model == '5080A' and mode == 'LAN':
                        self.assertIn('HOST Ethernet', dialog.panel_guide.text())
                        self.assertIn('LAN SETUP', dialog.panel_guide.text())
        dialog.close()

    def test_changing_cable_drops_unrelated_address_and_restores_draft(self):
        dialog = ConnectionDialog(ConnectionSettings(model='5560A', mode='LAN', address='169.254.1.1'), 'calibrator')
        dialog.mode.setCurrentIndex(dialog.mode.findData('RS232'))
        self.assertEqual(dialog.address.text(), '')
        dialog.address.setText('COM11')
        dialog.mode.setCurrentIndex(dialog.mode.findData('LAN'))
        self.assertEqual(dialog.address.text(), '169.254.1.1')
        dialog.model.setCurrentIndex(dialog.model.findData('5522A'))
        self.assertEqual((dialog.mode.currentData(), dialog.address.text()), ('AUTO', ''))
        dialog.close()

    def test_actual_lan_endpoint_survives_restart_with_auto_preference(self):
        hub = DeviceHub()
        with FakeInstrument(identity='FLUKE,5080A,UNIT17,2.0') as server:
            config = ConnectionSettings(model='AUTO', address='127.0.0.1', direct=False, port=server.port)
            hub.connect_calibrator(config)
            hub.disconnect('calibrator')
        self.assertEqual(hub.saved('calibrator').mode, 'AUTO')
        preset, = DeviceHub().calibrator_history.choices()
        self.assertEqual((preset.model, preset.serial, preset.mode, preset.address, preset.port),
                         ('5080A', 'UNIT17', 'LAN', '127.0.0.1', server.port))
        self.assertFalse(preset.direct)
        self.assertTrue(preset.connected_at)
        self.assertEqual(preset.apply(config), replace(config, model='5080A', mode='LAN'))

    def test_failed_output_status_leaves_confirmed_history_intact(self):
        hub = DeviceHub()
        target = CalibratorRecoveryTarget('RS232', 'COM7', 'FLUKE,5522A,TEST,1', '5522A', 3,
                                          serial_settings=(9600, 8, 'N', 1, 'NONE'))
        hub.calibrator_history.remember(target, ConnectionSettings())
        hub.fluke.manager._active_endpoint = replace(target, address='COM8')
        with patch.object(hub.fluke.manager, 'scan_and_auto_connect'), \
                patch.object(hub.calibrator, 'output_state', side_effect=TimeoutError('상태 조회 실패')):
            with self.assertRaises(TimeoutError):
                hub.connect_calibrator(ConnectionSettings(model='5522A', mode='RS232', address='COM8'))
        self.assertEqual([r.address for r in DeviceHub().calibrator_history.confirmed()], ['COM7'])

    def test_only_actual_route_gets_the_entered_lan_mask(self):
        hub = DeviceHub()
        target = CalibratorRecoveryTarget('LAN', '169.254.1.1', 'FLUKE,5080A,TWO,1', '5080A',
                                          3, 'CRLF', 3490, '169.254.1.100')
        entered = ConnectionSettings(address='169.254.001.001', mask='255.255.0.0', direct=True)
        hub.calibrator_history.remember(target, entered)
        preset, = hub.calibrator_history.confirmed()
        self.assertEqual((preset.direct, preset.mask), (True, '255.255.0.0'))
        self.assertTrue(preset.apply(ConnectionSettings(direct=False)).direct)
        hub.calibrator_history.remember(target, replace(entered, address='192.168.1.5'))
        fallback, = hub.calibrator_history.confirmed()
        self.assertEqual((fallback.direct, fallback.mask), (None, ''))

    def test_failed_new_lookup_does_not_become_an_old_success_on_restart(self):
        hub = DeviceHub()
        with patch.object(hub.fluke, 'output_state', side_effect=TimeoutError('조회 실패')), \
                FakeInstrument(identity='FLUKE,5080A,NO-STATUS,2.0') as server:
            try:
                with self.assertRaises(TimeoutError):
                    hub.connect_calibrator(ConnectionSettings(mode='LAN', model='5080A', address='127.0.0.1',
                                                              direct=False, port=server.port))
            finally:
                hub.fluke.manager.disconnect(standby=False)
        self.assertEqual(DeviceHub().calibrator_history.choices(), [])

    def test_success_updates_only_one_unit_and_cable(self):
        history = DeviceHub().calibrator_history
        base = CalibratorRecoveryTarget('USB-COM', 'COM5', 'FLUKE,5730A,ONE,1', '5730A', 7.5,
                                        'LF', serial_settings=(19200, 8, 'N', 1, 'XON/XOFF'))
        for target in (base, replace(base, identity='FLUKE,5730A,TWO,2'), replace(base, interface='RS232', address='COM8'),
                       replace(base, interface='GPIB', address='GPIB1::6::INSTR', serial_settings=()),
                       replace(base, address='COM12', identity='FLUKE,5730A,ONE,3')):
            history.remember(target, ConnectionSettings(operation_timeout=95))
        records = history.choices()
        self.assertEqual([(r.serial, r.mode) for r in records], [('ONE', 'USB'), ('ONE', 'GPIB'), ('ONE', 'RS232'), ('TWO', 'USB')])
        dialog = ConnectionDialog(ConnectionSettings(), 'calibrator', presets=records, allow_connect=True)
        dialog.load_button.click()
        self.assertEqual((dialog.value().address, dialog.value().flow, dialog.value().baudrate, dialog.value().eol),
                         ('COM12', 'XON/XOFF', 19200, 'LF'))
        self.assertFalse(dialog.connect_requested)
        self.assertEqual((dialog.value().timeout, dialog.value().operation_timeout), (7.5, 95))
        self.assertIn('PC 송신 줄 끝 LF', dialog.preset_detail.text())
        dialog.mode.setCurrentIndex(dialog.mode.findData('GPIB'))
        dialog.load_button.click()
        self.assertEqual(dialog.value().address, 'GPIB1::6::INSTR')
        dialog.close()

    def test_legacy_addresses_remain_separate_and_unknown_com_is_labeled(self):
        write_json(data_dir() / 'connections.json', {'5080A': {'interface': 'LAN', 'ip': '169.254.1.1',
                   'port': 3490, 'source_ip': '169.254.1.100', 'com': 'COM8'},
                   '5730A': {'interface': 'COM', 'com': 'COM11', 'serial_settings': [9600, 8, 'N', 1, 'NONE', 'CRLF']}})
        lan, com = DeviceHub().calibrator_history.choices()
        self.assertIn('이전 버전', lan.details())
        self.assertIn('PC 169.254.1.100', lan.details())
        self.assertTrue(lan.apply(ConnectionSettings(direct=True)).direct)
        self.assertFalse(lan.apply(ConnectionSettings(direct=False)).direct)
        self.assertIn('케이블 미확인', com.title())
        for mode in ('AUTO', 'USB', 'RS232'):
            self.assertTrue(com.matches('5730A', mode))
            self.assertEqual(com.apply(ConnectionSettings(mode=mode)).mode, mode)

    def test_invalid_manual_address_does_not_open_or_change_routes(self):
        hub = DeviceHub()
        with patch('calibration.services.connections.align_dut_direct_lan') as align, \
                patch.object(hub.fluke.manager, 'scan_and_auto_connect') as scan:
            for model, mode, value in [('5520A', 'LAN', '192.168.10.5'), ('5522A', 'RS232', '192.168.10.5'),
                                       ('5730A', 'USB', 'USB0::123::INSTR'), ('5560A', 'GPIB', '44')]:
                with self.subTest(model=model, mode=mode), self.assertRaises(ValueError):
                    hub.connect_calibrator(ConnectionSettings(model=model, mode=mode, address=value))
            align.assert_not_called()
            scan.assert_not_called()

    def test_manual_serial_uses_entered_settings_even_with_automatic_model(self):
        hub = DeviceHub()
        config = ConnectionSettings(mode='RS232', address='COM11', baudrate=4800, data_bits=7,
                                    parity='E', stop_bits=2, flow='RTS/CTS', eol='CR')
        mgr = hub.fluke.manager
        with patch.object(mgr, '_try_serial', return_value=mgr.info) as attempt, self.assertRaises(ConnectionError):
            hub.connect_calibrator(config)
        attempt.assert_called_once_with('COM11', 3.0, 'CR', 4800, 7, 'E', 2, 'RTS/CTS', stream_mode='RS232')

    def test_failed_auto_opens_setup_then_explicit_click_retries_the_saved_values(self):
        window = MainWindow()
        seen = []
        def use_dialog(dialog):
            seen.append((dialog.connect_button.text(), window.busy))
            dialog.model.setCurrentIndex(dialog.model.findData('5522A'))
            dialog.mode.setCurrentIndex(dialog.mode.findData('RS232'))
            dialog.address.setText('COM11')
            dialog.connect_button.click()
            return dialog.result()
        with patch.object(window.hub, 'connect_calibrator', side_effect=[ConnectionError('연결 실패'), object()]) as connect, \
                patch.object(ConnectionDialog, 'exec', use_dialog):
            window.connect_device('calibrator')
            end = time.monotonic() + 3
            while (connect.call_count < 2 or window.busy) and time.monotonic() < end:
                self.qt.processEvents()
                time.sleep(.005)
            self.assertEqual(seen, [('다시 연결', False)])
            self.assertEqual([(c.args[0].mode, c.args[0].address) for c in connect.call_args_list],
                             [('AUTO', ''), ('RS232', 'COM11')])
            self.assertEqual(window.hub.saved('calibrator').model, '5522A')
        window.close()

    def test_normal_save_cancel_and_standby_recovery_do_not_retry(self):
        window = MainWindow()
        def save(dialog):
            dialog.model.setCurrentIndex(dialog.model.findData('5080A'))
            return 1
        with patch.object(window, 'connect_device') as reconnect:
            with patch.object(ConnectionDialog, 'exec', save):
                window.connection_settings('calibrator')
            self.assertEqual(window.hub.saved('calibrator').model, '5080A')
            with patch.object(ConnectionDialog, 'exec', return_value=0):
                window.connection_settings('calibrator', failure='시간 초과')
            window.hub.connection_cancel.set()
            window.calibrator_connect_error(ConnectionError('검색 중지'))
            self.assertIsNone(window._pending_success)
            window.hub.connection_cancel.clear()
            window.hub.fluke.manager.recovery_target = object()
            window.calibrator_connect_error(ConnectionError('STBY 확인 필요'))
            self.assertIsNone(window._pending_success)
            reconnect.assert_not_called()
        window.close()
