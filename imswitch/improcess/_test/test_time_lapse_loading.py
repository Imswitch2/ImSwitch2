"""A lapse through the GUI's own paths: open, pick, show.

- With the Time lapse reconstructor active, opening any item makes the whole
  lapse the current source -- including a single-file lapse, which otherwise
  asks which of its per-point groups to open. With any other reconstructor, or
  for a file that is not a lapse, the file opens as the image it is.
- The picker cannot strand the user on the lapse: a reconstructor that wants
  the picked file as an image is still offered, and choosing it reopens it.
  A lapse is opened at the very file the user picked, which is what the old
  "already loaded" test (same path) got wrong.
- The viewer hands napari a dask array, so the stack is read a plane at a time
  as the slider moves instead of whole before the first plane is shown.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess._test._lapse_recordings import FRAME_SHAPE, record_lapse, record_point
from imswitch.improcess.controller.FileIOController import FileIOController
from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController as Manager,
)
from imswitch.improcess.model import DataObj, lapse_source
from imswitch.improcess.model.lapse_source import (
    TIME_LAPSE_SOURCE_KIND,
    TimeLapseIndex,
    discover_time_lapse,
    plan_time_lapse,
    read_lapse_headers,
)
from imswitch.improcess.model.lazy_array import is_dask_array
from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES
from imswitch.improcess.reconstructors.time_lapse import TimeLapseReconstructor

_QUIET = SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, debug=lambda *a, **k: None,
)


def _file_io(active):
    """The loader's own methods over a minimal surface."""
    emitted = []
    io = FileIOController.__new__(FileIOController)
    io._main = SimpleNamespace(_activeReconstructor=active, _currentDataObj=None)
    io._logger = _QUIET
    io._commChannel = SimpleNamespace(
        sigCurrentDataChanged=SimpleNamespace(emit=emitted.append)
    )

    def no_picker(*_args, **_kwargs):
        raise AssertionError("the dataset picker was shown")

    io._widget = SimpleNamespace(
        raiseCurrentDataDock=lambda: None, showPickDatasetsDialog=no_picker,
    )
    io.pickDatasetsController = SimpleNamespace(setDatasets=lambda *a: None)
    return io, emitted


@pytest.fixture
def single_file_lapse(tmp_path):
    return record_lapse(
        tmp_path, 5, camera=False, single_file=True, frames=2, layout_kind="time"
    )[0]


def test_an_item_opens_the_whole_lapse_while_time_lapse_is_active(single_file_lapse):
    io, emitted = _file_io(TimeLapseReconstructor())

    outcome = io._loadFromPath(str(single_file_lapse), prefer_as_current=True)

    assert outcome == "current"
    (data_obj,) = emitted
    assert data_obj.sourceKind == TIME_LAPSE_SOURCE_KIND
    assert isinstance(data_obj.sourceMetadata, TimeLapseIndex)
    assert len(data_obj.sourceMetadata.channel().candidates) == 5
    assert data_obj.sourceOriginalPath == str(single_file_lapse)
    assert data_obj.name == "12h00m00s_rec"


def test_a_file_that_is_not_a_lapse_still_opens_as_an_image(tmp_path):
    single = record_point(tmp_path, index=0, total=1)
    io, emitted = _file_io(TimeLapseReconstructor())

    outcome = io._loadFromPath(str(single), prefer_as_current=True)

    assert outcome == "current"
    assert emitted[0].sourceKind == "image"


def test_other_reconstructors_open_a_lapse_item_as_an_image(tmp_path):
    item = record_lapse(tmp_path, 3)[1]
    io, emitted = _file_io(_AVAILABLE_RECONSTRUCTOR_CLASSES["view-only"]())

    io._loadFromPath(str(item), prefer_as_current=True)

    assert emitted[0].sourceKind == "image"


def _two_lapses_in_one_file(folder, fmt="hdf5"):
    common = dict(camera=False, single_file=True, frames=2, layout_kind="time", fmt=fmt)
    record_lapse(folder, 3, **common)
    return record_lapse(folder, 3, started_at=None, **common)[0]


def _stacked_items(index):
    return [slot.dataset for slot in plan_time_lapse(index, read_lapse_headers(index)).slots]


def test_a_file_holding_two_lapses_asks_for_an_item_and_opens_its_lapse(tmp_path):
    """Picking the file alone cannot say which lapse; opening the first would
    leave the second unreachable. The dataset picker asks, and the lapse of
    the item picked is the one opened."""
    path = _two_lapses_in_one_file(tmp_path)
    io, emitted = _file_io(TimeLapseReconstructor())
    offered = []
    io.pickDatasetsController = SimpleNamespace(
        setDatasets=lambda _path, names: offered.extend(names),
        getSelectedDatasets=lambda: ["scan4/Camera"],
    )
    io._widget.showPickDatasetsDialog = lambda blocking=True: True

    outcome = io._loadFromPath(str(path), prefer_as_current=True)

    assert outcome == "current"
    assert offered == [f"scan{n}/Camera" for n in range(6)]
    (data_obj,) = emitted
    assert data_obj.sourceKind == TIME_LAPSE_SOURCE_KIND
    assert data_obj.sourceMetadata.anchor.dataset == "scan4/Camera"
    assert _stacked_items(data_obj.sourceMetadata) == [
        "scan3/Camera", "scan4/Camera", "scan5/Camera"
    ]


def test_a_path_picked_inside_a_zarr_lapse_opens_that_items_lapse(tmp_path):
    path = _two_lapses_in_one_file(tmp_path, fmt="zarr")
    io, emitted = _file_io(TimeLapseReconstructor())    # the picker must not show

    outcome = io._loadFromPath(str(path / "scan4" / "Camera"), prefer_as_current=True)

    assert outcome == "current"
    (data_obj,) = emitted
    assert data_obj.sourceMetadata.anchor.dataset == "scan4/Camera"
    assert _stacked_items(data_obj.sourceMetadata) == [
        "scan3/Camera", "scan4/Camera", "scan5/Camera"
    ]


def test_a_lapse_source_resolves_its_items_acquisition_layout(single_file_lapse):
    index = discover_time_lapse(single_file_lapse)
    data_obj = DataObj.fromMetadataSource(
        index.name, index.anchor.path, TIME_LAPSE_SOURCE_KIND, index,
        originalPath=single_file_lapse,
    )

    layout = data_obj.acquisition_layout.layout

    assert [partition.kind for partition in layout.partitions] == ["time"]
    assert layout.modality != "tiling"


# --------------------------------------------------------------------------
# the picker
# --------------------------------------------------------------------------


@pytest.fixture
def reconstructors(monkeypatch):
    plugins = {
        plugin_id: cls()
        for plugin_id, cls in _AVAILABLE_RECONSTRUCTOR_CLASSES.items()
        if plugin_id in ("time-lapse", "view-only")
    }
    ordered = [plugins["view-only"], plugins["time-lapse"]]
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.registry.get_registry",
        lambda: SimpleNamespace(reconstructors=lambda: list(ordered)),
    )
    return plugins


def _manager(reconstructors, active, data_obj):
    published = []
    reopened = []
    main = SimpleNamespace(
        _activeReconstructor=reconstructors[active],
        _currentDataObj=data_obj,
        fileIOController=SimpleNamespace(
            _loadFromPath=lambda path, **kwargs: reopened.append((path, kwargs))
        ),
    )
    manager = SimpleNamespace(
        _main=main,
        _widget=SimpleNamespace(
            setReconstructorChoices=lambda choices, current: published.append(
                [plugin_id for plugin_id, _name in choices]
            )
        ),
        _logger=_QUIET,
        _install_reconstructor_params=lambda reconstructor: None,
        _inspect_current_source=lambda: None,
    )
    for name in (
        "_accepts_current_source", "_offerable", "_reopen_path_for",
        "_publishReconstructorChoices", "_reopen_current_source",
        "_on_user_changed_reconstructor", "_confirm_reconstructor_change",
    ):
        bound = getattr(Manager, name)
        setattr(manager, name, (lambda fn: lambda *a, **k: fn(manager, *a, **k))(bound))
    manager.published = published
    manager.reopened = reopened
    return manager, main


def _opened_lapse(path):
    index = discover_time_lapse(path)
    return DataObj.fromMetadataSource(
        index.name, index.anchor.path, TIME_LAPSE_SOURCE_KIND, index,
        originalPath=path,
    )


def test_view_only_stays_offered_for_a_lapse(single_file_lapse, reconstructors):
    manager, _main = _manager(
        reconstructors, "time-lapse", _opened_lapse(single_file_lapse)
    )

    manager._publishReconstructorChoices()

    assert manager.published[-1] == ["view-only", "time-lapse"]


def test_choosing_view_only_reopens_the_picked_file(single_file_lapse, reconstructors):
    manager, main = _manager(
        reconstructors, "time-lapse", _opened_lapse(single_file_lapse)
    )

    manager._on_user_changed_reconstructor("view-only")

    assert main._activeReconstructor.id == "view-only"
    assert manager.reopened == [
        (str(single_file_lapse), {"prefer_as_current": True})
    ]


def test_an_image_that_is_loaded_is_not_offered_to_reopen(tmp_path, reconstructors):
    """The narrower "already loaded" test still holds for a plain image."""
    path = record_point(tmp_path, index=0, total=1)
    data_obj = SimpleNamespace(
        sourceKind="image", dataPath=str(path), sourceOriginalPath=str(path)
    )
    manager, _main = _manager(reconstructors, "view-only", data_obj)

    assert manager._reopen_path_for(reconstructors["view-only"]) is None


# --------------------------------------------------------------------------
# the viewer
# --------------------------------------------------------------------------


class _View:
    def __init__(self):
        self.images = []
        self.imgLayer = SimpleNamespace()
        self.napariViewer = SimpleNamespace(
            layers=SimpleNamespace(selection=SimpleNamespace(active=None))
        )

    def getViewName(self):
        return "standard"

    def setImage(self, im, axisLabels, axisScales=None, scaleUnit="px",
                 colormap="grayclip", name=None, identity=None):
        self.images.append((im, list(axisLabels), list(axisScales), identity))


def test_the_viewer_gets_the_lapse_lazily(tmp_path, monkeypatch):
    paths = record_lapse(tmp_path, 6)
    result = TimeLapseReconstructor().process(
        SimpleNamespace(dataPath=str(paths[0]), name="lapse", datasetName=None,
                        sourceKind="image"),
        {},
    )
    reads = []
    real_read = lapse_source._ItemReader.read
    monkeypatch.setattr(
        lapse_source._ItemReader, "read",
        lambda self, ref, dataset, key: reads.append(ref.ordinal)
        or real_read(self, ref, dataset, key),
    )
    view = _View()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._logger = _QUIET

    controller._setProcessingResultSlice(result)

    ((image, labels, scales, identity),) = view.images
    assert is_dask_array(image)
    assert image.shape == (6, *FRAME_SHAPE)
    assert [axis["size"] for axis in identity["axes"]] == [6, *FRAME_SHAPE]
    assert labels == ["T", "Y", "X"]
    assert reads == []
    # What napari does when the slider lands on a timepoint.
    np.testing.assert_array_equal(np.asarray(image[4]), np.full(FRAME_SHAPE, 5))
    assert reads == [4]


def test_the_real_view_keeps_a_dask_array_lazy(monkeypatch):
    """``ReconstructionView.setImage`` must not ``np.asarray`` a dask array."""
    da = pytest.importorskip("dask.array")
    from imswitch.improcess.view.ReconstructionView import ReconstructionView

    computed = []
    stack = da.from_array(np.ones((3, 4, 5)), chunks=(1, 4, 5)).map_blocks(
        lambda block: computed.append(block.shape) or block, dtype=float
    )
    computed.clear()  # dask evaluates a zero-size block to build the graph
    layer = SimpleNamespace(
        data=np.zeros((1, 1)), visible=True, name="", colormap="", scale=(),
        metadata={},
    )
    view = SimpleNamespace(
        imgLayer=layer,
        napariViewer=SimpleNamespace(
            dims=SimpleNamespace(axis_labels=()),
            scale_bar=SimpleNamespace(unit=""),
        ),
        _clearDisplayLayers=lambda: None,
        _patchLayerForNdimChange=lambda *a: None,
        _logger=_QUIET,
    )

    ReconstructionView.setImage(view, stack, ["T", "Y", "X"], [1.0, 1.0, 1.0])

    assert layer.data is stack
    assert computed == []
