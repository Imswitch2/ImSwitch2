"""Tests for saving a tiling run as a re-stitchable OME dataset."""

import json
import re
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.algorithms.tile_mosaic import (
    inspect_dataset,
    read_manifest_payload,
)
from imswitch.imcontrol.model.workflows.spiral import SPIRAL
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


def test_payload_locator_distinguishes_free_running_from_failed_triggered():
    snapshot = "tile_Camera.ome.tiff"

    free_running = TilingController._payloadLocator(None, "Camera", snapshot)
    missing_triggered = TilingController._payloadLocator({}, "Camera", snapshot)
    finalized = TilingController._payloadLocator({
        "Camera": {
            "path": "payloads/tile.h5",
            "group": "scan0/Camera",
            "axes": "ZYX",
            "stored_axes": "TZYX",
            "shape": [3, 4, 5],
            "stored_shape": [1, 3, 4, 5],
            "generation": 17,
            "complete": True,
        }
    }, "Camera", snapshot)

    assert free_running == {
        "path": snapshot, "group": None, "complete": True
    }
    assert missing_triggered == {
        "path": snapshot, "group": None, "complete": False
    }
    assert finalized["path"] == "payloads/tile.h5"
    assert finalized["group"] == "scan0/Camera"
    assert finalized["complete"] is True


def test_stack_tile_saves_separate_2d_alignment_and_recording_payload(tmp_path):
    class _RecordingManager:
        def __init__(self):
            self.calls = []
            self.stored = {}

        def snapImagesPrev(
            self, images, savename, _save_format, _attrs, **_kwargs
        ):
            copied = {name: np.asarray(image).copy()
                      for name, image in images.items()}
            self.calls.append(("compatibility", copied))
            self.stored = {
                name: tuple(image.shape) for name, image in copied.items()
            }
            return [
                f"{savename}_{name}.ome.tiff" for name in copied
            ]

        def snapImagePrev(
            self, detector, savename, _save_format, image, _attrs, **_kwargs
        ):
            copied = np.asarray(image).copy()
            self.calls.append(("alignment", {detector: copied}))
            self.stored = {detector: tuple(copied.shape)}
            return [f"{savename}_{detector}.ome.tiff"]

        def lastSnapStoredShapes(self):
            return dict(self.stored)

    manager = _RecordingManager()
    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(recordingManager=manager)
    ctrl._setupInfo = SimpleNamespace(
        tiling=SimpleNamespace(saveFormat="TIFF")
    )
    ctrl._logger = _Logger()
    ctrl._orientation = (False, False, False)
    ctrl._saveSetTransforms = {}
    ctrl._stitcher = SimpleNamespace(
        placement=lambda *_args: (0, 0),
        nominal_placement=lambda *_args: (0, 0),
    )
    stack = np.arange(2 * 4 * 5, dtype=np.uint16).reshape(2, 4, 5)
    projection = stack.max(axis=0)
    dataset = TileDataset(pixel_size_um=(1.0, 1.0))
    recording_locator = {
        "Camera": {
            "path": "payloads/tile-0.h5",
            "group": "scan0/Camera",
            "axes": "CYX",
            "stored_axes": "TCYX",
            "shape": [2, 4, 5],
            "stored_shape": [1, 2, 4, 5],
            "generation": 23,
            "complete": True,
        }
    }

    ctrl._saveTile(
        {"Camera": stack},
        "Camera",
        0,
        0,
        (0.0, 0.0),
        dataset,
        tmp_path,
        payloads=recording_locator,
        alignmentImage=projection,
    )

    record = dataset.tiles[0]
    assert [kind for kind, _images in manager.calls] == [
        "compatibility", "alignment"
    ]
    np.testing.assert_array_equal(manager.calls[0][1]["Camera"], stack)
    np.testing.assert_array_equal(manager.calls[1][1]["Camera"], projection)
    assert record.filename == "tile_x+00_y+00_Camera.ome.tiff"
    assert record.alignment["filename"] == (
        "tile_x+00_y+00_alignment_Camera.ome.tiff"
    )
    assert record.alignment["axes"] == "YX"
    assert record.alignment["shape"] == (4, 5)
    assert record.payloads["Camera"]["path"] == "payloads/tile-0.h5"
    assert record.payloads["Camera"]["complete"] is True


def test_multidetector_zarr_snapshot_uses_shared_path_and_exact_groups(tmp_path):
    class _RecordingManager:
        def snapImagesPrev(
            self, images, savename, _save_format, _attrs, **_kwargs
        ):
            self.shapes = {
                name: (1, *np.asarray(image).shape)
                for name, image in images.items()
            }
            return [f"{savename}.zarr"]

        def lastSnapStoredShapes(self):
            return dict(self.shapes)

    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(recordingManager=_RecordingManager())
    ctrl._setupInfo = SimpleNamespace(
        tiling=SimpleNamespace(saveFormat="ZARR")
    )
    ctrl._logger = _Logger()
    ctrl._orientation = (False, False, False)
    ctrl._saveSetTransforms = {}
    ctrl._stitcher = SimpleNamespace(
        placement=lambda *_args: (0, 0),
        nominal_placement=lambda *_args: (0, 0),
    )
    dataset = TileDataset(pixel_size_um=(1.0, 1.0))
    images = {
        "APD1": np.ones((4, 5), np.uint16),
        "APD2": np.full((4, 5), 2, np.uint16),
    }

    ctrl._saveTile(
        images,
        "APD1",
        0,
        0,
        (0.0, 0.0),
        dataset,
        tmp_path,
        payloads=None,
        alignmentImage=images["APD1"],
    )

    payloads = dataset.tiles[0].payloads
    assert payloads["APD1"]["path"] == "tile_x+00_y+00.zarr"
    assert payloads["APD2"]["path"] == "tile_x+00_y+00.zarr"
    assert payloads["APD1"]["group"] == "APD1"
    assert payloads["APD2"]["group"] == "APD2"


# ----------------------------------------------------------------------
# Where datasets are written
# ----------------------------------------------------------------------


def _controllerWithRecFolder(folder, configured=''):
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._commChannel = SimpleNamespace(
        getRecordingFolder=lambda: folder
    )
    return ctrl, SimpleNamespace(measurementsRoot=configured)


def test_save_root_follows_the_recording_widget():
    """The operator sets the output folder in one place; tiling honours it."""
    ctrl, tilingInfo = _controllerWithRecFolder('/data/today')

    assert ctrl._saveRoot(tilingInfo) == '/data/today'


def test_configured_root_overrides_the_recording_folder():
    ctrl, tilingInfo = _controllerWithRecFolder('/data/today', '/explicit')

    assert ctrl._saveRoot(tilingInfo) == '/explicit'


def test_save_root_falls_back_when_no_recording_widget():
    """Must not crash a run just because Recording is not loaded."""
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._commChannel = SimpleNamespace()

    assert ctrl._saveRoot(SimpleNamespace(measurementsRoot='')) is None


def test_save_root_falls_back_when_the_folder_is_blank():
    ctrl, tilingInfo = _controllerWithRecFolder('')

    assert ctrl._saveRoot(tilingInfo) is None


def test_save_root_survives_a_raising_accessor():
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()

    def _boom():
        raise RuntimeError('widget gone')

    ctrl._commChannel = SimpleNamespace(getRecordingFolder=_boom)

    assert ctrl._saveRoot(SimpleNamespace(measurementsRoot='')) is None


def test_tiling_folder_is_one_subfolder_per_run(tmp_path):
    from imswitch.imcontrol.model.workflows.tile_dataset import tiling_folder

    folder = tiling_folder(tmp_path)

    assert folder.parent == tmp_path
    assert folder.name.startswith('tiling_')


def test_tiling_folder_without_a_base_uses_the_measurements_root():
    from imswitch.imcontrol.model.workflows.tile_dataset import tiling_folder
    from imswitch.imcontrol.model.workflows.paths import (
        resolve_measurements_root,
    )

    assert tiling_folder(None).parent == resolve_measurements_root(None)


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------


class _Stage:
    def __init__(self):
        self.position = {'X': 0.0, 'Y': 0.0}

    def move(self, value, axis):
        self.position[axis] += value


def _runSavingScan(
    tmp_path, monkeypatch, grid=(2, 2), step_um=64.0, tile=None,
    save_format='TIFF',
):
    """Run a scan with saving on, against the real RecordingManager storer."""
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager

    if tile is None:
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
        tiling=SimpleNamespace(saveFormat=save_format),
    )
    ctrl._commChannel = SimpleNamespace(getRecordingFolder=lambda: str(tmp_path))
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
        '.tiling_folder',
        lambda _root=None: tmp_path / 'run',
    )

    TilingController._runScan(
        ctrl,
        SimpleNamespace(xyPositioner='STAGE', camera='CAM',
                        saveFormat=save_format, measurementsRoot=''),
        n_tiles_x=grid[0], n_tiles_y=grid[1], pattern=SPIRAL,
        step_um=step_um,
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
    _ctrl, folder = _runSavingScan(tmp_path, monkeypatch, grid=(2, 2), step_um=64.0)

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
    assert payload['tile_shape_px'] == {'height': 32, 'width': 32, 'depth': 1}
    assert len(payload['tiles']) == 4
    # Every listed file must actually exist, or the dataset is not usable.
    for entry in payload['tiles']:
        assert (folder / entry['filename']).exists()
        # A genuinely 2-D detector needs no duplicate alignment file.
        assert entry['alignment']['filename'] == entry['filename']
        assert entry['alignment']['axes'] == 'YX'


def test_real_stack_save_keeps_compatibility_and_2d_alignment_artifacts(
    tmp_path, monkeypatch
):
    stack = np.arange(3 * 32 * 32, dtype=np.uint16).reshape(3, 32, 32)
    _ctrl, folder = _runSavingScan(
        tmp_path, monkeypatch, grid=(1, 1), tile=stack
    )
    manifest = json.loads((folder / MANIFEST_NAME).read_text())
    entry = manifest['tiles'][0]
    compatibility_path = folder / entry['filename']
    alignment_path = folder / entry['alignment']['filename']

    assert compatibility_path != alignment_path
    assert compatibility_path.exists()
    assert alignment_path.exists()
    assert entry['alignment']['axes'] == 'YX'
    assert entry['alignment']['shape'] == [32, 32]
    assert entry['payloads']['CAM']['path'] == entry['filename']
    assert entry['payloads']['CAM']['complete'] is True

    tifffile = pytest.importorskip('tifffile')
    np.testing.assert_array_equal(tifffile.imread(compatibility_path), stack)
    np.testing.assert_array_equal(
        tifffile.imread(alignment_path), stack.max(axis=0)
    )


@pytest.mark.parametrize('save_format', ['HDF5', 'ZARR'])
def test_structured_free_running_payload_names_its_detector_group(
    tmp_path, monkeypatch, save_format
):
    image = np.arange(32 * 32, dtype=np.uint16).reshape(32, 32)
    _ctrl, folder = _runSavingScan(
        tmp_path,
        monkeypatch,
        grid=(1, 1),
        tile=image,
        save_format=save_format,
    )

    index, _completeness = inspect_dataset(folder)
    payload = index.tiles[0].payloads['CAM']

    assert payload.group == 'CAM'
    assert payload.path.exists()
    np.testing.assert_array_equal(read_manifest_payload(payload), image)


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
    ctrl._commChannel = SimpleNamespace(getRecordingFolder=lambda: str(tmp_path))
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
        '.tiling_folder',
        lambda _root=None: tmp_path / 'run',
    )

    TilingController._runScan(
        ctrl, SimpleNamespace(xyPositioner='STAGE', camera='CAM'),
        n_tiles_x=2, n_tiles_y=2, pattern=SPIRAL, step_um=8.0,
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
