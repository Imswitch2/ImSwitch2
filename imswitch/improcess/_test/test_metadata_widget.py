"""Metadata panel: tree rendering, filtering, exports and controller wiring."""

import os
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.model.metadata_tree import read_metadata_tree

# Direct import: view/__init__ pulls in the napari-backed widgets.
_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from MetadataWidget import MetadataWidget
finally:
    sys.path.pop(0)


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "rec.h5"
    with h5py.File(path, "w") as file:
        file.attrs["rec_mode"] = "snap"
        detector = file.create_group("Camera")
        detector.create_dataset("data", data=np.zeros((2, 3, 4), dtype="uint16"))
        metadata = detector.create_group("metadata")
        metadata.create_group("lasers").attrs["488 Laser"] = 12.5
    return path


def _items(tree_widget):
    """Every QTreeWidgetItem, with its hidden state."""
    found = []
    stack = [tree_widget.topLevelItem(index) for index in range(tree_widget.topLevelItemCount())]
    while stack:
        item = stack.pop()
        found.append(item)
        for index in range(item.childCount()):
            stack.append(item.child(index))
    return found


def _texts(tree_widget, *, visible_only=False):
    return [
        item.text(0)
        for item in _items(tree_widget)
        if not (visible_only and item.isHidden())
    ]


def test_widget_renders_the_whole_hierarchy(qapp, recording):
    widget = MetadataWidget()
    widget.setMetadataTree(read_metadata_tree(recording))

    names = _texts(widget.tree)
    assert "rec_mode" in names
    assert "Camera" in names
    assert "data" in names
    assert "488 Laser" in names
    assert widget.reloadButton.isEnabled() is False  # only the controller enables it


def test_filter_keeps_matches_and_their_ancestors(qapp, recording):
    widget = MetadataWidget()
    widget.setMetadataTree(read_metadata_tree(recording))

    widget.applyFilter("488")

    visible = _texts(widget.tree, visible_only=True)
    assert "488 Laser" in visible
    assert "lasers" in visible  # ancestor kept for context
    assert "Camera" in visible
    assert "rec_mode" not in visible

    widget.applyFilter("")
    assert "rec_mode" in _texts(widget.tree, visible_only=True)


def test_filter_matches_values_not_only_names(qapp, recording):
    widget = MetadataWidget()
    widget.setMetadataTree(read_metadata_tree(recording))

    widget.applyFilter("snap")

    assert "rec_mode" in _texts(widget.tree, visible_only=True)


def test_clearing_the_tree_disables_the_actions(qapp, recording):
    widget = MetadataWidget()
    widget.setMetadataTree(read_metadata_tree(recording))
    assert widget.exportButton.isEnabled()

    widget.setMetadataTree(None)

    assert widget.currentTree() is None
    assert not widget.exportButton.isEnabled()
    assert widget.tree.topLevelItemCount() == 0


def test_push_to_results_emits_the_visible_rows(qapp, recording):
    widget = MetadataWidget()
    widget.setMetadataTree(read_metadata_tree(recording))
    pushed = []
    widget.sigResultPushed.connect(lambda columns, records: pushed.append((columns, records)))

    widget.applyFilter("488")
    widget.pushToResults()

    assert len(pushed) == 1
    columns, records = pushed[0]
    assert columns == ["path", "value", "type"]
    assert [record["path"] for record in records] == ["Camera/metadata/lasers/488 Laser"]
    assert records[0]["value"] == "12.5"


def test_exports_cover_json_and_csv(qapp, recording):
    widget = MetadataWidget()
    widget.setMetadataTree(read_metadata_tree(recording))

    assert '"488 Laser"' in widget.toJson()
    csv_text = widget.toCsv()
    assert csv_text.splitlines()[0] == "path,value,type"
    assert "Camera/metadata/lasers/488 Laser" in csv_text


# -- controller ---------------------------------------------------------------


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _FakeCommChannel:
    def __init__(self):
        self.sigCurrentDataChanged = _FakeSignal()


class _FakeDataObj:
    def __init__(self, path, file=None):
        self.dataPath = str(path)
        self.name = Path(path).name
        self._file = file


def _make_controller(widget, comm_channel):
    from imswitch.improcess.controller.MetadataController import MetadataController

    return MetadataController(
        commChannel=comm_channel,
        widget=widget,
        factory=None,
        moduleCommChannel=None,
    )


def test_controller_shows_metadata_of_the_current_data(qapp, recording):
    widget = MetadataWidget()
    comm_channel = _FakeCommChannel()
    _make_controller(widget, comm_channel)

    comm_channel.sigCurrentDataChanged.emit(_FakeDataObj(recording))

    assert widget.currentTree() is not None
    assert "488 Laser" in _texts(widget.tree)
    assert widget.reloadButton.isEnabled()


def test_controller_uses_an_already_open_file_handle(qapp, recording):
    widget = MetadataWidget()
    comm_channel = _FakeCommChannel()
    _make_controller(widget, comm_channel)

    with h5py.File(recording, "r") as file:
        comm_channel.sigCurrentDataChanged.emit(_FakeDataObj(recording, file=file))
        # The controller must not close a handle it does not own.
        assert file["Camera/data"].shape == (2, 3, 4)

    assert "488 Laser" in _texts(widget.tree)


def test_controller_clears_the_panel_when_data_is_unloaded(qapp, recording):
    widget = MetadataWidget()
    comm_channel = _FakeCommChannel()
    _make_controller(widget, comm_channel)
    comm_channel.sigCurrentDataChanged.emit(_FakeDataObj(recording))

    comm_channel.sigCurrentDataChanged.emit(None)

    assert widget.currentTree() is None
    assert not widget.reloadButton.isEnabled()


def test_runtime_tool_wiring_can_build_the_panel(qapp):
    """The Tools toolbar / 'Load tool' path must reach a MetadataWidget."""
    from types import SimpleNamespace

    from imswitch.improcess.controller.shortcuts import improcess_shortcut_defaults
    from imswitch.improcess.model.runtime_tools import (
        runtime_analysis_panel_shortcuts,
        runtime_analysis_tool_specs,
    )
    from imswitch.improcess.view.icons import IMPROCESS_ICON_NAMES
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    spec = runtime_analysis_tool_specs()["metadata"]
    assert (spec.title, spec.attribute, spec.widget_kind) == (
        "Metadata",
        "metadataWidget",
        "metadata",
    )
    assert "metadata" in {shortcut.id for shortcut in runtime_analysis_panel_shortcuts()}
    assert "metadata" in IMPROCESS_ICON_NAMES
    assert "panel.metadata" in improcess_shortcut_defaults()

    stub_view = SimpleNamespace(
        reconstructionWidget=SimpleNamespace(napariViewer=None),
        roiManagerWidget=None,
    )
    widget = ImProcessMainView._runtimeAnalysisToolFactory(stub_view, spec)()

    # Not `isinstance`: this module imports the widget by file path, so its
    # class object differs from the one the view imports.
    assert type(widget).__name__ == "MetadataWidget"


def test_controller_reports_unreadable_files_instead_of_raising(qapp, tmp_path):
    broken = tmp_path / "broken.h5"
    broken.write_bytes(b"not hdf5")
    widget = MetadataWidget()
    comm_channel = _FakeCommChannel()
    _make_controller(widget, comm_channel)

    comm_channel.sigCurrentDataChanged.emit(_FakeDataObj(broken))

    # A corrupt container still yields a tree — the failure shows as an entry.
    assert widget.currentTree() is not None
    assert any(
        "error" in item.text(2).lower() for item in _items(widget.tree)
    )
