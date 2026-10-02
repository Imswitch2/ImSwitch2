"""PSF panel: the bead preview, the post-fit report and the collapsed form.

A stand-in viewer records what the panel draws; no napari needed.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.processors.psf_resolution._params import CALIBRATION_GROUP  # noqa: E402
from imswitch.improcess.view.PSFResolutionWidget import PREVIEW_LAYER_NAME, PSFResolutionWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Layers(list):
    def __init__(self):
        super().__init__()
        self.selection = SimpleNamespace(active=None)


class _Viewer:
    """Just enough of a napari viewer for the panel's preview."""

    def __init__(self):
        self.layers = _Layers()
        self.points_kwargs = None

    def add_points(self, data, **kwargs):
        self.points_kwargs = kwargs
        layer = SimpleNamespace(name=kwargs["name"], data=np.asarray(data), metadata=kwargs.get("metadata", {}),
                                selected_data=set())
        self.layers.append(layer)
        return layer


def _field(scaled=True):
    rng = np.random.default_rng(2)
    yy, xx = np.mgrid[:150, :150]
    image = np.full((150, 150), 100.0)
    for cy, cx in [(30, 30), (30, 90), (75, 60), (110, 30), (110, 110), (75, 128), (32, 36)]:
        image += 900.0 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 1.8**2))
    image = rng.poisson(image).astype(np.float32)
    kwargs = {"axis_scales": [0.065, 0.065], "scale_unit": "um"} if scaled else {}
    return ArrayProcessingResult("beads", image, ["Y", "X"], **kwargs)


def _panel_on(result):
    viewer = _Viewer()
    image_layer = SimpleNamespace(
        name=result.name, data=np.asarray(result.data), scale=(0.065, 0.065), translate=(0.0, 0.0), ndim=2,
        visible=True, metadata={"result_uid": result.result_uid, "view_mode": None},
    )
    viewer.layers.append(image_layer)
    panel = PSFResolutionWidget(napariViewer=viewer)
    panel.setCurrentResult(result)
    return panel, viewer


def test_preview_marks_every_candidate_with_its_reason(qapp):
    result = _field()
    panel, viewer = _panel_on(result)
    assert panel.previewButton.isEnabled()

    panel.preview()

    run = panel._previewRun
    assert run is not None and run.mask.any()
    preview = [layer for layer in viewer.layers if layer.name == PREVIEW_LAYER_NAME]
    assert len(preview) == 1 and len(preview[0].data) == len(run.analysis.beads)
    kwargs = viewer.points_kwargs
    assert kwargs["scale"] == (0.065, 0.065)
    reasons = kwargs["features"]["reason"]
    assert "selected" in reasons and "crowded" in reasons  # (30, 30) and (32, 36) sit together
    assert panel.beadTable.rowCount() == len(run.analysis.beads)
    assert panel.beadTable.item(0, 1).text() == "selected"  # selected beads first
    assert "candidates selected" in panel.summaryLabel.text()

    panel.clearPreview()
    assert not [layer for layer in viewer.layers if layer.name == PREVIEW_LAYER_NAME]
    assert panel.beadTable.rowCount() == 0


def test_a_new_result_drops_the_preview(qapp):
    panel, viewer = _panel_on(_field())
    panel.preview()
    panel.setCurrentResult(_field())
    assert not [layer for layer in viewer.layers if layer.name == PREVIEW_LAYER_NAME]


def test_fit_reports_the_result_in_the_panel(qapp):
    result = _field()
    panel, _viewer = _panel_on(result)

    def controller(source, params):  # what ResultProcessorController does, synchronously
        output = panel.processor.apply(source, params)
        for produced in output.results:
            panel.setCurrentResult(produced)
        panel.setStatusText(f"Created {len(output.results)} results.")

    panel.sigRunRequested.connect(controller)
    panel.run()

    text = panel.summaryLabel.text()
    assert "candidates selected" in text and "FWHM, half maximum" in text
    assert text.endswith("results.")


def test_form_opens_only_the_essentials(qapp):
    panel, _viewer = _panel_on(_field())
    sections = panel.form.sections
    assert sections["Beads"].isExpanded() and sections["Optics"].isExpanded() and sections["Outputs"].isExpanded()
    for name in ("Selection", CALIBRATION_GROUP, "Advanced"):
        assert not sections[name].isExpanded(), name
    assert "from metadata" in panel.dataLabel.text()


def test_uncalibrated_data_opens_the_calibration_override(qapp):
    panel, _viewer = _panel_on(_field(scaled=False))
    assert panel.form.sections[CALIBRATION_GROUP].isExpanded()
    assert "no calibration" in panel.dataLabel.text()


def test_the_panel_says_what_an_aberration_fit_still_needs(qapp):
    stack = ArrayProcessingResult("stack", np.zeros((5, 30, 30), np.float32), ["Z", "Y", "X"],
                                  axis_scales=[0.2, 0.1, 0.1], scale_unit="um")
    panel = PSFResolutionWidget(napariViewer=None)
    panel.setCurrentResult(stack)
    assert panel.requirementLabel.isHidden()
    panel.form.controls["fit_aberrations"].setChecked(True)
    assert not panel.requirementLabel.isHidden()
    assert "NA" in panel.requirementLabel.text()
    assert "e0a030" in panel.form.controls["na"].styleSheet()
    panel.form.set_values({"na": 1.0, "wavelength_nm": 515.0})
    assert panel.requirementLabel.isHidden()
    assert panel.form.controls["na"].styleSheet() == ""
