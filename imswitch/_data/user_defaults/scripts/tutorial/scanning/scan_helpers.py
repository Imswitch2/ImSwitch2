"""Helpers for the scanning tutorials -- not a script to run on its own.

Imported by: 02_record_a_scan.py

(03_scan_timelapse.py and 04_camera_and_apd.py use it too.) Tutorial 01
does the same steps inline, with comments on each; these are those steps
as functions, so the later tutorials can stay short.
"""

import json
import os
import tempfile

_BACKUP = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan_backup.json')
_LOADED = os.path.join(tempfile.gettempdir(), 'imswitch_tutorial_scan.json')


def backupScanSettings():
    """Save what the Scan widget shows now; returns the file to load back."""
    api.imcontrol.saveScanParamsToFile(_BACKUP)
    return _BACKUP


def loadScanSettings(fileName, camera, stages):
    """Load scan_params/<fileName> into the Scan widget, with this setup's
    names: ``camera`` gets the trigger pulses, ``stages`` are the scan axes
    (one name per axis, fastest first). Returns the settings as loaded."""
    with open(os.path.join(getScriptDirPath(), 'scan_params', fileName),
              encoding='utf-8') as file:
        settings = json.load(file)
    analog = settings['analogParameterDict']
    if len(stages) != len(analog['axis_length']):
        raise ValueError(f'{fileName} scans {len(analog["axis_length"])} axes; '
                         f'got {len(stages)} stage names: {stages}')
    analog['target_device'] = list(stages)
    settings['positionersScan'] = list(stages)
    settings['digitalParameterDict']['target_device'] = [camera]
    with open(_LOADED, 'w', encoding='utf-8') as file:
        json.dump(settings, file, indent=2)
    api.imcontrol.loadScanParamsFromFile(_LOADED)
    return settings


def applyCameraSettings(camera, settings):
    """Set camera parameters -- {name: value}, names as in the Settings
    widget -- and return their previous values, to put back afterwards with
    applyCameraSettings(camera, previous)."""
    previous = {name: api.imcontrol.getDetectorParameter(camera, name)
                for name in settings}
    for name, value in settings.items():
        api.imcontrol.setDetectorParameter(camera, name, value)
    return previous
