"""Tests for saving a tiling run as a re-stitchable OME dataset."""

import json
import re
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.model.managers.RecordingManager import SaveFormat
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SNAP,
    OmeAxis,
    OmeImageMeta,
    build_ome_image_meta,
    build_ome_xml,
)
from imswitch.imcontrol.model.workflows.tile_dataset import (
    MANIFEST_NAME,
    TILE_CONFIG_NAME,
    TileDataset,
    TileRecord,
)


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


# ----------------------------------------------------------------------
# OME stage position
# ----------------------------------------------------------------------


def _meta(position=None):
    return OmeImageMeta(
        name='CAM',
        axes=[OmeAxis('y', 'space', 'µm'), OmeAxis('x', 'space', 'µm')],
        scale=[0.65, 0.65],
        dtype=np.dtype(np.uint16),
        stage_position_um=position,
    )


def test_tiff_metadata_carries_the_stage_position():
    md = _meta((1234.5, 678.9, 0.0)).tiff_metadata()

    assert md['Plane']['PositionX'] == [1234.5]
    assert md['Plane']['PositionY'] == [678.9]
    assert md['Plane']['PositionXUnit'] == ['µm']


def test_tiff_metadata_is_unchanged_without_a_position():
    """Every non-tiled recording must serialize exactly as it did before."""
    assert 'Plane' not in _meta().tiff_metadata()


def test_ome_xml_round_trips_the_stage_position():
    xml = build_ome_xml(_meta((100.0, 200.0, 0.0)), (4, 4))

    plane = re.search(r'<Plane[^>]*/?>', xml)
    assert plane is not None
    assert 'PositionX="100.0"' in plane.group(0)
    assert 'PositionY="200.0"' in plane.group(0)


def test_ngff_metadata_uses_a_translation_transform():
    """OME-NGFF places a tile with a translation, in the axes' own order."""
    ome = _meta((10.0, 20.0, 0.0)).ngff_ome_metadata(path='data')

    transforms = ome['multiscales'][0]['datasets'][0]['coordinateTransformations']
    assert transforms[0]['type'] == 'scale'
    assert transforms[1] == {'type': 'translation', 'translation': [20.0, 10.0]}


def test_ngff_metadata_omits_translation_without_a_position():
    ome = _meta().ngff_ome_metadata(path='data')
    transforms = ome['multiscales'][0]['datasets'][0]['coordinateTransformations']

    assert [t['type'] for t in transforms] == ['scale']


def test_position_survives_axis_padding():
    padded = _meta((5.0, 6.0, 0.0)).padded_to(3)
    assert padded.stage_position_um == (5.0, 6.0, 0.0)


def test_builder_accepts_a_stage_position():
    meta = build_ome_image_meta(
        'CAM', MODE_SNAP, 1, pixel_size_yx_um=(0.5, 0.5),
        stage_position_um=(1.0, 2.0, 3.0),
    )
    assert meta.stage_position_um == (1.0, 2.0, 3.0)


# ----------------------------------------------------------------------
# Sidecars
# ----------------------------------------------------------------------


def _dataset():
    ds = TileDataset(
        pixel_size_um=(0.65, 0.65), step_um=100.0,
        tile_shape_px=(512, 512), orientation=(True, False, False),
    )
    ds.add(TileRecord('tile_x+00_y+00_CAM.ome.tiff', (0, 0), (10.0, 20.0), (0.0, 0.0)))
    ds.add(TileRecord('tile_x+01_y+00_CAM.ome.tiff', (1, 0), (110.0, 20.0),
                      (154.0, -3.0), correction_px=(-3.0, 0.0)))
    return ds


def test_tile_configuration_is_written_in_fiji_format(tmp_path):
    _dataset().write(tmp_path)
    text = (tmp_path / TILE_CONFIG_NAME).read_text()

    assert 'dim = 2' in text
    # Fiji wants "filename; ; (x, y)" with x = column.
    assert 'tile_x+00_y+00_CAM.ome.tiff; ; (0.000, 0.000)' in text
    assert 'tile_x+01_y+00_CAM.ome.tiff; ; (154.000, -3.000)' in text


def test_manifest_records_geometry_and_corrections(tmp_path):
    _dataset().write(tmp_path)
    payload = json.loads((tmp_path / MANIFEST_NAME).read_text())

    assert payload['tile_step_um'] == 100.0
    assert payload['pixel_size_um'] == {'y': 0.65, 'x': 0.65}
    assert payload['orientation']['flip_x'] is True
    assert len(payload['tiles']) == 2
    assert payload['tiles'][1]['grid'] == [1, 0]
    assert payload['tiles'][1]['stage_um'] == [110.0, 20.0]
    assert payload['tiles'][1]['correction_px'] == [-3.0, 0.0]


def test_write_creates_the_folder(tmp_path):
    target = tmp_path / 'nested' / 'run'
    written = _dataset().write(target)

    assert target.is_dir()
    assert {p.name for p in written} == {TILE_CONFIG_NAME, MANIFEST_NAME}


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------


class _Stage:
    def __init__(self):
        self.position = {'X': 0.0, 'Y': 0.0}

    def move(self, value, axis):
        self.position[axis] += value


def _runSavingScan(tmp_path, monkeypatch, n_tiles=4, step_um=64.0):
    """Run a scan with saving on, against the real RecordingManager storer."""
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager

    tile = np.arange(32 * 32, dtype=np.uint16).reshape(32, 32)

    class _Detector:
        name = 'CAM'
        pixelSizeUm = [1.0, 1.0, 1.0]
        binning = 1
        dtype = np.dtype(np.uint16)
        parameters = {}

        def getLatestFrameShared(self, is_save=False):
            return tile

    detector = _Detector()

    class _Detectors:
        def acquire(self, names, purpose):
            return 'lease-1'

        def release(self, handle):
            pass

        def __getitem__(self, name):
            return detector

    recordingManager = RecordingManager(_Detectors())

    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': _Stage()},
        detectorsManager=_Detectors(),
        recordingManager=recordingManager,
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
        tiling=SimpleNamespace(saveFormat='TIFF'),
    )
    ctrl._stopRequested = False
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._scanAcqHandle = None
    ctrl._scanning = True
    ctrl._scanThread = object()
    ctrl._closed = True
    ctrl._registrationReport = None
    ctrl._orientation = (False, False, False)
    ctrl._lastSaveFolder = None
    ctrl._logger = _Logger()

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _s: None,
    )
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController'
        '.default_tiling_folder',
        lambda _root=None: tmp_path / 'run',
    )

    TilingController._runScan(
        ctrl,
        SimpleNamespace(xyPositioner='STAGE', camera='CAM',
                        saveFormat='TIFF', measurementsRoot=''),
        n_tiles=n_tiles, step_um=step_um,
        blend_overlaps=False, intensity_correction=False,
        settle_s=0.0, register_tiles=False,
        orientation=(False, False, False), save_tiles=True,
    )
    return ctrl, tmp_path / 'run'


def test_saving_writes_tiles_mosaic_and_sidecars(tmp_path, monkeypatch):
    pytest.importorskip('tifffile')
    _ctrl, folder = _runSavingScan(tmp_path, monkeypatch)

    names = sorted(p.name for p in folder.iterdir())
    assert TILE_CONFIG_NAME in names
    assert MANIFEST_NAME in names
    assert any(n.startswith('mosaic') for n in names)
    assert sum(1 for n in names if n.startswith('tile_')) == 4


def test_saved_tiles_carry_their_stage_position(tmp_path, monkeypatch):
    """The whole point: each file knows where on the sample it came from."""
    tifffile = pytest.importorskip('tifffile')
    _ctrl, folder = _runSavingScan(tmp_path, monkeypatch, n_tiles=4, step_um=64.0)

    positions = {}
    for path in sorted(folder.glob('tile_*')):
        xml = tifffile.tiffcomment(str(path))
        plane = re.search(r'<Plane[^>]*/?>', xml).group(0)
        x = float(re.search(r'PositionX="([-\d.eE+]+)"', plane).group(1))
        y = float(re.search(r'PositionY="([-\d.eE+]+)"', plane).group(1))
        positions[path.name] = (x, y)

    assert len(positions) == 4
    # The spiral visits (0,0), (1,0), (1,1), (0,1) at a 64 µm step.
    assert positions['tile_x+00_y+00_CAM.ome.tiff'] == (0.0, 0.0)
    assert positions['tile_x+01_y+00_CAM.ome.tiff'] == (64.0, 0.0)
    assert positions['tile_x+01_y+01_CAM.ome.tiff'] == (64.0, 64.0)
    assert positions['tile_x+00_y+01_CAM.ome.tiff'] == (0.0, 64.0)


def test_saved_dataset_manifest_matches_the_run(tmp_path, monkeypatch):
    pytest.importorskip('tifffile')
    _ctrl, folder = _runSavingScan(tmp_path, monkeypatch)

    payload = json.loads((folder / MANIFEST_NAME).read_text())
    assert payload['completed'] is True
    assert payload['tile_step_um'] == 64.0
    assert payload['tile_shape_px'] == {'height': 32, 'width': 32}
    assert len(payload['tiles']) == 4
    # Every listed file must actually exist, or the dataset is not usable.
    for entry in payload['tiles']:
        assert (folder / entry['filename']).exists()


def test_nothing_is_written_when_saving_is_off(tmp_path, monkeypatch):
    pytest.importorskip('tifffile')

    from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager

    class _Detector:
        name = 'CAM'
        pixelSizeUm = [1.0, 1.0, 1.0]
        binning = 1
        dtype = np.dtype(np.uint16)
        parameters = {}

        def getLatestFrameShared(self, is_save=False):
            return np.ones((16, 16), dtype=np.uint16)

    class _Detectors:
        def acquire(self, names, purpose):
            return 'lease-1'

        def release(self, handle):
            pass

        def __getitem__(self, name):
            return _Detector()

    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': _Stage()},
        detectorsManager=_Detectors(),
        recordingManager=RecordingManager(_Detectors()),
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
        tiling=SimpleNamespace(saveFormat='TIFF'),
    )
    for attr, value in (
        ('_stopRequested', False), ('_stitcher', None), ('_originXY', None),
        ('_gridPositions', []), ('_scanAcqHandle', None), ('_scanning', True),
        ('_scanThread', object()), ('_closed', True),
        ('_registrationReport', None), ('_orientation', (False, False, False)),
        ('_lastSaveFolder', None), ('_logger', _Logger()),
    ):
        setattr(ctrl, attr, value)

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _s: None,
    )
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController'
        '.default_tiling_folder',
        lambda _root=None: tmp_path / 'run',
    )

    TilingController._runScan(
        ctrl, SimpleNamespace(xyPositioner='STAGE', camera='CAM'),
        n_tiles=4, step_um=8.0,
        blend_overlaps=False, intensity_correction=False,
        settle_s=0.0, register_tiles=False,
        orientation=(False, False, False), save_tiles=False,
    )

    assert not (tmp_path / 'run').exists()


def test_save_format_falls_back_for_an_unknown_name():
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._setupInfo = SimpleNamespace(tiling=SimpleNamespace(saveFormat='JPEG'))

    assert ctrl._saveFormat is SaveFormat.TIFF


def test_save_extension_tracks_the_format():
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._setupInfo = SimpleNamespace(tiling=SimpleNamespace(saveFormat='ZARR'))

    assert ctrl._saveFormat is SaveFormat.ZARR
    assert ctrl._saveExtension == '.zarr'
