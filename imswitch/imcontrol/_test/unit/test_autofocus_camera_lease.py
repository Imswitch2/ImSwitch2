"""When the Autofocus panel holds its camera.

The panel used to take its FOCUS lease in the controller's constructor and
keep it until ImSwitch closed, so merely having the panel -- or adding it
while ImSwitch runs -- started the focus camera. It now takes the lease when
a focus search starts and lets go when the search is over, the way closing
the panel always did. What a search does in between is unchanged.
"""

import threading
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.controller.controllers.AutofocusController import (
    AutofocusController,
)
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose


pytestmark = pytest.mark.nohardware

_MODULE = 'imswitch.imcontrol.controller.controllers.AutofocusController'


class _CommChannel(QtCore.QObject):
    sigScanStarting = QtCore.Signal()
    sigScanEnded = QtCore.Signal()


class _DetectorsManager:
    """Records acquire/release; can refuse an acquire."""

    def __init__(self, detector):
        self._detector = detector
        self.acquired = []
        self.released = []
        self.refuse = None

    def acquire(self, names, purpose):
        if self.refuse is not None:
            raise self.refuse
        handle = f'lease-{len(self.acquired) + 1}'
        self.acquired.append((tuple(names), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)

    def __getitem__(self, name):
        return self._detector

    @property
    def held(self):
        return len(self.acquired) - len(self.released)


class _Positioner:
    def __init__(self):
        self.position = {'Z': 50.0}
        self.moves = []

    def setPosition(self, value, axis):
        self.position[axis] = value
        self.moves.append(value)


class _Widget:
    def __init__(self):
        self.focusButton = QtWidgets.QPushButton('Autofocus')
        self.focusButton.setCheckable(True)
        self.focusPlotCurve = SimpleNamespace(setData=lambda *a: None)
        self.zStepRangeEdit = QtWidgets.QLineEdit('20')
        self.zStepSizeEdit = QtWidgets.QLineEdit('10')


def _controller(detectorsManager, positioner, autofocus=True):
    setupInfo = SimpleNamespace(autofocus=SimpleNamespace(
        camera='CAM', positioner='STAGE', settleTimeMs=0.0,
    ) if autofocus else None)
    master = SimpleNamespace(
        detectorsManager=detectorsManager,
        positionersManager={'STAGE': positioner},
    )
    widget = _Widget()
    controller = AutofocusController(
        setupInfo=setupInfo, commChannel=_CommChannel(), master=master,
        widget=widget, factory=None, moduleCommChannel=None,
    )
    return controller, widget


@pytest.fixture
def frames(monkeypatch):
    """A sharp frame at Z=50, blurrier away from it, always fresh."""
    gate = {'event': None}

    def grab(detector, key, should_stop, logger):
        if gate['event'] is not None:
            gate['event'].wait(5)
        z = detector.positioner.position['Z']
        rng = np.random.default_rng(0)
        image = rng.random((16, 16)) * np.exp(-((z - 50.0) / 10.0) ** 2)
        return image, True

    monkeypatch.setattr(f'{_MODULE}.grab_fresh_frame', grab)
    return gate


@pytest.fixture
def rig():
    positioner = _Positioner()
    detector = SimpleNamespace(positioner=positioner)
    return _DetectorsManager(detector), positioner


def test_building_the_panel_starts_no_camera(qapp, rig):
    manager, positioner = rig
    controller, _ = _controller(manager, positioner)

    assert manager.acquired == []
    assert controller.shutdownComplete()


def test_a_search_leases_the_camera_and_lets_it_go_afterwards(qtbot, rig, frames):
    manager, positioner = rig
    controller, widget = _controller(manager, positioner)

    controller.autoFocus(20.0, 10.0)

    assert manager.acquired == [(('CAM',), LeasePurpose.FOCUS, 'lease-1')]
    qtbot.waitUntil(lambda: manager.released == ['lease-1'], timeout=5000)
    # The search itself is what it was: it swept Z and ended on the peak.
    assert positioner.moves[:3] == [40.0, 50.0, 60.0]
    assert abs(positioner.moves[-1] - 50.0) < 5.0
    assert widget.focusButton.isEnabled()
    assert controller.shutdownComplete()


def test_each_search_takes_and_returns_its_own_lease(qtbot, rig, frames):
    manager, positioner = rig
    controller, _ = _controller(manager, positioner)

    for expected in (1, 2):
        controller.autoFocus(20.0, 10.0)
        qtbot.waitUntil(lambda: len(manager.released) == expected, timeout=5000)

    assert [handle for *_, handle in manager.acquired] == ['lease-1', 'lease-2']
    assert manager.released == ['lease-1', 'lease-2']


def test_a_camera_that_cannot_start_starts_no_search(qapp, rig, frames):
    manager, positioner = rig
    manager.refuse = RuntimeError('Not connected: CAM')
    controller, widget = _controller(manager, positioner)

    controller.autoFocus(20.0, 10.0)

    assert controller._focusThread is None
    assert controller._focusing is False
    assert positioner.moves == []
    assert widget.focusButton.isEnabled()
    assert manager.released == []


def test_a_rejected_request_during_a_search_keeps_the_camera(qtbot, rig, frames):
    """Invalid input lands in _onFocusStopped too; it must not take the
    camera away from the worker still reading it."""
    manager, positioner = rig
    frames['event'] = threading.Event()
    controller, _ = _controller(manager, positioner)

    controller.autoFocus(20.0, 10.0)
    controller.autoFocus(0, 10.0)   # rejected: not a positive range

    assert manager.released == []
    assert manager.held == 1
    frames['event'].set()
    qtbot.waitUntil(lambda: manager.released == ['lease-1'], timeout=5000)


def test_closing_during_a_search_still_releases_once(qtbot, qapp, rig, frames):
    manager, positioner = rig
    frames['event'] = threading.Event()
    controller, _ = _controller(manager, positioner)

    controller.autoFocus(20.0, 10.0)
    threading.Timer(0.2, frames['event'].set).start()
    controller.closeEvent()
    qtbot.waitUntil(lambda: manager.released == ['lease-1'], timeout=5000)
    qapp.processEvents()

    assert manager.released == ['lease-1']
    assert controller.shutdownComplete()


def test_a_panel_without_an_autofocus_section_touches_nothing(qapp, rig):
    manager, positioner = rig
    controller, _ = _controller(manager, positioner, autofocus=False)

    controller.closeEvent()

    assert manager.acquired == [] and manager.released == []
