"""Phase 2d contracts for metadata-only tiling sources and ImProcess UX."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import tifffile
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.tile_mosaic import (
    MANIFEST_NAME,
    PayloadProvenance,
    RefinementReport,
    SkippedPayload,
    inspect_dataset,
    manifest_fingerprint,
)
from imswitch.improcess.controller.DataFrameController import DataFrameController
from imswitch.improcess.controller.FileIOController import FileIOController
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController,
)
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.dataset_sources import (
    TIFF_SPEC,
    TILING_MANIFEST_SPEC,
    resolve_dataset_source,
)
from imswitch.improcess.model.result import ViewMode
from imswitch.improcess.reconstructors.base import SourceInspection
from imswitch.improcess.reconstructors.tiling.reconstructor import (
    TilingMosaicResult,
    TilingReconstructor,
    _TilingParamsWidget,
)


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _write_run(folder: Path, *, payload_bytes=b"metadata only") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    tiles = []
    for tile_id, column in enumerate((0, 5)):
        alignment = folder / f"alignment_{tile_id}.tiff"
        tifffile.imwrite(alignment, np.full((4, 5), tile_id + 1, np.uint16))
        payload = folder / f"payload_{tile_id}.tiff"
        payload.write_bytes(payload_bytes)
        tiles.append({
            "grid": [0, tile_id],
            "stage_um": [float(column), 0.0],
            "pixel_xy": [float(column), 0.0],
            "alignment": {
                "detector": "Camera",
                "filename": alignment.name,
                "axes": "YX",
                "stored_axes": "YX",
                "shape": [4, 5],
                "stored_shape": [4, 5],
            },
            "payloads": {
                "Camera": {
                    "path": payload.name,
                    "group": None,
                    "detector": "Camera",
                    "axes": "CZYX",
                    "stored_axes": "CZYX",
                    "shape": [2, 3, 4, 5],
                    "stored_shape": [2, 3, 4, 5],
                    "generation": tile_id,
                    "complete": True,
                    "transform_to_alignment": "identity",
                }
            },
        })
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps({
        "format": "imswitch-tiling/2",
        "pixel_size_um": {"y": 1.0, "x": 1.0},
        "z_step_um": 0.5,
        "orientation": {
            "flip_x": False,
            "flip_y": False,
            "swap_axes": False,
        },
        "tiles": tiles,
    }), encoding="utf-8")
    return manifest


def _metadata_obj(manifest: Path):
    index, completeness = inspect_dataset(manifest)
    data_obj = DataObj.fromMetadataSource(
        manifest.parent.name,
        manifest,
        "tiling-manifest",
        index,
    )
    data_obj.sourceSummary = completeness
    data_obj.sourceFingerprint = manifest_fingerprint(manifest)
    return data_obj


def test_tiling_source_resolves_run_manifest_and_owned_tile(tmp_path):
    manifest = _write_run(tmp_path / "run")
    tile = manifest.parent / "payload_0.tiff"
    allowed = [TILING_MANIFEST_SPEC, TIFF_SPEC]

    for selected in (manifest.parent, manifest, tile):
        source = resolve_dataset_source(selected, allowed_specs=allowed)
        assert source.format_id == "tiling-manifest"
        assert source.path == manifest

    # Ordinary image loading is unchanged when tiling is not accepted.
    assert resolve_dataset_source(tile).path == tile


def test_nested_tiling_ownership_requires_explicit_choice(tmp_path):
    outer = _write_run(tmp_path / "outer")
    inner = _write_run(outer.parent / "inner")
    artifact = inner.parent / "nested" / "tile.tiff"
    artifact.parent.mkdir()
    artifact.write_bytes(b"not opened")

    with pytest.raises(ValueError, match="multiple nested tiling runs"):
        resolve_dataset_source(artifact, allowed_specs=[TILING_MANIFEST_SPEC])
    assert resolve_dataset_source(
        inner.parent, allowed_specs=[TILING_MANIFEST_SPEC]
    ).path == inner


def test_metadata_data_obj_is_ready_without_an_array_source(tmp_path, monkeypatch):
    manifest = _write_run(tmp_path / "run")
    index, _summary = inspect_dataset(manifest)
    monkeypatch.setattr(
        DataObj,
        "_open",
        staticmethod(lambda *_args, **_kwargs: pytest.fail("image open attempted")),
    )

    data_obj = DataObj.fromMetadataSource(
        "run", manifest, "tiling-manifest", index
    )

    assert data_obj.sourceReady
    assert not data_obj.sourceLoaded
    assert not data_obj.dataLoaded
    assert data_obj.data is None
    assert data_obj.data_source is None
    assert data_obj.attrs == {}
    assert data_obj.numFrames is None


def test_file_io_routes_tiling_source_as_one_current_object(tmp_path):
    manifest = _write_run(tmp_path / "run")
    emitted = []
    raised = []
    controller = SimpleNamespace(
        _main=SimpleNamespace(_currentDataObj=None),
        _logger=SimpleNamespace(error=lambda *_args: None),
        _commChannel=SimpleNamespace(
            sigCurrentDataChanged=SimpleNamespace(emit=emitted.append)
        ),
        _widget=SimpleNamespace(raiseCurrentDataDock=lambda: raised.append(True)),
    )
    controller._loadMetadataAsCurrent = (
        FileIOController._loadMetadataAsCurrent.__get__(controller)
    )
    source = resolve_dataset_source(
        manifest.parent, allowed_specs=[TILING_MANIFEST_SPEC]
    )

    assert controller._loadMetadataAsCurrent(source) == "current"
    assert emitted == [controller._main._currentDataObj]
    assert raised == [True]
    assert controller._main._currentDataObj.sourceKind == "tiling-manifest"
    assert controller._main._currentDataObj.sourceMetadata.manifest == manifest
    assert not controller._main._currentDataObj.sourceLoaded


def test_metadata_source_clears_and_disables_image_panel(tmp_path):
    data_obj = _metadata_obj(_write_run(tmp_path / "run"))
    calls = SimpleNamespace(images=[], enabled=[], frames=[])
    controller = SimpleNamespace(
        _widget=SimpleNamespace(
            setImage=lambda image, autoLevels: calls.images.append(image),
            setImageControlsEnabled=calls.enabled.append,
            setNumFrames=calls.frames.append,
            setDataName=lambda _value: None,
            setDatasetName=lambda _value: None,
        ),
        _dataObj=None,
        _displayedImage=None,
        _commChannel=SimpleNamespace(
            sigDisplayedFrameChanged=SimpleNamespace(emit=lambda: None)
        ),
    )

    DataFrameController.currentDataChanged(controller, data_obj)

    assert calls.enabled == [False]
    assert calls.frames == [0]
    np.testing.assert_array_equal(calls.images[0], np.zeros((1, 1)))
    assert controller._displayedImage is None


def test_tiling_inspection_populates_choices_without_opening_payload(tmp_path):
    data_obj = _metadata_obj(_write_run(tmp_path / "run"))

    inspection = TilingReconstructor().inspect_source(data_obj)

    output = {choice.value: choice for choice in inspection.choices["output"]}
    camera = output["Camera"].metadata
    assert camera["axes"] == "CZYX"
    assert camera["shape"] == (2, 3, 4, 5)
    assert camera["channels"] == (0, 1)
    assert camera["has_z"]
    assert camera["complete"] == camera["total"] == 2
    assert camera["estimates"]["1:max"]["shape"] == (4, 10)
    assert "__alignment__" in output
    assert inspection.metadata["refinement_available"]


def test_tiling_widget_refreshes_selection_from_inspection(tmp_path, qapp):
    inspection = TilingReconstructor().inspect_source(
        _metadata_obj(_write_run(tmp_path / "run"))
    )
    widget = _TilingParamsWidget()

    widget.set_source_inspection(inspection)

    assert widget.detectorCombo.currentData() == "Camera"
    assert widget.channelCombo.count() == 3
    assert widget.projectCheck.isEnabled()
    assert "2/2 complete" in widget.sourceStatus.text()
    assert "Estimated output" in widget.sourceStatus.text()


def test_changed_manifest_is_rejected_before_reconstruction(tmp_path):
    manifest = _write_run(tmp_path / "run")
    data_obj = _metadata_obj(manifest)
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after it was opened"):
        TilingReconstructor._indexForProcess(data_obj, manifest)


def test_manager_selects_only_a_source_compatible_reconstructor(monkeypatch):
    delivered = []
    choices = []
    image_only = SimpleNamespace(
        id="image", name="Image", accepted_source_kinds=("image",)
    )
    tiling = SimpleNamespace(
        id="tiling",
        name="Tiling",
        accepted_source_kinds=("image", "tiling-manifest"),
        inspect_source=lambda _data: SourceInspection("tiling-manifest"),
    )
    registry = SimpleNamespace(reconstructors=lambda: [image_only, tiling])
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.registry.get_registry",
        lambda: registry,
    )
    controller = SimpleNamespace(
        _main=SimpleNamespace(
            _currentDataObj=SimpleNamespace(sourceKind="tiling-manifest"),
            _activeReconstructor=image_only,
        ),
        _widget=SimpleNamespace(
            parTree=SimpleNamespace(set_source_inspection=delivered.append),
            setReconstructorChoices=lambda value, current: choices.append(
                (value, current)
            ),
        ),
        _logger=SimpleNamespace(warning=lambda *_args: None, debug=lambda *_args: None),
        _install_reconstructor_params=lambda _candidate: None,
    )
    for method in (
        "_accepts_current_source",
        "_publishReconstructorChoices",
        "_inspect_current_source",
        "currentDataChanged",
    ):
        setattr(
            controller,
            method,
            getattr(ReconstructorManagerController, method).__get__(controller),
        )

    controller.currentDataChanged(controller._main._currentDataObj)

    assert controller._main._activeReconstructor is tiling
    assert choices[-1] == ([('tiling', 'Tiling')], 'tiling')
    assert delivered[-1].source_kind == "tiling-manifest"


def test_saved_ome_tiff_embeds_payload_provenance(tmp_path):
    provenance = PayloadProvenance(
        detector="Camera",
        channel=1,
        z_projection="max",
        skipped=(SkippedPayload(3, "incomplete"),),
        manifest=tmp_path / MANIFEST_NAME,
        layout_cache_key=("layout",),
        refinement_report=RefinementReport(tiles=4, moved=1),
        transform_source="manifest",
        transform={"kind": "identity"},
        placement_path="identity-integer",
    )
    result = TilingMosaicResult(
        name="mosaic",
        data=np.ones((4, 5), np.float32),
        axis_labels=["Y", "X"],
        axis_scales=[1.0, 1.0],
        scale_unit="µm",
        view_modes=[ViewMode("Standard", (0, 1))],
        provenance=provenance,
    )
    output = tmp_path / "mosaic.ome.tiff"

    result.save(output)

    with tifffile.TiffFile(output) as handle:
        ome = handle.ome_metadata
    assert "imswitch-tiling-mosaic-provenance/1" in ome
    assert '"detector": "Camera"' in ome
    assert '"tile_id": 3' in ome
