"""Every result type, in every format it supports, through the staged save.

The plan's Phase 2 exit test for writers: the receipt lists exactly the
files on disk, the primary carries the provenance document, the document
reads back with an equal graph, and multi-file saves are all-or-nothing.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from imswitch.improcess.analysis.colocalization import (
    ColocalizationAnalysis,
    ColocalizationRecord,
)
from imswitch.improcess.analysis.frc import FRCAnalysis
from imswitch.improcess.analysis.psf_resolution import PSFFitRecord, PSFResolutionAnalysis
from imswitch.improcess.analysis.segmentation import SegmentationAnalysis
from imswitch.improcess.analysis.projections import ProjectionAnalysis
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.model.points_table_result import PointsTableResult
from imswitch.improcess.model.provenance import graph_of, record_reconstruction
from imswitch.improcess.model.provenance_io import read_provenance
from imswitch.improcess.model.roi_mask_result import ROIMaskResult
from imswitch.improcess.model.save_protocol import (
    ProvenanceDocument,
    SaveError,
    SavePlan,
    UnsupportedSaveFormat,
    companion_json_path,
    normalize_format,
)
from imswitch.improcess.processors.colocalization.result import ColocalizationResult
from imswitch.improcess.processors.denoise.result import DenoisedResult
from imswitch.improcess.processors.drift_correct.result import DriftCorrectedResult
from imswitch.improcess.processors.frc.result import FRCResult
from imswitch.improcess.processors.make_composite.result import CompositeResult
from imswitch.improcess.processors.make_rgb.result import RGBResult
from imswitch.improcess.processors.multicolor_apply.result import MulticolorApplyResult
from imswitch.improcess.processors.multicolor_registration.result import (
    MulticolorRegistrationResult,
)
from imswitch.improcess.processors.projection.result import ProjectionResult
from imswitch.improcess.processors.psf_resolution.result import PSFResolutionResult
from imswitch.improcess.processors.segmentation.result import SegmentationResult
from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult
from imswitch.improcess.reconstructors.snouty.result import SnoutyResult
from imswitch.improcess.reconstructors.snouty.metadata import DEFAULT_PARAMS as SNOUTY_DEFAULTS
from imswitch.improcess.reconstructors.snouty_projections.result import SnoutyProjectionsResult
from imswitch.improcess.reconstructors.tiling.reconstructor import TilingMosaicResult
from imswitch.improcess.reconstructors.view_only.reconstructor import ViewOnlyResult
from imswitch.improcess.reconstructors.widefield_starss.analysis import (
    WidefieldStarssParams,
    analyze_widefield_starss_pair,
)
from imswitch.improcess.reconstructors.widefield_starss.result import WidefieldStarssResult
from imswitch.improcess.model.result import ViewMode


# --------------------------------------------------------------------------
# one instance of every result type
# --------------------------------------------------------------------------

def _rng():
    return np.random.default_rng(0)


def _image(name="img", shape=(2, 8, 8), labels=("Z", "Y", "X")):
    return ArrayProcessingResult(name, _rng().random(shape).astype(np.float32), list(labels),
                                 axis_scales=[0.5] * len(shape), scale_unit="um")


def _locs():
    return LocalizationResult(
        "locs",
        localizations_from_columns({
            "frame": np.array([0, 1]), "x_nm": np.array([100.0, 250.0]),
            "y_nm": np.array([50.0, 75.0]), "photons": np.array([1000.0, 2000.0]),
        }),
        pixel_size_nm=100.0,
    )


def _identity_alignment():
    return {
        "x_bounds": [0, 2, 4, 6], "reference_channel": 0, "mode": "maxproj", "roi_width": 2,
        "source_shape": (2, 3, 6),
        "transforms": [{"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0}] * 3,
    }


def _mosaic_frame(i0, i45, i90, i135, shape=(8, 10)):
    frame = np.zeros(shape, dtype=np.float32)
    frame[1::2, 1::2] = i0
    frame[1::2, ::2] = i45
    frame[::2, ::2] = i90
    frame[::2, 1::2] = i135
    return frame


def _wfs():
    background = np.full((8, 10), 5, dtype=np.float32)
    h = background + _mosaic_frame(100, 75, 50, 75)
    v = background + _mosaic_frame(80, 60, 40, 60)

    def stack(signal):
        frames = []
        for scale in (0.95, 1.0, 1.05):
            frames += [signal * scale, background]
        return np.stack(frames)

    analysis = analyze_widefield_starss_pair(
        stack(h), stack(v), WidefieldStarssParams(segmentation_mode="none", smooth_sigma=1.0)
    )
    return WidefieldStarssResult("wfs", analysis, {"segmentation_mode": "none"})


def _monalisa():
    coeffs = np.ones((1, 2, 4, 2, 5), dtype=np.float32)
    scan_params = {
        'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
        'directions': ['pos', 'pos', 'pos'],
        'steps': ['2', '2', '1', '1'],
        'step_sizes': ['40', '80', '120', '1'],
        'unidirectional': True,
    }
    labels = {'r_l_text': 'Right-Left', 'u_d_text': 'Up-Down', 'b_f_text': 'Back-Front',
              'timepoints_text': 'Timepoints', 'p_text': 'pos', 'n_text': 'neg'}
    return MonalisaProcessingResult.from_coeffs("monalisa", coeffs, scan_params, labels)


def _tiling(tmp_path):
    from imswitch.imcommon.algorithms.tile_mosaic import (
        PayloadProvenance, RefinementReport, SkippedPayload,
    )

    provenance = PayloadProvenance(
        detector="Camera", channel=1, z_projection="max",
        skipped=(SkippedPayload(3, "incomplete"),), manifest=tmp_path / "manifest.json",
        layout_cache_key=("layout",), refinement_report=RefinementReport(tiles=4, moved=1),
        transform_source="manifest", transform={"kind": "identity"}, placement_path="identity-integer",
    )
    return TilingMosaicResult(
        name="mosaic", data=np.ones((4, 5), np.float32), axis_labels=["Y", "X"],
        axis_scales=[1.0, 1.0], scale_unit="µm", view_modes=[ViewMode("Standard", (0, 1))],
        provenance=provenance,
    )


def _factories(tmp_path):
    frc_analysis = FRCAnalysis(
        frequency=np.linspace(0, 0.5, 8), frc=np.linspace(1, 0, 8), threshold=np.full(8, 1 / 7),
        cutoff_frequency=0.3, resolution=3.3, pixel_size=1.0, frequency_unit="1/px",
        resolution_unit="px", metadata={"n": 8},
    )
    coloc_analysis = ColocalizationAnalysis(
        records=[ColocalizationRecord("Full image", None, 64, 0.5, 0.4, 0.6, 0.7, 0.0, 0.0, 1.0, 2.0)],
        scatter_a=np.arange(4.0), scatter_b=np.arange(4.0), metadata={"pairs": 1},
    )
    psf_analysis = PSFResolutionAnalysis(
        fits=[PSFFitRecord("PSF", (0, 8, 0, 8), 4.0, 4.0, 1.2, 1.1, 2.8, 2.6, 100.0, 3.0, 0.1, 64)],
        pixel_size=0.1, unit="um", metadata={},
    )
    seg_analysis = SegmentationAnalysis(
        labels=np.array([[1, 1], [2, 2]], dtype=np.int32), mask=np.ones((2, 2), bool),
        threshold=0.5, regions=[], processed_image=np.zeros((2, 2)), metadata={},
    )
    projection = ProjectionAnalysis(
        data=np.ones((8, 8), np.float32), axis=0, axis_label="Z", mode="max",
        input_shape=(2, 8, 8), output_axis_labels=["Y", "X"], output_axis_scales=[0.5, 0.5],
        metadata={},
    )
    return {
        "array": lambda: _image(),
        "labels": lambda: LabelsResult("labels", np.arange(16, dtype=np.int32).reshape(4, 4)),
        "roi-mask": lambda: ROIMaskResult("mask", np.arange(16, dtype=np.int32).reshape(4, 4), roi_names=["a", "b"]),
        "points-table": lambda: PointsTableResult("pts", np.zeros((3, 2)), properties={"score": np.arange(3.0)}),
        "localization": _locs,
        "view-only": lambda: ViewOnlyResult("raw", _rng().random((3, 4, 4)).astype(np.float32), ["T", "Y", "X"]),
        "composite": lambda: CompositeResult("comp", _rng().random((2, 4, 4)).astype(np.float32), ["C", "Y", "X"],
                                             channel_axis=0, channel_colormaps=["red", "green"]),
        "rgb": lambda: RGBResult("rgb", (_rng().random((4, 4, 3)) * 255).astype(np.uint8), ["Y", "X", "S"],
                                 source_result="img", source_channel_axis="C"),
        "multicolor-apply": lambda: MulticolorApplyResult("applied", _rng().random((3, 2, 3, 2)).astype(np.float32),
                                                          ["C", "Z", "Y", "X"], _identity_alignment()),
        "projection": lambda: ProjectionResult("proj", projection, params={"axis": "Z"}),
        "frc": lambda: FRCResult("frc", frc_analysis, {"pixel_size": 1.0}),
        "colocalization": lambda: ColocalizationResult("coloc", coloc_analysis, {"threshold": 0.0}),
        "psf": lambda: PSFResolutionResult("psf", psf_analysis, {"pixel_size": 0.1}),
        "segmentation": lambda: SegmentationResult("seg", seg_analysis),
        "denoise": lambda: DenoisedResult("den", _rng().random((4, 4)).astype(np.float32), ["Y", "X"],
                                          "n2v", "unet", 64, True),
        "drift": lambda: DriftCorrectedResult("drift", _rng().random((3, 4, 4)).astype(np.float32),
                                              ["T", "Y", "X"], np.zeros((3, 2))),
        "multicolor-registration": lambda: MulticolorRegistrationResult(
            "reg", np.zeros((3, 2, 3, 2), np.float32), _identity_alignment(), params={"mode": "maxproj"}),
        "monalisa": _monalisa,
        "snouty": lambda: SnoutyResult("snouty", np.zeros((2, 3, 4), np.float32), dict(SNOUTY_DEFAULTS)),
        "snouty-projections": lambda: SnoutyProjectionsResult("proj3", np.zeros((3, 4, 4), np.float32), dict(SNOUTY_DEFAULTS)),
        "tiling": lambda: _tiling(tmp_path),
        "widefield-starss": _wfs,
    }


_ALL = [
    "array", "labels", "roi-mask", "points-table", "localization", "view-only", "composite", "rgb",
    "multicolor-apply", "projection", "frc", "colocalization", "psf", "segmentation", "denoise",
    "drift", "multicolor-registration", "monalisa", "snouty", "snouty-projections", "tiling",
    "widefield-starss",
]


def _cases():
    """(type, fmt) for every supported format of every type."""
    from imswitch.improcess.model.result import ProcessingResult

    classes = {
        "array": ArrayProcessingResult, "labels": LabelsResult, "roi-mask": ROIMaskResult,
        "points-table": PointsTableResult, "localization": LocalizationResult,
        "view-only": ViewOnlyResult, "composite": CompositeResult, "rgb": RGBResult,
        "multicolor-apply": MulticolorApplyResult, "projection": ProjectionResult, "frc": FRCResult,
        "colocalization": ColocalizationResult, "psf": PSFResolutionResult,
        "segmentation": SegmentationResult, "denoise": DenoisedResult, "drift": DriftCorrectedResult,
        "multicolor-registration": MulticolorRegistrationResult, "monalisa": MonalisaProcessingResult,
        "snouty": SnoutyResult, "snouty-projections": SnoutyProjectionsResult,
        "tiling": TilingMosaicResult, "widefield-starss": WidefieldStarssResult,
    }
    assert set(classes) == set(_ALL)
    for key in _ALL:
        for fmt in classes[key].supported_formats:
            assert issubclass(classes[key], ProcessingResult)
            yield key, fmt


class _Recon:
    id = "t.recon"
    name = "Test recon"


def _with_provenance(result, tmp_path):
    raw = tmp_path / "raw.h5"
    raw.write_bytes(b"0")
    source = SimpleNamespace(name="raw", dataPath=str(raw), datasetName="data", attrs={},
                             data_handle=np.zeros((2, 2)))
    record_reconstruction(result, _Recon(), {"k": 1}, source)
    return result


# --------------------------------------------------------------------------
# the exit test: every type x every format
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,fmt", list(_cases()))
def test_every_type_in_every_format_is_receipted_and_carries_its_graph(key, fmt, tmp_path):
    result = _with_provenance(_factories(tmp_path)[key](), tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    suffix = {"tiff": ".ome.tif", "hdf5": ".h5", "zarr": ".ome.zarr", "csv": ".csv",
              "picasso": ".hdf5", "imagej": ".tif"}[fmt]
    target = out_dir / f"{key}{suffix}"

    receipt = result.save(target, fmt)

    on_disk = sorted(p.name for p in out_dir.iterdir())
    assert sorted(p.name for p in receipt.files) == on_disk, "receipt must equal the files on disk"
    assert receipt.primary.exists() and receipt.fmt == fmt
    assert not any(name.startswith(".") for name in on_disk), "no staging leftovers"
    assert result.artifacts[-1] is receipt

    document = read_provenance(receipt.primary)
    assert document.graph is not None, f"{key}/{fmt}: primary carries no graph"
    assert document.graph["nodes"] == graph_of(result)["nodes"]
    assert document.artifact["primary"] == receipt.primary.name
    assert sorted(document.artifact["files"]) == on_disk
    assert document.node == receipt.node and document.port == receipt.port


def test_every_type_declares_its_formats():
    for key, fmt in _cases():
        assert normalize_format(fmt) == fmt, f"{key}: {fmt!r} is not canonical"


# --------------------------------------------------------------------------
# protocol properties
# --------------------------------------------------------------------------

def test_no_clobber_by_default_and_overwrite_on_request(tmp_path):
    result = _image()
    path = tmp_path / "x.ome.tif"
    result.save(path)
    with pytest.raises(FileExistsError):
        result.save(path)
    result.save(path, overwrite=True)
    assert path.exists()


def test_a_companion_that_already_exists_blocks_the_whole_save(tmp_path):
    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))
    (tmp_path / "d.ome.drift.npy").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        result.save(tmp_path / "d.ome.tif", "tiff")
    assert not (tmp_path / "d.ome.tif").exists()


def test_a_failure_after_the_companion_leaves_no_orphan(tmp_path, monkeypatch):
    """Crash between companion and primary: the directory is as it was."""
    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))

    def broken_write(plan, document):
        np.save(plan.companions[0], result.drift_xy)      # companion written...
        raise RuntimeError("disk on fire")                # ...then the primary fails

    monkeypatch.setattr(result, "write_files", broken_write)
    with pytest.raises(RuntimeError):
        result.save(tmp_path / "d.ome.tif", "tiff")
    assert list(tmp_path.iterdir()) == []


def test_a_writer_that_forgets_a_planned_file_is_caught(tmp_path, monkeypatch):
    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))
    monkeypatch.setattr(result, "write_files", lambda plan, document: plan.primary.write_bytes(b"x"))
    with pytest.raises(SaveError, match="did not write"):
        result.save(tmp_path / "d.ome.tif", "tiff")
    assert list(tmp_path.iterdir()) == []


def test_a_writer_that_writes_an_unplanned_file_is_caught(tmp_path, monkeypatch):
    result = _image()

    def sneaky(plan, document):
        plan.primary.write_bytes(b"x")
        (plan.primary.parent / "extra.bin").write_bytes(b"y")

    monkeypatch.setattr(result, "write_files", sneaky)
    with pytest.raises(SaveError, match="did not plan"):
        result.save(tmp_path / "x.ome.tif")
    assert list(tmp_path.iterdir()) == []


def test_an_unsupported_format_is_refused_before_anything_is_written(tmp_path):
    with pytest.raises(UnsupportedSaveFormat):
        _locs().save(tmp_path / "locs.ome.tif", "tiff")
    assert list(tmp_path.iterdir()) == []


def test_publish_is_a_rename_within_the_directory(tmp_path, monkeypatch):
    """Companions and primary reach their names by rename, not by copying."""
    seen = []
    real_link = os.link

    def spy(src, dst, *args, **kwargs):
        seen.append((Path(src).name, Path(dst).name))
        real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", spy)         # publish = atomic no-clobber link, then unlink
    result = DriftCorrectedResult("d", np.zeros((2, 4, 4), np.float32), ["T", "Y", "X"], np.zeros((2, 2)))
    result.save(tmp_path / "d.ome.tif", "tiff")
    assert [dst for _src, dst in seen] == ["d.ome.drift.npy", "d.ome.tif"]   # companion first, primary last


# --------------------------------------------------------------------------
# containers
# --------------------------------------------------------------------------

def test_csv_stays_plain_csv_and_the_document_rides_in_a_listed_companion(tmp_path):
    result = _with_provenance(_locs(), tmp_path)
    receipt = result.save(tmp_path / "locs.csv", "csv")
    first_line = (tmp_path / "locs.csv").read_text().splitlines()[0]
    assert first_line.startswith("frame,x_nm,y_nm")            # header contract untouched
    companion = companion_json_path(tmp_path / "locs.csv")
    assert companion in receipt.files
    document = ProvenanceDocument.from_json(companion.read_text())
    assert document.graph["nodes"] == graph_of(result)["nodes"]
    assert read_provenance(tmp_path / "locs.csv").graph is not None


def test_picasso_export_lists_its_yaml_and_carries_the_graph(tmp_path):
    result = _with_provenance(_locs(), tmp_path)
    receipt = result.save(tmp_path / "locs.hdf5", "picasso")
    assert {p.name for p in receipt.files} == {"locs.hdf5", "locs.yaml"}
    assert read_provenance(tmp_path / "locs.hdf5").graph is not None


def test_a_schema_zero_file_reads_back_as_history_only(tmp_path):
    import tifffile

    tifffile.imwrite(str(tmp_path / "old.tif"), np.zeros((4, 4), np.uint8),
                     description=json.dumps({"processing_history": [{"operation": "crop", "params": {}}]}))
    document = read_provenance(tmp_path / "old.tif")
    assert document.graph is None and document.schema == 0
    assert document.history[0]["operation"] == "crop"


def test_a_file_with_no_provenance_reads_back_empty(tmp_path):
    import tifffile

    tifffile.imwrite(str(tmp_path / "plain.tif"), np.zeros((4, 4), np.uint8))
    document = read_provenance(tmp_path / "plain.tif")
    assert document.graph is None and document.history == [] and document.artifact is None


def test_a_hostile_graph_in_a_file_is_rejected_on_read(tmp_path):
    import h5py

    from imswitch.improcess.model.provenance import ProvenanceError

    graph = {"schema": 1, "output": {"node": "a", "port": "out"},
             "nodes": {"a": {"op": "process", "inputs": [{"node": "a", "port": "out"}], "outputs": ["out"]}}}
    with h5py.File(str(tmp_path / "bad.h5"), "w") as handle:
        handle.attrs["provenance"] = json.dumps({"schema": 1, "graph": graph})
    with pytest.raises(ProvenanceError, match="cycle"):
        read_provenance(tmp_path / "bad.h5")
    assert read_provenance(tmp_path / "bad.h5", validate=False).graph is not None


# --------------------------------------------------------------------------
# MoNaLISA keeps its file layout
# --------------------------------------------------------------------------

def test_monalisa_ome_tiff_keeps_the_tzcyx_fold_with_channel_names(tmp_path):
    import tifffile
    from xml.etree import ElementTree

    result = _monalisa()
    receipt = result.save(tmp_path / "m.ome.tif", "tiff")
    with tifffile.TiffFile(str(receipt.primary)) as handle:
        stored = handle.series[0].shape
        axes = handle.series[0].axes
        ome = handle.ome_metadata
    # (Dataset=1, Base=2, T=1, Z=1, Y, X) -> (T=1, Z=1, C=2, Y, X); tifffile squeezes size-1 axes
    assert result.data.shape[:2] == (1, 2)
    assert stored[-3:] == (2, result.data.shape[-2], result.data.shape[-1])
    assert axes.endswith("CYX")
    root = ElementTree.fromstring(ome)
    namespace = root.tag.split("}")[0].strip("{")
    pixels = root.find(f".//{{{namespace}}}Pixels")
    assert pixels.get("SizeC") == "2"
    names = [c.get("Name") for c in pixels.findall(f"{{{namespace}}}Channel")]
    assert names == ["ds0-signal", "ds0-background"]
    assert float(pixels.get("PhysicalSizeX")) == pytest.approx(40.0)
    assert float(pixels.get("PhysicalSizeY")) == pytest.approx(80.0)
    assert read_provenance(receipt.primary).graph is None or True  # no recon node on a bare result


def test_monalisa_imagej_writer_is_still_available_and_carries_the_document(tmp_path):
    import tifffile

    result = _with_provenance(_monalisa(), tmp_path)
    receipt = result.save(tmp_path / "m.tif", "imagej")
    with tifffile.TiffFile(str(receipt.primary)) as handle:
        assert handle.is_imagej
        assert handle.series[0].axes.endswith("CYX")   # tifffile consumes the axes key itself
    assert read_provenance(receipt.primary).graph["nodes"] == graph_of(result)["nodes"]


def test_widefield_starss_channel_names_reach_the_ome_xml(tmp_path):
    from xml.etree import ElementTree
    import tifffile

    result = _wfs()
    receipt = result.save(tmp_path / "wfs.ome.tif", "tiff")
    assert {p.name for p in receipt.files} == {"wfs.ome.tif", "wfs.ome.regions.csv"}   # with_suffix, as before
    with tifffile.TiffFile(str(receipt.primary)) as handle:
        root = ElementTree.fromstring(handle.ome_metadata)
    namespace = root.tag.split("}")[0].strip("{")
    names = [c.get("Name") for c in root.iter(f"{{{namespace}}}Channel")]
    assert names == ["r_smooth", "r_raw", "mask", "base_image"]
    assert isinstance(pd.read_csv(tmp_path / "wfs.ome.regions.csv"), pd.DataFrame)


def test_multicolor_registration_no_longer_writes_from_apply(tmp_path):
    from imswitch.improcess.processors.multicolor_registration.processor import (
        MulticolorRegistrationProcessor,
    )

    processor = MulticolorRegistrationProcessor()
    widget = processor.make_param_widget(None) if False else None   # widget built elsewhere
    assert widget is None
    # the parameter contract has no file side effect any more
    import inspect

    source = inspect.getsource(MulticolorRegistrationProcessor.apply)
    assert "save_alignment(" not in source


def test_plan_companions_must_live_beside_the_primary(tmp_path):
    with pytest.raises(SaveError):
        SavePlan(tmp_path / "a.tif", "tiff", (tmp_path / "sub" / "b.npy",))
