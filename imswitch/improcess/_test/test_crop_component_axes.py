"""Crop/Substack on a result whose component has other axes than the whole result.

MoNaLISA's whole result is (Dataset, Base, T, Z, Y, X) while its ``signal``
component is (Dataset, T, Z, Y, X). The panel's table is built for the whole
result, so a range carrying only an axis *index* pointed at the wrong axis of
the component (or past its rank). Ranges now carry the axis label too, and a
range that cannot fit says so instead of raising a bare ``ValueError``.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.processors.base import ProcessorInputError
from imswitch.improcess.processors.run import run_batch
from imswitch.improcess.processors.stack_subset.processor import (
    StackSubsetProcessor,
    normalize_subset_ranges,
)
from imswitch.improcess.view.StackSubsetDialog import StackSubsetRangesWidget

NM_PER_PX = 35.0
ROWS, COLS = 40, 50


class _TwoGridResult(ArrayProcessingResult):
    """A whole result with a Base axis and one component without it, at its own scale."""

    def display_layers(self):
        return [
            DisplayLayerSpec(
                name="signal",
                data=np.asarray(self.data)[:, 0],
                axis_labels=["Dataset", "T", "Z", "Y", "X"],
                axis_scales=[1.0, 1.0, 1.0, NM_PER_PX, NM_PER_PX],
                scale_unit="nm",
                component="signal",
            )
        ]


@pytest.fixture
def whole():
    data = np.random.default_rng(3).random((1, 1, 1, 1, ROWS, COLS)).astype(np.float32)
    return _TwoGridResult(
        "rec", data, ["Dataset", "Base", "T", "Z", "Y", "X"],
        axis_scales=[1.0, 1.0, 1.0, 1.0, NM_PER_PX, NM_PER_PX], scale_unit="nm",
    )


@pytest.fixture
def widget(qtbot, whole):
    roi = ROIRecord("cell", "rectangle", (5, 15, 8, 30), uid="u1")
    panel = StackSubsetRangesWidget(whole, rois=[roi])
    qtbot.addWidget(panel)
    panel.applyROI(roi)
    return panel


def test_ranges_filled_for_the_whole_result_crop_the_component_by_label(widget, whole):
    params = widget.selected_params()
    component = whole.processor_input_choices()[1].result

    out = StackSubsetProcessor().apply(component, params)

    assert out.data.shape == (1, 1, 1, 10, 22)
    assert np.array_equal(out.data, np.asarray(component.data)[..., 5:15, 8:30])


def test_the_whole_result_is_cropped_to_the_same_pixels(widget, whole):
    out = StackSubsetProcessor().apply(whole, widget.selected_params())

    assert out.data.shape == (1, 1, 1, 1, 10, 22)


def test_ranges_written_by_index_alone_still_work():
    ranges = normalize_subset_ranges(
        [{"axis": 1, "start": 2, "stop": 4}], labels=["Z", "Y", "X"], shape=(3, 8, 9)
    )
    assert ranges[0]["axis"] == 1


def test_a_range_that_does_not_fit_says_which_image_and_why():
    with pytest.raises(ProcessorInputError, match=r"axis Y.*'small'.*32 pixels"):
        normalize_subset_ranges(
            [{"axis": 0, "label": "Y", "start": 425, "stop": 551}],
            labels=["Y", "X"], shape=(32, 32), input_name="small",
        )


def test_a_label_the_image_lacks_is_a_clear_error():
    with pytest.raises(ProcessorInputError, match="no axis 'Base'"):
        normalize_subset_ranges(
            [{"axis": 1, "label": "Base", "start": 0, "stop": 1}],
            labels=["Y", "X"], shape=(32, 32),
        )


def test_the_runner_reports_it_as_a_failure_without_a_traceback(caplog):
    small = ArrayProcessingResult("small", np.zeros((32, 32), np.float32), ["Y", "X"])
    params = {"ranges": [{"axis": 0, "label": "Y", "start": 425, "stop": 551}]}
    logger = logging.getLogger("crop-test")

    with caplog.at_level(logging.WARNING, logger="crop-test"):
        results, failures = run_batch(StackSubsetProcessor(), [small], params, logger)

    assert results == [] and "32 pixels" in failures[0][1]
    assert all(record.exc_info is None for record in caplog.records)


def test_an_roi_off_the_image_is_refused_with_a_note_not_clipped_to_a_sliver(qtbot):
    small = ArrayProcessingResult("small", np.zeros((32, 32), np.float32), ["Y", "X"])
    far = ROIRecord("far", "rectangle", (425, 551, 300, 420), uid="u2")
    panel = StackSubsetRangesWidget(small, rois=[far])
    qtbot.addWidget(panel)
    panel.show()

    assert panel.applyROI(far) is False

    assert panel.selected_params()["ranges"] == []
    assert panel.roiNote.isVisible() and "outside this image" in panel.roiNote.text()
    panel.setResult(small, [far])
    assert not panel.roiNote.isVisible()


def test_an_roi_drawn_after_the_panel_opened_is_offered_by_its_chooser(qtbot, whole):
    from qtpy import QtCore

    from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget

    class Manager(QtCore.QObject):
        sigStateChanged = QtCore.Signal()

        def __init__(self):
            super().__init__()
            self.records = []

        def rois(self, visible_only=False):
            return list(self.records)

    manager = Manager()
    panel = ResultProcessorWidget(StackSubsetProcessor())
    qtbot.addWidget(panel)
    panel.setCurrentResult(whole)
    panel.setROIManagerWidget(manager)
    panel.setROIManagerWidget(manager)          # wired again by a later panel: no double listener
    assert panel.paramWidget.roiCombo.count() == 1

    manager.records.append(ROIRecord("late", "rectangle", (5, 15, 8, 30), uid="u9", visible=True))
    manager.sigStateChanged.emit()

    assert panel.paramWidget.roiCombo.count() == 2
