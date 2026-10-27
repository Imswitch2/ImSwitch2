"""The settings context managers: they apply, and put back on leaving --
also after an error -- only what they own; returningToPositions moves back;
a failed restore never hides the block's own error."""
import logging

import pytest

from imswitch.imscripting.model import getActionsScope


class _Imcontrol:
    def __init__(self):
        self.calls = []
        self.params = {'Camera': {'Set exposure time': 0.01, 'Trigger source': 'Internal',
                                  'Camera pixel size': 0.15}}
        self.lasers = {'488': [False, 0.0], '405': [True, None]}
        self.scan = {'step': 0.1}
        self.stages = {'XY': {'X': 1.0, 'Y': 2.0}}
        self.angles = {'HWP': 10.0}
        self.failOn = None

    def _log(self, *call):
        self.calls.append(call)
        if self.failOn and call[0] == self.failOn:
            raise RuntimeError(f'{call[0]} refused')

    def getDetectorParameters(self, d):
        return {n: {'value': v, 'editable': True, 'configOwned': n == 'Camera pixel size'}
                for n, v in self.params[d].items()}

    def getDetectorParameter(self, d, n):
        return self.params[d][n]

    def setDetectorParameter(self, d, n, v):
        self._log('setDetectorParameter', n, v)
        self.params[d][n] = v

    def getLaserInfo(self, n):
        return {'binary': self.lasers[n][1] is None}

    def isLaserActive(self, n):
        return self.lasers[n][0]

    def getLaserValue(self, n):
        return self.lasers[n][1]

    def setLaserValue(self, n, v):
        self._log('setLaserValue', n, v)
        self.lasers[n][1] = v

    def setLaserActive(self, n, on):
        self._log('setLaserActive', n, on)
        self.lasers[n][0] = on

    def getScanParameters(self):
        return dict(self.scan)

    def setScanParameters(self, p):
        self._log('setScanParameters', dict(p))
        self.scan = dict(p)

    def getPositionerPositions(self):
        return {s: dict(a) for s, a in self.stages.items()}

    def setPositioner(self, s, axis, v):
        self._log('setPositioner', s, axis, v)
        self.stages[s][axis] = v

    def getRotatorPosition(self, r):
        return self.angles[r]

    def setRotatorPosition(self, r, v):
        self._log('setRotatorPosition', r, v)
        self.angles[r] = v


@pytest.fixture
def imc():
    return _Imcontrol()


@pytest.fixture
def helpers(imc):
    api = type('api', (), {'imcontrol': imc})()
    return getActionsScope({'api': api})


def test_detector_settings_apply_and_come_back_also_after_an_error(imc, helpers):
    with pytest.raises(ZeroDivisionError):
        with helpers['temporaryDetectorSettings']('Camera', {'Set exposure time': 0.5}):
            assert imc.params['Camera']['Set exposure time'] == 0.5
            1 / 0
    assert imc.params['Camera']['Set exposure time'] == 0.01


def test_without_settings_only_what_the_block_changed_comes_back(imc, helpers):
    with helpers['temporaryDetectorSettings']('Camera'):
        imc.setDetectorParameter('Camera', 'Trigger source', 'External')
        imc.setDetectorParameter('Camera', 'Camera pixel size', 0.3)   # the setup's: not ours
    assert imc.params['Camera']['Trigger source'] == 'Internal'
    assert imc.params['Camera']['Camera pixel size'] == 0.3
    restores = [c for c in imc.calls[2:]]
    assert restores == [('setDetectorParameter', 'Trigger source', 'Internal')]


def test_scan_parameters_come_back(imc, helpers):
    with helpers['temporaryScanParameters']({'step': 0.05}):
        assert imc.scan == {'step': 0.05}
    assert imc.scan == {'step': 0.1}


def test_lasers_come_back_off_first_or_value_first(imc, helpers):
    with helpers['temporaryLaserSettings']({'488': {'value': 5.0, 'on': True},
                                            '405': {'on': False}}):
        assert imc.lasers['488'] == [True, 5.0] and imc.lasers['405'][0] is False
    imc.calls.clear()
    # (already restored above; check the order with a fresh run)
    with helpers['temporaryLaserSettings']({'488': {'value': 5.0, 'on': True}}):
        imc.calls.clear()
    assert imc.calls == [('setLaserActive', '488', False), ('setLaserValue', '488', 0.0)]
    assert imc.lasers['405'][0] is True


def test_positions_come_back_only_with_returning_to_positions(imc, helpers):
    with helpers['temporaryDetectorSettings']('Camera', {}):
        imc.setPositioner('XY', 'X', 50.0)
    assert imc.stages['XY']['X'] == 50.0                        # settings never move stages
    with helpers['returningToPositions'](stages=['XY'], rotators=['HWP']):
        imc.setPositioner('XY', 'Y', 9.0)
        imc.setRotatorPosition('HWP', 80.0)
    assert imc.stages['XY'] == {'X': 50.0, 'Y': 2.0} and imc.angles['HWP'] == 10.0


def test_a_failed_restore_raises_after_a_clean_block(imc, helpers):
    with pytest.raises(RuntimeError, match='Could not put back the scan parameters'):
        with helpers['temporaryScanParameters']({'step': 0.05}):
            imc.failOn = 'setScanParameters'


def test_a_failed_restore_never_hides_the_blocks_error(imc, helpers, caplog):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(KeyError):
            with helpers['temporaryScanParameters']({'step': 0.05}):
                imc.failOn = 'setScanParameters'
                raise KeyError('the real problem')
    assert 'Could not put back the scan parameters' in caplog.text


def test_every_step_is_tried_when_one_fails(imc, helpers):
    imc.failOn = 'setPositioner'
    with pytest.raises(RuntimeError, match='XY X'):
        with helpers['returningToPositions'](stages=['XY'], rotators=['HWP']):
            imc.angles['HWP'] = 5.0
            imc.stages['XY']['X'] = 7.0
    assert imc.angles['HWP'] == 10.0                            # still put back
