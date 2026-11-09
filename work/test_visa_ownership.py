"""Shared VISA lifetime regressions; every VISA object is an in-memory fake."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import fluke_5560a_controller as f


CAL_ADDRESS = 'USB0::CALIBRATOR::INSTR'
DUT_ADDRESS = 'USB0::DUT::INSTR'


class FakeResource:
    def __init__(self, address):
        self.address = address
        self.closed = False
        self.commands = []

    def close(self):
        self.closed = True

    def _live(self):
        if self.closed:
            raise RuntimeError('Invalid VISA session')

    def write(self, command):
        self._live()
        self.commands.append(command)

    def query(self, command):
        self.write(command)
        return self.read()

    def read(self):
        self._live()
        if self.commands[-1] != '*IDN?':
            raise AssertionError('Ownership test must not send control commands')
        return 'FLUKE,5560A,TEST-CAL,4.0' if self.address == CAL_ADDRESS else 'YOKOGAWA,WT310E,TEST-DUT,1.0'

    def control_ren(self, _operation):
        self._live()


class CachedBackendManager:
    """Like PyVISA, manager.close invalidates ALL resources it created."""
    def __init__(self):
        self.visalib = object()
        self.resources = []
        self.closed = False
        self.fail_open = False
        self.close = Mock(side_effect=self._close)

    def _close(self):
        self.closed = True
        for resource in self.resources:
            resource.close()

    def list_resources(self, *_args, **_kwargs):
        if self.closed:
            raise RuntimeError('Invalid VISA manager')
        return (CAL_ADDRESS, DUT_ADDRESS)

    def open_resource(self, address, **_kwargs):
        if self.closed:
            raise RuntimeError('Invalid VISA manager')
        if self.fail_open:
            raise TimeoutError('fake open failure')
        resource = FakeResource(address)
        self.resources.append(resource)
        return resource


class VisaOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.backend = CachedBackendManager()
        self.leases, self.transports, self.calibrators = [], [], []
        self.pool_patches = [
            patch.object(f, '_VISA_POOL_LOCK', threading.RLock()),
            patch.object(f, '_VISA_POOL_MANAGER', None),
            patch.object(f, '_VISA_POOL_USERS', 0),
            patch.object(f.pyvisa, 'ResourceManager', return_value=self.backend),
        ]
        for item in self.pool_patches:
            item.start()
        self.constructor = self.pool_patches[-1].target.ResourceManager

    def tearDown(self):
        for transport in self.transports:
            transport.close()
        for calibrator in self.calibrators:
            calibrator.disconnect(standby=False)
        for lease in self.leases:
            lease.close()
        for item in reversed(self.pool_patches):
            item.stop()

    def lease(self):
        lease = f.SharedVisaManager()
        self.leases.append(lease)
        return lease

    def calibrator(self):
        calibrator = f.Fluke5560AConnectionManager()
        self.calibrators.append(calibrator)
        info = calibrator._try_visa_auto_scan('USB', '0', 1, CAL_ADDRESS)
        self.assertTrue(info.connected, calibrator.last_error)
        return calibrator

    def dut_transport(self):
        transport = f.DUTTransport(f.DUTSettings(address=DUT_ADDRESS), threading.Event())
        self.transports.append(transport)
        transport.open()
        return transport

    def test_dut_scan_close_keeps_active_calibrator_resource_alive(self):
        calibrator = self.calibrator()
        resource = calibrator._visa_inst
        scan = self.lease()
        self.assertIn(DUT_ADDRESS, scan.list_resources())
        scan.close()
        self.assertFalse(resource.closed)
        self.assertEqual(resource.query('*IDN?'), 'FLUKE,5560A,TEST-CAL,4.0')
        self.backend.close.assert_not_called()
        calibrator.disconnect(standby=False)
        self.backend.close.assert_called_once()

    def test_dut_disconnect_does_not_close_active_calibrator(self):
        calibrator = self.calibrator()
        dut = self.dut_transport()
        cal_resource, dut_resource = calibrator._visa_inst, dut.resource
        self.assertIn('WT310E', dut.query('*IDN?'))
        dut.close()
        self.assertTrue(dut_resource.closed)
        self.assertFalse(cal_resource.closed)
        self.assertIn('5560A', cal_resource.query('*IDN?'))
        self.backend.close.assert_not_called()
        calibrator.disconnect(standby=False)
        self.backend.close.assert_called_once()

    def test_calibrator_disconnect_does_not_close_active_dut(self):
        dut = self.dut_transport()
        calibrator = self.calibrator()
        cal_resource, dut_resource = calibrator._visa_inst, dut.resource
        calibrator.disconnect(standby=False)
        self.assertTrue(cal_resource.closed)
        self.assertFalse(dut_resource.closed)
        self.assertIn('WT310E', dut.query('*IDN?'))
        self.backend.close.assert_not_called()
        dut.close()
        self.backend.close.assert_called_once()

    def test_closing_a_lease_is_idempotent_and_last_owner_closes_backend(self):
        first, second = self.lease(), self.lease()
        first_resource = first.open_resource(CAL_ADDRESS)
        second_resource = second.open_resource(DUT_ADDRESS)
        self.assertIs(first.visalib, self.backend.visalib)
        self.constructor.assert_called_once()
        first.close()
        first.close()
        self.assertTrue(first_resource.closed)
        self.assertFalse(second_resource.closed)
        self.backend.close.assert_not_called()
        second.close()
        second.close()
        self.backend.close.assert_called_once()
        self.assertEqual(f._VISA_POOL_USERS, 0)
        self.assertIsNone(f._VISA_POOL_MANAGER)

    def test_failed_dut_open_releases_only_its_lease(self):
        calibrator = self.calibrator()
        cal_resource = calibrator._visa_inst
        self.backend.fail_open = True
        with self.assertRaisesRegex(TimeoutError, 'fake open failure'):
            self.dut_transport()
        self.assertFalse(cal_resource.closed)
        self.assertIn('5560A', cal_resource.query('*IDN?'))
        self.assertEqual(f._VISA_POOL_USERS, 1)
        self.backend.close.assert_not_called()
        calibrator.disconnect(standby=False)
        self.backend.close.assert_called_once()

    def test_manager_creation_failure_leaves_pool_empty(self):
        self.constructor.side_effect = RuntimeError('fake backend missing')
        with self.assertRaisesRegex(RuntimeError, 'fake backend missing'):
            self.lease()
        self.assertEqual(f._VISA_POOL_USERS, 0)
        self.assertIsNone(f._VISA_POOL_MANAGER)
        self.backend.close.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
