"""Return-to-center coverage across every scan terminal.

``returnToCenterAfterScan`` positioners were parked only on the completion
path, and only by the two scan controllers that called it explicitly. A run
that failed to build, or was aborted before an iteration was live, released
its actuator wherever the waveform left it -- and then published
``sigScanEnded``, which is what hands the focus axis back to the focus lock.

Parking therefore also happens at the run terminal, but only when the finished
iteration did not already do it, so multi-part and repeat sequences keep
parking per part with no extra hardware write.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def error(self, message, *args, **kwargs):  # pragma: no cover - unused
        pass


class _Positioner:
    def __init__(self, name, log):
        self._name = name
        self._log = log

    def setPosition(self, position, axis):
        self._log.append((self._name, position, axis))


class _PositionersManager(dict):
    pass


class _ScanController(SuperScanController):
    """Concrete stand-in: the abstract surface is irrelevant to parking."""

    def emitScanSignal(self, signal, *args):  # pragma: no cover - unused
        pass

    def getParameters(self):  # pragma: no cover - unused
        pass

    def setParameters(self):  # pragma: no cover - unused
        pass

    def updatePixels(self):  # pragma: no cover - unused
        pass

    def runScanAdvanced(self, **kwargs):  # pragma: no cover - unused
        pass

    def scanDone(self):  # pragma: no cover - unused
        pass


def _makeController(*, parks=True, failing=False):
    ctrl = _ScanController.__new__(_ScanController)
    ctrl._logger = _Logger()
    ctrl.moves = []

    ctrl._analogParameterDict = {
        'target_device': ['ND-GalvoX', 'ND-PiezoZ'],
        'axis_centerpos': [0.0, 25.0],
    }
    ctrl._positionersScan = ['ND-GalvoX']
    ctrl._scanPositionersRestored = False

    managers = _PositionersManager()
    for name in ('ND-GalvoX', 'ND-PiezoZ'):
        managers[name] = _Positioner(name, ctrl.moves)
    ctrl._master = SimpleNamespace(positionersManager=managers)

    properties = {
        'ND-GalvoX': {},
        'ND-PiezoZ': (
            {'returnToCenterAfterScan': parks, 'returnToCenterAfterScanAxis': 'Z'}
        ),
    }
    ctrl._setupInfo = SimpleNamespace(positioners={
        name: SimpleNamespace(managerProperties=props, axes=['Z'])
        for name, props in properties.items()
    })

    if failing:
        def explode(position, axis):
            raise RuntimeError('stage refused to park')

        managers['ND-PiezoZ'].setPosition = explode

    return ctrl


def _restore(ctrl):
    SuperScanController._restoreScanPositioners(ctrl)


def _restoreIfPending(ctrl):
    SuperScanController._restoreScanPositionersIfPending(ctrl)


def test_marked_positioner_is_parked_at_its_center():
    ctrl = _makeController()

    _restore(ctrl)

    assert ctrl.moves == [('ND-PiezoZ', 25.0, 'Z')]


def test_unmarked_positioners_are_left_alone():
    ctrl = _makeController(parks=False)

    _restore(ctrl)

    assert ctrl.moves == []


def test_the_terminal_parks_a_run_that_never_completed():
    """The gap: a failed or aborted run published sigScanEnded unparked."""
    ctrl = _makeController()

    _restoreIfPending(ctrl)

    assert ctrl.moves == [('ND-PiezoZ', 25.0, 'Z')]


def test_the_terminal_does_not_repark_after_a_normal_completion():
    ctrl = _makeController()

    _restore(ctrl)          # what scanDone does
    _restoreIfPending(ctrl)  # what the run terminal does

    assert ctrl.moves == [('ND-PiezoZ', 25.0, 'Z')], (
        'the terminal must not issue a second hardware write'
    )


def test_each_armed_iteration_parks_again():
    """Multi-part and repeat sequences park per part, as they always have."""
    ctrl = _makeController()

    _restore(ctrl)
    ctrl._scanPositionersRestored = False   # what _armScanIteration does
    _restore(ctrl)
    _restoreIfPending(ctrl)

    assert ctrl.moves == [
        ('ND-PiezoZ', 25.0, 'Z'),
        ('ND-PiezoZ', 25.0, 'Z'),
    ]


def test_a_stage_that_refuses_to_park_does_not_strand_the_run():
    ctrl = _makeController(failing=True)

    _restore(ctrl)  # must not raise

    assert ctrl._logger.warnings, 'the refusal must be reported'


def test_a_refusal_still_counts_as_attempted_for_this_iteration():
    """Retrying at the terminal would just fail again and log twice."""
    ctrl = _makeController(failing=True)

    _restore(ctrl)
    _restoreIfPending(ctrl)

    assert len(ctrl._logger.warnings) == 1


def test_missing_center_position_is_reported_not_guessed():
    ctrl = _makeController()
    ctrl._analogParameterDict['axis_centerpos'] = [0.0]

    _restore(ctrl)

    assert ctrl.moves == []
    assert ctrl._logger.warnings


def test_a_run_that_never_armed_does_not_move_the_stage():
    """A build failure in getParameters or signal construction publishes a
    terminal without ever driving a waveform. Treating the unset flag as
    "pending" made that terminal issue a hardware move anyway, possibly from a
    half-rebuilt parameter dict."""
    ctrl = _makeController()
    del ctrl._scanPositionersRestored   # never armed, so never cleared

    _restoreIfPending(ctrl)

    assert ctrl.moves == []


def test_an_unconfigured_axis_parks_under_the_positioner_own_axis_name():
    """Managers key tracked position by axis *name*. Defaulting to integer 0
    still wrote the voltage but recorded it under a brand-new 0 key, leaving
    position["Z"] stale for the next relative move. example_sted.json ships
    returnToCenterAfterScan with no axis, so this was the shipped path."""
    ctrl = _makeController()
    del ctrl._setupInfo.positioners['ND-PiezoZ'].managerProperties[
        'returnToCenterAfterScanAxis'
    ]

    _restore(ctrl)

    assert ctrl.moves == [('ND-PiezoZ', 25.0, 'Z')]


def test_an_explicit_axis_still_wins():
    ctrl = _makeController()
    ctrl._setupInfo.positioners['ND-PiezoZ'].managerProperties[
        'returnToCenterAfterScanAxis'
    ] = 'W'

    _restore(ctrl)

    assert ctrl.moves == [('ND-PiezoZ', 25.0, 'W')]
