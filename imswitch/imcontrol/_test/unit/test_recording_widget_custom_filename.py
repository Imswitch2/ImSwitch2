"""``api.imcontrol.setRecFilename`` names the recording it promises to.

The widget only reads the typed name while "Specify file name" is ticked, and
``setCustomFilename`` filled in the name without ticking it -- so the API call
was silently ignored and every recording kept its time-based name.
"""

import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcontrol.model import Options  # noqa: E402
from imswitch.imcontrol.view.widgets.RecordingWidget import RecordingWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def widget(qapp):
    widget = RecordingWidget(options=Options(setupFileName="mock.json"))
    # RecordingController makes this connection; the checkbox drives the edit.
    widget.sigSpecFileToggled.connect(widget.setCustomFilenameEnabled)
    yield widget
    widget.deleteLater()


def test_set_custom_filename_is_the_name_recordings_use(widget):
    widget.setCustomFilename("bead_scan")
    assert widget.getCustomFilename() == "bead_scan"
    assert widget.specifyfile.isChecked()


def test_renaming_keeps_the_new_name(widget):
    widget.setCustomFilename("first")
    widget.setCustomFilename("second")
    assert widget.getCustomFilename() == "second"


def test_clear_returns_to_time_based_names(widget):
    widget.setCustomFilename("bead_scan")
    widget.clearCustomFilename()
    assert widget.getCustomFilename() is None
    assert not widget.specifyfile.isChecked()
    assert not widget.filenameEdit.isEnabled()
