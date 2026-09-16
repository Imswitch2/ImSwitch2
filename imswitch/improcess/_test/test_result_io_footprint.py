"""Saving a result keeps its calibration and says how it was made.

Reported from the rig: a file was cropped, saved as TIFF and reopened, and the
metadata was empty. Two causes. Nothing recorded the crop in the first place,
and the TIFF writer was ``imwrite(path, data)`` -- pixels only, so even the
pixel size that was known throughout was dropped at the one point it cannot be
recovered from.
"""

import json
from xml.etree import ElementTree

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.footprint import (
    HISTORY_KEY,
    history_of,
    json_safe,
    make_step,
    record_step,
)
from imswitch.improcess.model.result_io import (
    UnsupportedResultFormat,
    format_for_path,
    ome_meta_for_result,
    save_image_result,
)
from imswitch.improcess.processors.base import normalize_processor_output
from imswitch.improcess.processors.stack_subset.processor import StackSubsetProcessor


def _source(name="MoNaLISA recon"):
    return ArrayProcessingResult(
        name=name,
        data=np.random.default_rng(0).random((1, 64, 64)).astype(np.float32),
        axis_labels=["Z", "Y", "X"],
        axis_scales=[1.0, 0.1, 0.1],
        scale_unit="um",
    )


def _crop(source, **extra):
    """The reported workflow: crop a reconstruction through the real path."""
    processor = StackSubsetProcessor()
    params = {
        "ranges": [
            {"axis": 1, "start": 10, "stop": 30},
            {"axis": 2, "start": 20, "stop": 45},
        ],
        "copy": True,
    }
    params.update(extra)
    output = processor.apply(source, params)
    return normalize_processor_output(output, source, processor, params)[0]


def _ome_description(path):
    """The Description an OME reader would parse out (entity-decoded)."""
    import tifffile

    with tifffile.TiffFile(path) as handle:
        root = ElementTree.fromstring(handle.ome_metadata)
    namespace = {"o": root.tag.split("}")[0].strip("{")}
    node = root.find(".//o:Description", namespace)
    return json.loads(node.text) if node is not None else {}


# --------------------------------------------------------------------------
# the footprint
# --------------------------------------------------------------------------

def test_a_crop_records_what_it_did():
    cropped = _crop(_source())
    history = history_of(cropped)

    assert len(history) == 1
    assert history[0]["label"] == "Crop/Substack"
    assert history[0]["params"]["ranges"][0]["start"] == 10


def test_a_step_names_the_result_it_came_from():
    cropped = _crop(_source("MoNaLISA recon"))
    assert history_of(cropped)[0]["inputs"][0].startswith("MoNaLISA recon [")


def test_steps_accumulate_down_a_chain():
    """Crop the crop: the second result carries both steps, oldest first."""
    once = _crop(_source())
    twice = _crop(
        once,
        ranges=[{"axis": 1, "start": 2, "stop": 8}, {"axis": 2, "start": 3, "stop": 9}],
    )

    assert [step["label"] for step in history_of(twice)] == [
        "Crop/Substack",
        "Crop/Substack",
    ]


def test_a_derived_result_does_not_grow_its_source_history():
    """Two results from one source must not write into each other's past."""
    source = _source()
    first = _crop(source)
    second = _crop(source)

    assert history_of(source) == []
    assert len(history_of(first)) == 1
    assert len(history_of(second)) == 1


def test_the_roi_a_crop_came_from_is_recorded():
    cropped = _crop(_source(), roi_name="cell", roi_uid="u1")
    params = history_of(cropped)[0]["params"]
    assert params["roi_name"] == "cell"
    assert params["roi_uid"] == "u1"


def test_a_run_with_no_processor_records_nothing():
    source = _source()
    record_step((source,), None, None, {})
    assert history_of(source) == []


# -- params that are not JSON ----------------------------------------------

def test_an_array_parameter_is_summarised_not_dropped():
    """A key going missing reads as "there was no such setting"."""
    step = make_step("x", params={"kernel": np.zeros((512, 512))})
    assert "512" in step["params"]["kernel"]


def test_an_object_parameter_falls_back_to_its_name():
    class _Thing:
        name = "gaussian"

    assert make_step("x", params={"model": _Thing()})["params"]["model"] == "gaussian"


def test_non_finite_numbers_survive_json():
    """JSON has no NaN; a reader that rejects it would reject the whole file."""
    encoded = json.dumps(json_safe({"fill": float("nan")}))
    assert json.loads(encoded)["fill"] == "nan"


def test_a_huge_string_is_truncated():
    step = make_step("x", params={"expression": "a" * 5000})
    assert len(step["params"]["expression"]) < 700


# --------------------------------------------------------------------------
# what reaches the file
# --------------------------------------------------------------------------

def test_a_saved_tiff_carries_the_pixel_size(tmp_path):
    path = tmp_path / "crop.ome.tif"
    _crop(_source()).save(path, "tiff")

    import tifffile

    with tifffile.TiffFile(path) as handle:
        root = ElementTree.fromstring(handle.ome_metadata)
    namespace = {"o": root.tag.split("}")[0].strip("{")}
    pixels = root.find(".//o:Pixels", namespace)
    assert float(pixels.get("PhysicalSizeX")) == pytest.approx(0.1)
    assert pixels.get("PhysicalSizeXUnit") == "µm"


def test_a_saved_tiff_carries_the_footprint(tmp_path):
    """The reported failure, end to end."""
    path = tmp_path / "crop.ome.tif"
    _crop(_source(), roi_name="cell").save(path, "tiff")

    history = _ome_description(path)[HISTORY_KEY]
    assert history[0]["label"] == "Crop/Substack"
    assert history[0]["params"]["roi_name"] == "cell"


def test_hdf5_carries_the_footprint_and_a_fiji_readable_pixel_size(tmp_path):
    import h5py

    path = tmp_path / "crop.h5"
    _crop(_source()).save(path, "hdf5")

    with h5py.File(path) as handle:
        assert list(handle["data"].attrs["element_size_um"]) == [1.0, 0.1, 0.1]
        assert "ome_xml" in handle.attrs
        history = json.loads(handle.attrs[HISTORY_KEY])
    assert history[0]["label"] == "Crop/Substack"


def test_zarr_is_written_as_ome_ngff(tmp_path):
    import zarr

    path = tmp_path / "crop.ome.zarr"
    _crop(_source()).save(path, "zarr")

    group = zarr.open_group(str(path), mode="r")
    attributes = dict(group.attrs)
    multiscales = attributes["ome"]["multiscales"][0]
    assert [axis["name"] for axis in multiscales["axes"]] == ["z", "y", "x"]
    scale = multiscales["datasets"][0]["coordinateTransformations"][0]["scale"]
    assert scale == pytest.approx([1.0, 0.1, 0.1])
    assert json.loads(attributes[HISTORY_KEY])[0]["label"] == "Crop/Substack"


def test_the_pixels_survive_every_container(tmp_path):
    cropped = _crop(_source())
    expected = np.asarray(cropped.data)

    import h5py
    import tifffile
    import zarr

    cropped.save(tmp_path / "a.ome.tif", "tiff")
    cropped.save(tmp_path / "a.h5", "hdf5")
    cropped.save(tmp_path / "a.ome.zarr", "zarr")

    assert np.array_equal(
        tifffile.imread(tmp_path / "a.ome.tif").reshape(expected.shape), expected
    )
    with h5py.File(tmp_path / "a.h5") as handle:
        assert np.array_equal(handle["data"][...], expected)
    assert np.array_equal(
        zarr.open_group(str(tmp_path / "a.ome.zarr"), mode="r")["0"][...], expected
    )


# --------------------------------------------------------------------------
# choosing a format
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,expected",
    [
        ("x.tif", "tiff"),
        ("x.ome.tif", "tiff"),
        ("x.h5", "hdf5"),
        ("x.hdf5", "hdf5"),
        ("x.zarr", "zarr"),
        # The two-part suffix must win: read as a bare ".zarr" following an
        # ".ome" this is still Zarr, but ".ome.tif" read as ".tif" is fine
        # while a hypothetical ".zarr.tif" would not be.
        ("x.ome.zarr", "zarr"),
        ("x.unknown", "tiff"),
    ],
)
def test_the_suffix_picks_the_format(filename, expected):
    assert format_for_path(filename) == expected


def test_an_unwritable_format_says_so(tmp_path):
    with pytest.raises(UnsupportedResultFormat):
        save_image_result(_source(), tmp_path / "x.png", "png")


def test_an_uncalibrated_result_does_not_claim_micrometres():
    """Defaulting a px result to µm would invent a calibration."""
    plain = ArrayProcessingResult(
        name="raw", data=np.zeros((4, 4)), axis_labels=["Y", "X"]
    )
    assert all(axis.unit is None for axis in ome_meta_for_result(plain).axes)


def test_a_result_with_fewer_labels_than_axes_is_still_saveable(tmp_path):
    """OmeImageMeta refuses mismatched axes/scale; a partly-labelled result
    must not become unsaveable because of it."""
    odd = ArrayProcessingResult(
        name="odd", data=np.zeros((2, 4, 4)), axis_labels=["Y", "X"]
    )
    save_image_result(odd, tmp_path / "odd.ome.tif", "tiff")
    assert (tmp_path / "odd.ome.tif").exists()


# --------------------------------------------------------------------------
# sanity round: cases the first pass did not cover
# --------------------------------------------------------------------------

def test_every_processor_output_carries_a_footprint():
    """Five result types have no metadata dict of their own. Skipping those
    would leave the footprint present for some results and absent for others,
    and "no history" is indistinguishable from "nothing was done"."""
    from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES

    source = ArrayProcessingResult(
        name="src",
        data=np.random.default_rng(0).random((3, 16, 16)).astype(np.float32),
        axis_labels=["Z", "Y", "X"],
    )
    missing = []
    for processor_id, cls in sorted(_AVAILABLE_PROCESSOR_CLASSES.items()):
        processor = cls()
        if not processor.accepts(source):
            continue
        try:
            outputs = normalize_processor_output(
                processor.apply(source, {}), source, processor, {}
            )
        except Exception:
            continue           # needs parameters; not this test's business
        for output in outputs:
            if not history_of(output):
                missing.append((processor_id, type(output).__name__))
    assert missing == []


def test_a_result_without_metadata_is_given_one():
    class _Bare:
        name = "bare"

    bare = _Bare()
    record_step((bare,), None, _FakeProcessor(), {"radius": 3})

    assert history_of(bare)[0]["params"] == {"radius": 3}


class _FakeProcessor:
    id = "fake"
    name = "Fake"


# -- axis names OME will actually accept -----------------------------------

@pytest.mark.parametrize(
    "labels,shape",
    [
        (["Y", "X"], (2, 4, 4)),                    # fewer labels than axes
        (["A", "B", "Y", "X"], (2, 2, 4, 4)),       # two unrecognised labels
        (["C", "C", "Y", "X"], (2, 2, 4, 4)),       # a duplicated label
        (list("ABCDEF"), (2, 2, 2, 2, 4, 4)),       # more axes than OME names
    ],
)
def test_an_unusual_axis_set_is_still_saveable(labels, shape, tmp_path):
    """Saving used to be `imwrite(path, data)`, which always worked. Adding
    OME metadata must not make a result unwritable -- tifffile refuses
    "multiple 'Z' dimensions" outright."""
    import tifffile

    result = ArrayProcessingResult(
        name="odd", data=np.zeros(shape), axis_labels=list(labels)
    )
    path = tmp_path / "odd.ome.tif"
    save_image_result(result, path, "tiff")

    assert tifffile.imread(path).size == np.zeros(shape).size


def test_ome_axes_are_never_duplicated():
    result = ArrayProcessingResult(
        name="odd", data=np.zeros((2, 2, 4, 4)), axis_labels=["A", "B", "Y", "X"]
    )
    names = [axis.name for axis in ome_meta_for_result(result).axes]
    assert len(names) == len(set(names))


def test_the_footprint_survives_the_fallback_write(tmp_path):
    """The plainer file is still worth writing *because* it carries this."""
    import tifffile

    result = ArrayProcessingResult(
        name="odd", data=np.zeros((2, 2, 2, 2, 4, 4)), axis_labels=list("ABCDEF")
    )
    record_step((result,), None, _FakeProcessor(), {"radius": 3})
    path = tmp_path / "odd.ome.tif"
    save_image_result(result, path, "tiff")

    with tifffile.TiffFile(path) as handle:
        description = handle.pages[0].description
    assert "radius" in description


def test_an_unusual_axis_set_is_still_saveable_as_zarr(tmp_path):
    import zarr

    result = ArrayProcessingResult(
        name="odd", data=np.zeros((2, 2, 2, 2, 4, 4)), axis_labels=list("ABCDEF")
    )
    path = tmp_path / "odd.ome.zarr"
    save_image_result(result, path, "zarr")

    group = zarr.open_group(str(path), mode="r")
    assert group["0"].shape == (2, 2, 2, 2, 4, 4)
