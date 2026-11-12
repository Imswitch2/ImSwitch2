"""The event-trigger workflow script: tiling -> spots per tile -> a scan where there are spots.

Run headless against a fake ``api``: a synthetic tiling run on disk stands in
for the Tiling panel, a dict for the stage, a stub for the scan. What is
checked is the script's own logic -- which tiles it visits, in which order,
where it sends the stage, what it writes -- not the hardware.
"""

import contextlib
import csv
import importlib.util
import logging
from pathlib import Path

import numpy as np
import pytest

import imswitch

SCRIPT = (Path(imswitch.__file__).parent / '_data' / 'user_defaults' / 'scripts' / 'workflows'
          / 'event_trigger' / '01_spot_tiles_confocal.py')
SPOT_TESTS = Path(imswitch.__file__).parent / 'improcess' / '_test' / 'test_spot_count.py'


def _write_tiling_run(folder):
    spec = importlib.util.spec_from_file_location('_spot_count_tests', SPOT_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._write_tiling_run(folder)


class _Scan:
    def __init__(self, calls, stage):
        self._calls, self._stage = calls, stage

    def __call__(self, detectors, timeout=None, structured=False):
        self._calls.append((dict(self._stage), list(detectors), structured))
        return self

    def save(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        path = Path(folder) / 'scan_APD.hdf5'
        path.write_bytes(b'')
        return [str(path)]

    def show(self, name):
        raise RuntimeError('no image view')


class _ImControl:
    def __init__(self, run_folder):
        self.stage = {'X': 0.0, 'Y': 0.0}
        self.scans = []
        self.moves = []
        self._folder = ''
        self._run_folder = str(run_folder)
        self.recordScanToMemory = _Scan(self.scans, self.stage)

    def isTilingRunning(self):
        return False

    def getLastTilingFolder(self):
        return self._folder

    def startTiling(self):
        self._folder = self._run_folder  # a mock run: done at once

    def stopTiling(self):
        pass

    def getPositionerNames(self):
        return ['XY Stage']

    def setPositioner(self, name, axis, value):
        assert name == 'XY Stage'
        self.stage[axis] = float(value)
        self.moves.append((axis, float(value)))

    def getPositionerPosition(self, name, axis):
        return self.stage[axis]


def _run_script(tmp_path, *, replace=()):
    manifest, _layout, _spots = _write_tiling_run(tmp_path / 'run')
    imcontrol = _ImControl(manifest.parent)
    restored = []

    @contextlib.contextmanager
    def returningToPositions(stages=None):
        before = dict(imcontrol.stage)
        yield
        imcontrol.stage.update(before)
        restored.append(stages)

    def waitUntil(predicate, timeout=None):
        assert predicate()

    source = SCRIPT.read_text(encoding='utf-8')
    for old, new in replace:
        assert old in source
        source = source.replace(old, new)
    scope = {
        '__name__': '__main__',
        'api': type('Api', (), {'imcontrol': imcontrol})(),
        'getLogger': lambda: logging.getLogger('event-trigger-test'),
        'waitUntil': waitUntil,
        'sleep': lambda seconds: None,
        'returningToPositions': returningToPositions,
    }
    exec(compile(source, str(SCRIPT), 'exec'), scope)
    return imcontrol, restored, manifest.parent, scope


def test_scans_every_tile_with_spots_most_spots_first(tmp_path):
    imcontrol, restored, folder, scope = _run_script(tmp_path)
    found = scope['found']
    selected = found.selected
    assert len(selected['tile']) >= 1

    # One scan per selected tile, in rank order, with the stage on its target.
    assert len(imcontrol.scans) == len(selected['tile'])
    for (stage, detectors, structured), x, y in zip(
            imcontrol.scans, selected['target_x_um'], selected['target_y_um']):
        assert stage == pytest.approx({'X': x, 'Y': y})
        assert detectors == ['APD'] and structured
    assert (np.diff(selected['spots']) <= 0).all()
    assert restored == [['XY Stage']] and imcontrol.stage == {'X': 0.0, 'Y': 0.0}

    with open(folder / 'spot_targets.csv', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert [int(r['tile']) for r in rows] == found.tiles['tile'].tolist()
    assert (folder / 'spots.csv').is_file()
    scanned = sorted(p.name for p in (folder / 'confocal').iterdir())
    assert scanned == sorted(f'{r:02d}_tile{t:02d}' for r, t in zip(selected['rank'], selected['tile']))


def test_dry_run_and_a_high_minimum_move_nothing(tmp_path):
    imcontrol, _restored, _folder, _scope = _run_script(
        tmp_path / 'dry', replace=[('DRY_RUN = False', 'DRY_RUN = True')])
    assert imcontrol.scans == [] and imcontrol.moves == []

    imcontrol, _restored, _folder, _scope = _run_script(
        tmp_path / 'none', replace=[('MIN_SPOTS = 1 ', 'MIN_SPOTS = 999 ')])
    assert imcontrol.scans == [] and imcontrol.moves == []


def test_max_targets_limits_the_visits(tmp_path):
    imcontrol, _restored, _folder, scope = _run_script(
        tmp_path, replace=[('MAX_TARGETS = None', 'MAX_TARGETS = 1')])
    assert len(imcontrol.scans) == 1
    best = scope['found'].selected
    assert imcontrol.scans[0][0] == pytest.approx({'X': best['target_x_um'][0], 'Y': best['target_y_um'][0]})
