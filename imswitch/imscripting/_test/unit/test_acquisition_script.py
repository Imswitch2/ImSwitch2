"""Copy acquisition as Python: the generated script compiles, sets every
setting it read, acquires the way the Recording widget was set, moves stages
and rotators only when asked, and leaves out what no literal can spell."""
from datetime import datetime

import numpy as np
import pytest

from imswitch.imscripting.model.acquisition_script import acquisitionScript


class _Imcontrol:
    """A fake api.imcontrol: the getters a setup with every widget has,
    and setters that record what the generated script does."""

    _OPTIONAL = {'Laser': 'getLaserNames', 'Scan': 'getScanParameters',
                 'Rotator': 'getRotatorNames'}

    def __init__(self, *, mode='ScanOnce', detectors=('Camera',), widgets=('all',)):
        self.calls = []
        self._mode, self._detectors = mode, list(detectors)
        self._missing = set() if 'all' in widgets else {
            getter for widget, getter in self._OPTIONAL.items() if widget not in widgets}

    def __getattribute__(self, name):
        if name in object.__getattribute__(self, '__dict__').get('_missing', ()):
            raise AttributeError(name)        # that widget is not loaded
        return object.__getattribute__(self, name)

    def getRecordingSettings(self):
        return {'mode': self._mode, 'frames': 7, 'detectors': self._detectors}

    def getDetectorNames(self):
        return list(self._detectors)

    def getDetectorParameters(self, name):
        return {
            'Set exposure time': {'value': np.float64(0.005), 'units': 's', 'editable': True},
            'Trigger source': {'value': 'External "frame-trigger"', 'units': None,
                               'editable': True},
            'Image width': {'value': 512, 'units': 'px', 'editable': False},
            'Camera pixel size': {'value': 0.15, 'units': 'µm', 'editable': True,
                                  'configOwned': True},
            'Strange': {'value': object(), 'units': None, 'editable': True},
        }

    def getLaserNames(self):
        return ['488', '405']

    def getLaserInfo(self, name):
        return {'wavelength': int(name), 'units': 'mW', 'binary': name == '405'}

    def getLaserValue(self, name):
        return 2.5

    def isLaserActive(self, name):
        return name == '488'

    def getScanParameters(self):
        return {'analogParameterDict': {'axis_step_size': [np.float32(0.25), 0.1]},
                'positionersScan': ['X', 'Y']}

    def getPositionerPositions(self):
        return {'X': {'X': 1.5}, 'Y': {'Y': -2.0}}

    def getRotatorNames(self):
        return ['HWP']

    def getRotatorPosition(self, name):
        return 45.0

    def __getattr__(self, name):          # every setter and acquisition: recorded
        if name.startswith('_') or name in self.__dict__.get('_missing', ()):
            raise AttributeError(name)

        def record(*args, **kwargs):
            object.__getattribute__(self, 'calls').append((name, args))
            if name.startswith('record'):
                return np.zeros((3, 4, 5))
        return record


def _run(text, imcontrol, **overrides):
    shown = []
    namespace = {'api': type('api', (), {'imcontrol': imcontrol})(),
                 'show': lambda value, name=None: shown.append(name)}
    code = text
    for name, value in overrides.items():
        code = code.replace(f'{name} = False', f'{name} = {value}')
    exec(compile(code, '<generated>', 'exec'), namespace)
    return namespace, shown


def test_a_scan_acquisition_round_trips_every_setting():
    imcontrol = _Imcontrol()
    text = acquisitionScript(imcontrol, now=datetime(2026, 10, 14, 20, 41))
    assert text.startswith('"""Acquisition copied from ImSwitch on 2026-10-14 at 20:41.')
    assert text.count('# %%') == 3

    namespace, shown = _run(text, imcontrol)

    calls = imcontrol.calls
    assert ('setDetectorParameter', ('Camera', 'Set exposure time', 0.005)) in calls
    assert ('setDetectorParameter', ('Camera', 'Trigger source', 'External "frame-trigger"')) in calls
    assert not any(c[1][1] in ('Image width', 'Camera pixel size')   # read-only; the setup's
                   for c in calls if c[0] == 'setDetectorParameter')
    # The value before switching on; an on/off laser gets no value.
    assert calls.index(('setLaserValue', ('488', 2.5))) < calls.index(('setLaserActive', ('488', True)))
    assert ('setLaserActive', ('405', False)) in calls
    assert not any(c == ('setLaserValue', ('405', 2.5)) for c in calls)
    scan = next(args[0] for name, args in calls if name == 'setScanParameters')
    assert scan['analogParameterDict']['axis_step_size'] == [0.25, 0.1]   # NumPy -> Python
    assert ('recordScanToMemory', ('Camera',)) in calls
    assert namespace['frames'].shape == (3, 4, 5) and shown == ['acquisition']


def test_stages_and_rotators_move_only_when_asked():
    imcontrol = _Imcontrol()
    text = acquisitionScript(imcontrol)
    _run(text, imcontrol)
    assert not any(c[0] in ('setPositioner', 'setRotatorPosition') for c in imcontrol.calls)

    imcontrol.calls.clear()
    _run(text, imcontrol, MOVE_TO_POSITIONS=True)
    assert ('setPositioner', ('X', 'X', 1.5)) in imcontrol.calls
    assert ('setRotatorPosition', ('HWP', 45.0)) in imcontrol.calls


def test_what_no_literal_spells_is_left_out_and_named():
    text = acquisitionScript(_Imcontrol())
    assert "'Strange'" not in text.split('# Left out')[0]
    assert '#   Camera: Strange (object)' in text


def test_a_frames_acquisition_without_lasers_scan_or_rotators():
    imcontrol = _Imcontrol(mode='SpecFrames', widgets=('Positioner',))
    text = acquisitionScript(imcontrol)
    assert 'LASERS' not in text and 'SCAN' not in text and 'ROTATOR' not in text
    _run(text, imcontrol)
    assert ('recordFramesToMemory', ('Camera', 7)) in imcontrol.calls


def test_a_scan_held_while_recording_frames_records_frames():
    imcontrol = _Imcontrol(mode='SpecFrames')
    _run(acquisitionScript(imcontrol), imcontrol)
    assert ('recordFramesToMemory', ('Camera', 7)) in imcontrol.calls
    assert not any(c[0] == 'recordScanToMemory' for c in imcontrol.calls)


def test_several_detectors_are_acquired_together():
    imcontrol = _Imcontrol(detectors=('Camera', 'APD'))
    namespace, shown = _run(acquisitionScript(imcontrol), imcontrol)
    assert ('recordScanToMemory', (['Camera', 'APD'],)) in imcontrol.calls
