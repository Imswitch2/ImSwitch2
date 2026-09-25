"""``api.imcontrol.setRecFileFormat`` / ``getRecFileFormat``.

A script that reads back its own recording has to know the file format, and
until now only the snap format was reachable from a script: the recording
format was whatever the Recording widget last showed.
"""

import types

import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcontrol.controller.controllers.RecordingController import (  # noqa: E402
    RecordingController,
)
from imswitch.imcontrol.model import Options  # noqa: E402
from imswitch.imcontrol.view.widgets.RecordingWidget import RecordingWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def controller(qapp):
    widget = RecordingWidget(options=Options(setupFileName="mock.json"))
    yield types.SimpleNamespace(_widget=widget)
    widget.deleteLater()


@pytest.mark.parametrize("name, expected", [("zarr", "ZARR"), ("TIFF", "TIFF"), ("hdf5", "HDF5")])
def test_set_then_get_round_trips_whatever_the_case(controller, name, expected):
    RecordingController.setRecFileFormat(controller, name)
    assert RecordingController.getRecFileFormat(controller) == expected
    assert controller._widget.saveFormatList.currentText() == expected


def test_an_unknown_format_is_refused_and_changes_nothing(controller):
    before = RecordingController.getRecFileFormat(controller)
    with pytest.raises(ValueError, match="HDF5, TIFF or ZARR"):
        RecordingController.setRecFileFormat(controller, "png")
    assert RecordingController.getRecFileFormat(controller) == before


def test_a_locked_format_is_not_overridden(controller):
    # Snaps sent to the image display lock the recording format to TIFF
    # (RecordingController.snapSaveModeChanged); the API must not undo that.
    controller._widget.setsaveFormat(2)          # TIFF
    controller._widget.setsaveFormatEnabled(False)
    with pytest.raises(RuntimeError, match="fixed to TIFF"):
        RecordingController.setRecFileFormat(controller, "HDF5")
    assert RecordingController.getRecFileFormat(controller) == "TIFF"
