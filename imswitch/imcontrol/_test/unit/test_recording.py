import pytest

import h5py
import numpy as np

from imswitch.imcontrol.model import DetectorsManager, RecordingManager, RecMode, SaveMode, DetectorInfo
from . import detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare


def record(qtbot, detectorInfos, *args, **kwargs):
    detectorsManager = DetectorsManager(detectorInfos, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)

    filePerDetector, savedToDiskPerDetector = {}, {}

    def memoryRecordingAvailable(_, file, __, savedToDisk, detectorName):
        nonlocal filePerDetector, savedToDiskPerDetector
        filePerDetector[detectorName], savedToDiskPerDetector[detectorName] = file, savedToDisk
        return True

    recordingManager.startRecording(*args, **kwargs)
    with qtbot.waitSignals(
            [recordingManager.sigMemoryRecordingAvailable for _ in detectorInfos],
            check_params_cbs=[(lambda *args, detectorName=detectorName, **kwargs:
                               memoryRecordingAvailable(*args, detectorName=detectorName, **kwargs))
                              for detectorName in detectorInfos],
            timeout=30000
    ):
        pass

    return filePerDetector, savedToDiskPerDetector


@pytest.mark.parametrize('detectorInfos,numFrames',
                         [(detectorInfosBasic, 10), (detectorInfosNonSquare, 53)])
def test_recording_spec_frames(qtbot, detectorInfos, numFrames):
    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecFrames,
        savename='test_spec_frames',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recFrames=numFrames
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    assert savedToDiskPerDetector.keys() == detectorInfos.keys()

    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        dataset = h5pyFile.get(detectorName)
        assert dataset.shape[0] == numFrames
        h5pyFile.close()  # Otherwise we can get segfaults
        file.close()  # Otherwise we can get segfaults
    for savedToDisk in savedToDiskPerDetector.values():
        assert savedToDisk is False


@pytest.mark.parametrize('detectorInfos',
                         [detectorInfosBasic, detectorInfosMulti, detectorInfosNonSquare])
def test_recording_spec_time(qtbot, detectorInfos):
    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecTime,
        savename='test_spec_time',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recTime=5
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    assert savedToDiskPerDetector.keys() == detectorInfos.keys()

    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        dataset = h5pyFile.get(detectorName)
        assert dataset.shape[0] > 0
        h5pyFile.close()  # Otherwise we can get segfaults
        file.close()  # Otherwise we can get segfaults
    for savedToDisk in savedToDiskPerDetector.values():
        assert savedToDisk is False


@pytest.mark.parametrize('dtype,mock_suffix', [
    (np.uint8, 'mock_uint8'),
    (np.uint16, 'mock_uint16')
])
def test_recording_dtype_preservation(qtbot, dtype, mock_suffix):
    """Test that HDF5 datasets preserve the frame dtype instead of hardcoded 'i2'.
    
    Regression test for issue where:
    - uint16 data >32767 wrapped to negative values (signed int16 corruption)
    - uint8 data was needlessly widened to int16
    """
    # Create detector with specific dtype through mock suffix
    detectorInfos = {
        'CAM': DetectorInfo(
            analogChannel=None,
            digitalLine=3,
            managerName='HamamatsuManager',
            managerProperties={
                'cameraListIndex': mock_suffix,
                'hamamatsu': {
                    'readout_speed': 3,
                    'trigger_global_exposure': 5,
                    'trigger_active': 2,
                    'trigger_polarity': 2,
                    'exposure_time': 0.01,
                    'trigger_source': 1,
                    'subarray_hpos': 0,
                    'subarray_vpos': 0,
                    'subarray_hsize': 512,
                    'subarray_vsize': 512,
                    'image_width': 512,
                    'image_height': 512
                }
            },
            forAcquisition=True
        )
    }

    filePerDetector, savedToDiskPerDetector = record(
        qtbot,
        detectorInfos,
        detectorNames=list(detectorInfos.keys()),
        recMode=RecMode.SpecFrames,
        savename=f'test_dtype_{dtype.__name__}',
        saveMode=SaveMode.RAM,
        attrs={detectorName: {
            'testAttr1': 2,
            'testAttr2': 'value'
        } for detectorName in detectorInfos.keys()},
        recFrames=5
    )

    assert filePerDetector.keys() == detectorInfos.keys()
    
    for detectorName, file in filePerDetector.items():
        h5pyFile = h5py.File(file)
        dataset = h5pyFile.get(detectorName)
        
        # Check dtype matches expected type
        assert dataset.dtype == dtype, \
            f"Expected dtype {dtype}, got {dataset.dtype}"
        
        # For uint16, verify no wrap-around corruption
        if dtype == np.uint16:
            # Verify we can represent values >32767 without wrapping
            assert dataset.dtype.kind == 'u', \
                "uint16 detector should produce unsigned dataset, not signed i2"
        
        # For uint8, verify no unnecessary widening
        if dtype == np.uint8:
            assert dataset.dtype.itemsize == 1, \
                f"uint8 detector should produce 1-byte dataset, got {dataset.dtype.itemsize} bytes"
        
        h5pyFile.close()
        file.close()


# Copyright (C) 2020-2021 ImSwitch developers
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
