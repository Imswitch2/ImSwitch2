"""PSF-resolution and colocalization panels conform to the run->publish contract.

Phase 4 of result unification: measurement panels no longer compute on raw
napari layers and display locally; they emit sigRunRequested with the
selected result and processor-contract params, and ResultProcessorController
publishes the outcome. These tests also run the real processors on the
panel-emitted params, so a drift between panel keys and processor contract
(the Phase 2 segmentation bug) fails loudly.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Direct import to avoid the view package import chain (matplotlib/napari).
_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from ColocalizationWidget import ColocalizationWidget
    from PSFResolutionWidget import PSFResolutionWidget
finally:
    sys.path.pop(0)

from imswitch.improcess.analysis.roi_manager import ROIRecord
from imswitch.improcess.model import ProcessingResult
from imswitch.improcess.processors.colocalization import (
    ColocalizationProcessor,
    ColocalizationResult,
)
from imswitch.improcess.processors.psf_resolution import (
    PSFResolutionProcessor,
    PSFResolutionResult,
)


class MinimalResult(ProcessingResult):
    def save(self, path, fmt):
        pass


def _gaussian(shape=(24, 24), center=(10.0, 12.0), sigma=(1.5, 2.0)):
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    return 5.0 + 80.0 * np.exp(
        -(
            ((yy - center[0]) ** 2) / (2.0 * sigma[0] ** 2)
            + ((xx - center[1]) ** 2) / (2.0 * sigma[1] ** 2)
        )
    )


def _psf_result():
    return MinimalResult(name="beads", data=_gaussian(), axis_labels=["Y", "X"])


def _coloc_result():
    y, x = np.mgrid[:8, :8]
    a = (y + x).astype(float)
    return MinimalResult(
        name="channels",
        data=np.stack([a, 3.0 * a], axis=0),
        axis_labels=["C", "Y", "X"],
    )


def _roi_manager(rois):
    return SimpleNamespace(rois=lambda: rois)


def _capture_runs(panel):
    runs = []
    panel.sigRunRequested.connect(lambda result, params: runs.append((result, params)))
    return runs


# --- PSF resolution panel ---


def test_psf_panel_emits_processor_contract_params(qtbot):
    panel = PSFResolutionWidget(napariViewer=None)
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)
    result = _psf_result()
    panel.setCurrentResult(result)

    panel.run()

    assert len(runs) == 1
    emitted_result, params = runs[0]
    assert emitted_result is result
    assert isinstance(panel.processor, PSFResolutionProcessor)
    # End-to-end: the real processor must accept the panel's params.
    psf_result = panel.processor.apply(emitted_result, params)
    assert isinstance(psf_result, PSFResolutionResult)
    assert psf_result.analysis.metadata["source"] == "full-image"


def test_psf_panel_roi_source_forwards_roi_manager_rois(qtbot):
    rois = [ROIRecord("roi-bead", "rectangle", (4, 17, 5, 20))]
    panel = PSFResolutionWidget(napariViewer=None, roiManagerWidget=_roi_manager(rois))
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)
    panel.setCurrentResult(_psf_result())
    panel.sourceCombo.setCurrentText("ROI Manager")

    panel.run()

    assert len(runs) == 1
    _, params = runs[0]
    assert params["rois"] == rois
    psf_result = panel.processor.apply(_psf_result(), params)
    assert psf_result.analysis.rows()[0]["name"] == "roi-bead"


def test_psf_panel_roi_source_requires_roi_manager(qtbot):
    panel = PSFResolutionWidget(napariViewer=None)
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)
    panel.setCurrentResult(_psf_result())
    panel.sourceCombo.setCurrentText("ROI Manager")

    panel.run()

    assert runs == []
    assert "ROI Manager" in panel.summaryLabel.text()


def test_psf_panel_requires_selected_result(qtbot):
    panel = PSFResolutionWidget(napariViewer=None)
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)

    panel.run()

    assert runs == []
    assert "No result selected" in panel.summaryLabel.text()
    assert not panel.fitButton.isEnabled()


# --- Colocalization panel ---


def test_coloc_panel_emits_processor_contract_params(qtbot):
    panel = ColocalizationWidget(napariViewer=None)
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)
    result = _coloc_result()
    panel.setCurrentResult(result)
    panel.axisCombo.setCurrentText("C")

    panel.run()

    assert len(runs) == 1
    emitted_result, params = runs[0]
    assert emitted_result is result
    assert params["compare_axis"] == "C"
    assert isinstance(panel.processor, ColocalizationProcessor)
    coloc = panel.processor.apply(emitted_result, params)
    assert isinstance(coloc, ColocalizationResult)
    assert coloc.analysis.rows()[0]["pearson"] > 0.99


def test_coloc_panel_roi_source_forwards_roi_manager_rois(qtbot):
    rois = [ROIRecord("roi-1", "rectangle", (1, 6, 1, 6))]
    panel = ColocalizationWidget(napariViewer=None, roiManagerWidget=_roi_manager(rois))
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)
    panel.setCurrentResult(_coloc_result())
    panel.sourceCombo.setCurrentText("ROI Manager")

    panel.run()

    assert len(runs) == 1
    _, params = runs[0]
    assert params["rois"] == rois
    coloc = panel.processor.apply(_coloc_result(), params)
    assert coloc.analysis.rows()[0]["name"] == "roi-1"


def test_coloc_panel_requires_selected_result(qtbot):
    panel = ColocalizationWidget(napariViewer=None)
    qtbot.addWidget(panel)
    runs = _capture_runs(panel)

    panel.run()

    assert runs == []
    assert "No result selected" in panel.summaryLabel.text()
    assert not panel.runButton.isEnabled()
