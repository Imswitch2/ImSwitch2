"""BeadRec saves OME-TIFF with its pixel size, and keeps it through a reload.

It used to write ``tifffile.imwrite(path, image)``: no pixel size, no
provenance -- the one fact a bead fit needs, dropped at the save.
"""
from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

from imswitch.imcommon.algorithms.bead_recognition import BeadRecResultRecord
from imswitch.imcontrol.controller.display_transform import DisplayTransform
from imswitch.imcontrol.model.bead_rec_io import (
    oriented_pixel_size, read_pixel_size_um, write_reconstruction_tiff,
)


def test_a_reconstruction_round_trips_its_pixel_size_and_provenance(tmp_path):
    path = str(tmp_path / 'bead.tiff')
    image = np.arange(12, dtype=np.float64).reshape(3, 4)

    write_reconstruction_tiff(path, image, (0.05, 0.02), {'BeadRec:name': 'run 1', 'BeadRec:scaled': False})

    with tifffile.TiffFile(path) as tiff:
        assert tiff.is_ome
        xml = tiff.ome_metadata
        np.testing.assert_array_equal(tiff.asarray(), image)
    assert 'PhysicalSizeY="0.05"' in xml and 'PhysicalSizeX="0.02"' in xml
    assert 'BeadRec:name' in xml and 'run 1' in xml
    assert read_pixel_size_um(path) == (0.05, 0.02)


def test_an_unknown_pixel_size_is_omitted_not_invented(tmp_path):
    path = str(tmp_path / 'unknown.tiff')
    write_reconstruction_tiff(path, np.ones((3, 4)), None)
    with tifffile.TiffFile(path) as tiff:
        assert 'PhysicalSizeX' not in (tiff.ome_metadata or '')
    assert read_pixel_size_um(path) is None


def test_a_plain_tiff_reads_back_no_pixel_size(tmp_path):
    path = str(tmp_path / 'plain.tif')
    tifffile.imwrite(path, np.ones((3, 4)))
    assert read_pixel_size_um(path) is None


def test_only_2d_reconstructions_are_written(tmp_path):
    with pytest.raises(ValueError, match='2-D'):
        write_reconstruction_tiff(str(tmp_path / 'x.tiff'), np.ones((2, 3, 4)), (1, 1))


@pytest.mark.parametrize('rotation, expected', [
    (0, (0.05, 0.02)), (90, (0.02, 0.05)), (180, (0.05, 0.02)), (270, (0.02, 0.05)),
])
def test_a_quarter_turn_swaps_the_pixel_size(rotation, expected):
    assert oriented_pixel_size((0.05, 0.02), rotation) == expected


# ----------------------------------------------------------------------
# The controller: what is shown carries its pixel size to the save
# ----------------------------------------------------------------------


def _controller(tmp_path, *, rotation=0, scaled=False):
    from imswitch.imcontrol.controller.controllers.BeadRecController import (
        BeadRecController,
    )

    shown = []
    stub = SimpleNamespace(
        stepSizes=(0.02, 0.05),                    # (x, y), as the scan reports it
        dims=(4, 3), framesPerPixel=1, axialName='XY',
        _orientation=DisplayTransform(rotation, False, False),
        _widget=SimpleNamespace(
            updateImage=shown.append,
            scaleButton=SimpleNamespace(isChecked=lambda: scaled),
            getOrientation=lambda: (rotation, False, False),
            isSelectedCurrent=lambda: True,
        ),
        _logger=SimpleNamespace(warning=lambda *_a, **_k: None),
        lastDir=str(tmp_path),
    )
    for name in ('_reconstructionPixelSizeUm', '_showReconstruction',
                 '_applyOrientation', '_displayAnnotations', '_toTransformArgs',
                 '_recordAnnotations', 'saveRec', 'saveAll', 'rescale'):
        attr = BeadRecController.__dict__[name]
        setattr(stub, name, attr.__func__ if isinstance(attr, staticmethod)
                else attr.__get__(stub))
    return stub, shown


def _annotations(path):
    import json
    import xml.etree.ElementTree as ET

    with tifffile.TiffFile(path) as tiff:
        root = ET.fromstring(tiff.ome_metadata)
    values = {}
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] == 'M':
            try:
                values[element.get('K')] = json.loads(element.text)
            except (TypeError, ValueError):
                values[element.get('K')] = element.text
    return values


def test_the_reconstructions_pixel_is_one_scan_step_or_the_finer_step_when_rescaled(tmp_path):
    stub, _ = _controller(tmp_path)
    assert stub._reconstructionPixelSizeUm(False) == (0.05, 0.02)      # (y, x)
    assert stub._reconstructionPixelSizeUm(True) == (0.02, 0.02)
    stub.stepSizes = None
    assert stub._reconstructionPixelSizeUm(False) is None


def test_save_writes_the_shown_image_with_its_rotated_pixel_size(tmp_path, monkeypatch):
    from imswitch.imcontrol.controller.controllers import BeadRecController as module

    stub, _ = _controller(tmp_path, rotation=90)
    base = np.arange(12, dtype=np.float64).reshape(3, 4)
    stub._showReconstruction(base, stub._reconstructionPixelSizeUm(False))
    assert stub.imDisplay.shape == (4, 3)                       # rotated on screen
    stub._widget.isSelectedCurrent = lambda: True
    path = str(tmp_path / 'shown.tiff')
    monkeypatch.setattr(module.guitools, 'askForFilePath', lambda *_a, **_k: path)

    stub.saveRec()

    assert read_pixel_size_um(path) == (0.02, 0.05)             # swapped with the image
    with tifffile.TiffFile(path) as tiff:
        np.testing.assert_array_equal(tiff.asarray(), stub.imDisplay)
    annotations = _annotations(path)
    assert annotations['BeadRec:orientation']['rotation'] == 90
    assert annotations['BeadRec:scan_step_um'] == [0.02, 0.05]


def test_save_all_writes_each_record_with_its_own_pixel_size(tmp_path, monkeypatch):
    from imswitch.imcontrol.controller.controllers import BeadRecController as module

    stub, _ = _controller(tmp_path)
    stub.resultRecords = [
        BeadRecResultRecord(name='a', image=np.ones((3, 4)), pixel_size_um=(0.05, 0.02)),
        BeadRecResultRecord(name='b', image=np.ones((2, 2)), pixel_size_um=None,
                            source_path='/data/b.tif'),
    ]
    names = ['a', 'b']
    stub._widget.getInsertIndexAfterCurrent = lambda: 0
    stub._widget.imageListWidget = SimpleNamespace(
        item=lambda index: SimpleNamespace(text=lambda: names[index]))
    monkeypatch.setattr(module.guitools, 'askForFolderPath', lambda *_a, **_k: str(tmp_path))

    stub.saveAll()

    assert read_pixel_size_um(str(tmp_path / 'a.tif')) == (0.05, 0.02)
    assert read_pixel_size_um(str(tmp_path / 'b.tif')) is None
    assert _annotations(str(tmp_path / 'b.tif'))['BeadRec:source_path'] == '/data/b.tif'
