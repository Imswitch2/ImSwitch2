"""An interleaved scan recorded by a camera, read back by the layout reader
and SNOUTY (docs/design/plans/scan-engines-and-expert-scan.md, M2).

The seam of test_acquisition_layout_recording_seam, for a scan that visits
its axis out of order: the Beta designer and the program compiler for the
waveforms, the real TTL designer for the camera, the producer builders for
the layout, ``RecordingManager.startRecording`` for the gate, the recording
worker and HDF5 storer for the file, and the ImProcess resolver to read it
back. Every recorded frame must resolve to the position the stage held when
the camera was triggered for it.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    encode_acquisition_layout,
    recorded_frame_coordinates,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts,
    validate_detector_edge_counts,
)
from imswitch.imcontrol.model import RecMode, SaveFormat, SaveMode, SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import HamamatsuManager
from imswitch.imcontrol.model.scan_program.advanced import program_scan_signals
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import BetaScanDesigner
from imswitch.improcess.model.acquisition_layout_resolver import resolve_acquisition_layout
from imswitch.improcess.reconstructors.snouty.metadata import recorded_snouty_geometry
from imswitch.improcess.reconstructors.snouty.restack import restack_interleaved

from .test_acquisition_layout_recording_seam import CAMERA_JSON, _POSITIONER, _text
from .test_mock_scan_simulation_coordinator import (
    _ManualDetectorsManager,
    _ensure_qcore_app,
    _recorded_dataset,
    _wait_for,
)

CYCLES, PLANES = 2, 3
POSITIONS = CYCLES * PLANES
STEP_UM = 0.5
DWELL_S = 2e-3
#: A simulated stepped stage (Beta's move and settle), short beside the dwell.
STAGE = {'return_time': 1e-3, 'move_time': 1e-4, 'settle_time': 1e-4}
#: The camera exposes at the start of each dwell, while the stage holds.
EXPOSURE_S = 0.5e-3


def _beta_setup():
    return SetupInfo.from_json(
        json.dumps({
            'detectors': {'Camera': CAMERA_JSON},
            'lasers': {},
            'positioners': {
                axis: dict(_POSITIONER, analogChannel=index, axes=[axis])
                for index, axis in enumerate(('X', 'Y', 'Z'))
            },
            'scan': {
                'scanWidgetType': 'Advanced',
                'scanDesigner': 'BetaScanDesigner',
                'scanDesignerParams': dict(STAGE),
                'TTLCycleDesigner': 'AdvancedScanTTLCycleDesigner',
                'TTLCycleDesignerParams': {},
                'sampleRate': 100000,
            },
            'nidaq': {'simulation': True, 'timerCounterChannel': None, 'startTrigger': False},
        }),
        infer_missing=True,
    )


def _interleaved_scan(setup):
    """Y over six positions as two cycles of three planes (1, 3, 5, 2, 4, 6);
    X held."""
    stage = dict(STAGE)
    stage.update({
        'target_device': ['X', 'Y', 'Z'],
        'axis_length': [0.0, POSITIONS * STEP_UM, 0.0],
        'axis_step_size': [1.0, STEP_UM, 1.0],
        'axis_centerpos': [0.0, 0.0, 0.0],
        'axis_startpos': [[0.0], [0.0], [0.0]],
        'sequence_time': DWELL_S,
        'n_linesteps': 1,
        'axis_order': ['forward', f'interleave:{CYCLES}', 'forward'],
    })
    designed, _positions, info = BetaScanDesigner().make_signal(copy.deepcopy(stage), setup)
    signals, info = program_scan_signals(stage, setup, designed, info)
    ttl, info = AdvancedScanTTLCycleDesigner().make_signal({
        'target_device': ['Camera'],
        'n_linesteps': 1,
        'linestep_enable': {'Camera': [True]},
        'pulse_starts_s': {'Camera': [[0.0]]},
        'pulse_ends_s': {'Camera': [[EXPOSURE_S]]},
        'sequence_time': DWELL_S,
        'advanced_mode': True,
    }, setup, info)
    return signals, ttl, info


def _rising_edges(line):
    levels = np.asarray(line, dtype=bool)
    return np.flatnonzero(levels & ~np.concatenate(([False], levels[:-1])))


@pytest.fixture
def qt_app():
    return _ensure_qcore_app()


def test_an_interleaved_camera_recording_reads_back_at_its_positions(qt_app, tmp_path):
    setup = _beta_setup()
    signals, ttl, info = _interleaved_scan(setup)
    assert info['img_dims'][:2] == [1, POSITIONS]

    layouts = build_advanced_scan_layouts(
        info, ('Camera',), scan_source='ScanControllerAdvanced',
        detector_masks={'Camera': [True]}, pulse_counts_by_condition={'Camera': [1]},
        devices={'scan_x': 'X', 'scan_y': 'Y', 'scan_z': 'Z'},
    )
    layout = layouts['Camera']
    assert layout.payload_kind == PAYLOAD_DETECTOR_FRAME_STREAM
    assert [(loop.kind, loop.count) for loop in layout.event_loops] == \
        [('cycle', CYCLES), ('plane', PLANES)]
    validate_detector_edge_counts(layouts, ttl)

    nidaq = NidaqManager(setup)
    camera = HamamatsuManager(setup.detectors['Camera'], 'Camera', nidaqManager=nidaq)
    detectors = _ManualDetectorsManager({'Camera': camera})
    recording = RecordingManager(detectors)
    memory = []
    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: memory.append((name, file, path, saved)))
    recording.startRecording(
        detectorNames=['Camera'], recMode=RecMode.ScanOnce,
        savename=str(tmp_path / 'interleaved'), saveMode=SaveMode.RAM,
        saveFormat=SaveFormat.HDF5, attrs={'Camera': {}}, recFrames=POSITIONS,
        numCamTTL={'Camera': 1}, stallTimeout=1.0, acquisitionLayouts=layouts,
    )
    assert recording.waitForAcquisitionStarted(2.0)
    try:
        nidaq.runScan({'scanSignalsDict': signals, 'TTLCycleSignalsDict': ttl}, info)
        assert _wait_for(lambda: len(memory) == 1, timeout=5.0)
        memFile, h5file, dataset = _recorded_dataset(memory, 'Camera')
        try:
            assert dataset.shape[0] == POSITIONS
            assert _text(dataset.attrs['recording:completion_outcome']) == 'complete'
            resolved = resolve_acquisition_layout(
                dict(dataset.attrs), shape=dataset.shape, detector='Camera')
            assert resolved.is_usable and resolved.is_authoritative
            assert encode_acquisition_layout(resolved.layout) == \
                encode_acquisition_layout(layout)

            # Frame f is the camera's f-th trigger; the stage held the
            # position the layout gives it.
            y = np.asarray(signals['Y']) * setup.positioners['Y'].managerProperties[
                'conversionFactor']
            edges = _rising_edges(ttl['Camera'])
            assert len(edges) == POSITIONS
            first = -(POSITIONS - 1) * STEP_UM / 2.0
            truth = []
            for frame in range(POSITIONS):
                c = recorded_frame_coordinates(resolved.layout, frame)
                index = c['cycle'] + CYCLES * c['plane']
                assert y[edges[frame]] == pytest.approx(first + index * STEP_UM)
                truth.append(index)
            assert truth == [0, 2, 4, 1, 3, 5]

            # SNOUTY reads its geometry from the recording and restacks the
            # frames into position order.
            geometry = recorded_snouty_geometry(SimpleNamespace(acquisition_layout=resolved))
            assert geometry[:2] == (CYCLES, PLANES)
            stack = np.asarray(truth, dtype=float)[:, None, None]
            assert restack_interleaved(stack, CYCLES, PLANES)[:, 0, 0].tolist() == \
                list(range(POSITIONS))
        finally:
            h5file.close()
            memFile.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)
        nidaq.finalize()


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
