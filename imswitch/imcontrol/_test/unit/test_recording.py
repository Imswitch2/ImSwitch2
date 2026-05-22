import os
import pytest

import h5py
import numpy as np

from imswitch.imcontrol.model import DetectorsManager, RecordingManager, RecMode, SaveMode, SaveFormat, DetectorInfo
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


def test_snap_hdf5_structured_layout(tmp_path):
    """Test that HDF5 snapshots use the structured layout with detector groups and metadata."""
    from imswitch.imcontrol.model import DetectorsManager, RecordingManager, SaveMode, SaveFormat
    
    # Create a simple detector setup
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Create test image and attrs
    detectorName = list(detectorInfosBasic.keys())[0]
    test_image = np.random.randint(0, 1000, (10, 128, 128), dtype=np.uint16)
    
    # Create attrs with categorized keys
    test_attrs = {
        detectorName: {
            'detector:exposure': 0.01,
            'detector:gain': 1.5,
            'lasers:laser1:power': 50,
            'lasers:laser1:wavelength': 488,
            'scan:size_um': 100.0,
            'uncategorized_key': 'value'
        }
    }
    
    # Snap the image
    savepath = str(tmp_path / 'test_snap')
    recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.HDF5, test_image, test_attrs)
    
    # Verify structured layout
    snap_file = f'{savepath}_{detectorName}.h5'
    assert os.path.exists(snap_file), f"Snapshot file not created: {snap_file}"
    
    with h5py.File(snap_file, 'r') as f:
        # Check file-level attrs
        assert 'timestamp' in f.attrs
        assert 'rec_mode' in f.attrs
        assert f.attrs['rec_mode'] == 'snap'
        
        # Check detector group exists
        assert detectorName in f, f"Detector group '{detectorName}' not found"
        det_group = f[detectorName]
        
        # Check data dataset
        assert 'data' in det_group, "data dataset not found in detector group"
        dataset = det_group['data']
        
        # Check dataset attrs
        assert 'detector_name' in dataset.attrs
        assert 'element_size_um' in dataset.attrs
        assert dataset.attrs['detector_name'] == detectorName
        
        # Check dtype preservation
        assert dataset.dtype == np.uint16, f"Expected uint16, got {dataset.dtype}"
        
        # Check shape (should be T, Y, X)
        assert dataset.shape == test_image.shape, f"Shape mismatch: {dataset.shape} != {test_image.shape}"
        
        # Check data content
        np.testing.assert_array_equal(dataset[:], test_image)
        
        # Check compression
        assert dataset.compression is not None, "Dataset should be compressed"
        assert dataset.compression == 'lzf', f"Expected lzf compression, got {dataset.compression}"
        assert dataset.shuffle, "Shuffle filter should be enabled"
        
        # Check metadata group structure
        assert 'metadata' in det_group, "metadata group not found"
        meta_group = det_group['metadata']
        
        # Check category subgroups
        assert 'detector' in meta_group, "detector category not found in metadata"
        assert 'lasers' in meta_group, "lasers category not found in metadata"
        assert 'scan' in meta_group, "scan category not found in metadata"
        
        # Check detector category attrs
        det_meta = meta_group['detector']
        assert 'exposure' in det_meta.attrs
        assert 'gain' in det_meta.attrs
        assert det_meta.attrs['exposure'] == 0.01
        assert det_meta.attrs['gain'] == 1.5
        
        # Check lasers category attrs
        laser_meta = meta_group['lasers']
        assert 'laser1:power' in laser_meta.attrs
        assert 'laser1:wavelength' in laser_meta.attrs
        assert laser_meta.attrs['laser1:power'] == 50
        assert laser_meta.attrs['laser1:wavelength'] == 488
        
        # Check scan category attrs
        scan_meta = meta_group['scan']
        assert 'size_um' in scan_meta.attrs
        assert scan_meta.attrs['size_um'] == 100.0
        
        # Check uncategorized attrs (flat in metadata group)
        assert 'uncategorized_key' in meta_group.attrs
        assert meta_group.attrs['uncategorized_key'] == 'value'


def test_snap_axis_ordering():
    """Test that snapshots use (T, Y, X) axis convention without reversal."""
    from imswitch.imcontrol.model import DetectorsManager, RecordingManager
    
    detectorsManager = DetectorsManager(detectorInfosBasic, updatePeriod=100)
    recordingManager = RecordingManager(detectorsManager)
    
    # Create test image with known pattern to detect axis swaps
    # Shape: (3, 100, 50) = (T, Y, X)
    test_image = np.zeros((3, 100, 50), dtype=np.uint16)
    test_image[0, :, 0] = 1  # Mark first frame, first column
    test_image[1, 0, :] = 2  # Mark second frame, first row
    test_image[2, :, :] = 3  # Mark third frame, all pixels
    
    detectorName = list(detectorInfosBasic.keys())[0]
    test_attrs = {detectorName: {'test': 'value'}}
    
    # Use a unique temporary filename
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        savepath = os.path.join(tmpdir, 'test_axis')
        recordingManager.snapImagePrev(detectorName, savepath, SaveFormat.HDF5, test_image, test_attrs)
        
        # Read back and verify no axis transposition
        snap_file = f'{savepath}_{detectorName}.h5'
        with h5py.File(snap_file, 'r') as f:
            dataset = f[detectorName]['data']
            saved_data = dataset[:]
            
            # Should match exactly (T, Y, X) with no reversal
            assert saved_data.shape == test_image.shape
            np.testing.assert_array_equal(saved_data, test_image)
            
            # Verify the pattern markers are in correct positions
            assert saved_data[0, :, 0].sum() == 100  # First column of frame 0
            assert saved_data[1, 0, :].sum() == 100  # First row of frame 1
            assert saved_data[2, :, :].sum() == 3 * 100 * 50  # All of frame 2


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
