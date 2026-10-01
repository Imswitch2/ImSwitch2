"""End-to-end integration tests for the Phase-5 wiring.

Proves the abstraction works: a ``PulseGeneratorLaserManager`` looking
up ``lowLevelManagers['pulseGeneratorManager']`` finds a
``TeensyPulseManager``, which in turn drives a
``MockTeensyPulseDriver`` whose pin states the test can assert.

These tests bypass ``MasterController`` and wire the chain manually,
which keeps them fast and isolated.  A separate smoke test below loads
the manager via a ``SetupInfo`` instance to confirm the
``TeensyPulseInfo`` field round-trips through the JSON-friendly
dataclass machinery.
"""

from __future__ import annotations

import pytest

from imswitch.imcontrol.model.SetupInfo import (
    LaserInfo,
    SetupInfo,
    TeensyPulseInfo,
)
from imswitch.imcontrol.model.managers.lasers.PulseGeneratorLaserManager import (
    PulseGeneratorLaserManager,
)
from imswitch.imcontrol.model.managers.pulsegen import TeensyPulseManager


def _make_laser_info(*, digital=None, analog=None):
    """Build a minimal LaserInfo for tests.

    ``valueRangeMin/Max`` and ``wavelength`` are required positional
    fields on the dataclass — pick neutral values."""
    return LaserInfo(
        analogChannel=analog,
        digitalLine=digital,
        managerName='PulseGeneratorLaserManager',
        managerProperties={},
        valueRangeMin=0,
        valueRangeMax=1,
        wavelength=488,
    )


@pytest.fixture
def pulse_chain():
    """Manager + driver, no real hardware.  Returns the manager so
    tests can inspect ``manager.driver.pin_states`` and ``timeline``.
    """
    return TeensyPulseManager(TeensyPulseInfo(
        port=None,
        useMockOnFailure=True,
        mockNChannels=8,
    ))


# ---------------------------------------------------------------------------
# Phase-5 wiring: laser → pulse generator → mock driver
# ---------------------------------------------------------------------------


class TestLaserToPulseGenerator:
    def test_setEnabled_flips_pin(self, pulse_chain):
        laser_info = _make_laser_info(digital=2)
        laser = PulseGeneratorLaserManager(
            laser_info, name='488',
            pulseGeneratorManager=pulse_chain,
        )

        laser.setEnabled(True)
        assert pulse_chain.driver.get_pin_state(2) is True

        laser.setEnabled(False)
        assert pulse_chain.driver.get_pin_state(2) is False

    def test_binary_when_backend_lacks_analog(self, pulse_chain):
        # Mock Teensy driver reports supports_analog=False.  Configuring
        # an analogChannel should NOT make the laser non-binary —
        # the manager downgrades transparently.
        laser_info = _make_laser_info(digital=2, analog=1)
        laser = PulseGeneratorLaserManager(
            laser_info, name='488',
            pulseGeneratorManager=pulse_chain,
        )
        assert laser.isBinary is True, (
            'analog ch configured but backend supports_analog=False — '
            'manager should downgrade to binary'
        )
        # And setValue is a silent no-op on a binary laser.
        laser.setValue(0.5)

    def test_mock_mode_when_no_pulse_generator(self):
        laser_info = _make_laser_info(digital=2)
        laser = PulseGeneratorLaserManager(
            laser_info, name='488',
            pulseGeneratorManager=None,
        )
        # All calls accepted, none raise.
        laser.setEnabled(True)
        laser.setEnabled(False)
        laser.setValue(0.5)


# ---------------------------------------------------------------------------
# SetupInfo round trip: confirms the dataclass field is wired correctly
# ---------------------------------------------------------------------------


class TestSetupInfoIntegration:
    def test_teensy_pulse_info_default_is_none(self):
        s = SetupInfo()
        assert s.teensyPulse is None

    def test_teensy_pulse_info_constructs_with_full_config(self):
        info = TeensyPulseInfo(
            port='/dev/ttyACM0',
            baud=115200,
            pinMap={'laser488': 2, 'camera': 4},
            useMockOnFailure=True,
        )
        s = SetupInfo(teensyPulse=info)
        assert s.teensyPulse is info
        assert s.teensyPulse.pinMap['laser488'] == 2

    def test_manager_constructs_from_setup_info(self):
        """The Phase-5-wired MasterController path: read
        ``setupInfo.teensyPulse`` and pass it to ``TeensyPulseManager``.
        We replicate that without instantiating the full Master."""
        info = TeensyPulseInfo(port=None, useMockOnFailure=True)
        mgr = TeensyPulseManager(info)
        assert mgr.connected is True
        # Capabilities reflect the mock defaults.
        assert mgr.n_digital_channels == 16
        assert mgr.min_pulse_width_ns == 1_000


# ---------------------------------------------------------------------------
# Hardware-validation marker
# ---------------------------------------------------------------------------
#
# A separate set of tests targeting a real Teensy lives outside this
# file — they require ``--with-teensy=<port>`` and are skipped by
# default.  The mock-based tests above cover semantic correctness; only
# wire-format compatibility on real silicon is missing, and that's what
# v4_smoke_test_checklist.md walks through manually.


@pytest.mark.skip(reason='requires real Teensy hardware; walk v4_smoke_test_checklist.md instead')
def test_real_teensy_round_trip():  # pragma: no cover
    """Placeholder for a future opt-in hardware test.  Once we add a
    --with-teensy CLI flag the body becomes:

        from imswitch.imcontrol.model.interfaces.teensypulse import TeensyPulseDriver
        drv = TeensyPulseDriver(port=request.config.getoption('--with-teensy'))
        assert drv.capabilities.is_v4
        drv.pin(0, True); assert ...  # multimeter check, manual
        drv.upload_sequence([...], 1); drv.start(); drv.wait_done()
        drv.close()
    """


class _StuckThread:
    def is_alive(self):
        return True

    def join(self, timeout=None):
        return None


def test_stop_reports_a_worker_that_did_not_stop(pulse_chain):
    """stop() used to return as if it had succeeded while the worker was still
    inside the driver, so the next run was refused as 'already running' with
    nothing saying why."""
    manager = pulse_chain
    manager._running = True
    manager._run_thread = _StuckThread()
    with pytest.raises(TimeoutError, match='did not stop within 2 s'):
        manager.stop()
