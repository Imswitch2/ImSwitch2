"""``api.imcontrol.setSnapModeSave`` selects the snap format it names.

The combo box holds 'HDF5', 'TIFF' and 'ZARR', and the API passed the name to
QComboBox.setCurrentText, which matches case-sensitively and does nothing
without a match -- so the documented default, ``setSnapModeSave()`` (i.e.
"tiff"), was silently ignored and snaps stayed HDF5.
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
def widget(qapp):
    widget = RecordingWidget(options=Options(setupFileName="mock.json"))
    yield widget
    widget.deleteLater()


@pytest.mark.parametrize("name, expected", [("tiff", "TIFF"), ("TIFF", "TIFF"),
                                            ("zarr", "ZARR"), ("Hdf5", "HDF5")])
def test_api_selects_the_format_whatever_the_case(widget, name, expected):
    RecordingController.setSnapModeSave(types.SimpleNamespace(_widget=widget), name)
    assert widget.saveSnapFormatList.currentText() == expected


def test_the_default_is_tiff(widget):
    RecordingController.setSnapModeSave(types.SimpleNamespace(_widget=widget))
    assert widget.saveSnapFormatList.currentText() == "TIFF"


def test_an_unknown_format_is_refused_and_changes_nothing(widget):
    before = widget.saveSnapFormatList.currentText()
    with pytest.raises(ValueError, match="HDF5, TIFF or ZARR"):
        RecordingController.setSnapModeSave(types.SimpleNamespace(_widget=widget), "png")
    assert widget.saveSnapFormatList.currentText() == before
