"""An interrupted recording must not look like a complete one.

A scan stopped part way through is a normal outcome, and the acquisition
layout is what makes it detectable: the layout says how many positions were
planned, the container says how many frames it holds, and the difference is
the answer. Every place that answer could have been given was dropping it.

* The source inspection knew, and had nowhere to put it. Exactly one
  reconstructor's parameter widget implements the display hook, and the
  default view-only reconstructor's does not, so the whole inspection was
  assembled and discarded -- a four-of-six-position scan opened as an
  ordinary four-frame stack.
* The SMLM selection range-checked against the *layout*, so a selection its
  own validator accepted indexed past the end of the stored array and came
  out as a bare numpy ``IndexError``.
* A file stopped before its first frame carried nothing at all: not the
  detector, not the outcome. TIFF was worse than nothing, inventing a dataset
  name and then refusing to open it.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    LayoutIssue,
    TraversalRule,
)
from imswitch.improcess.reconstructors.base import SourceInspection

ROWS, COLS = 2, 3
PLANNED = ROWS * COLS
RECORDED = 4


def _layout():
    loops = (
        AcquisitionLoop('scan_y', 'scan_y', ROWS, step=1.0, unit='um'),
        AcquisitionLoop('scan_x', 'scan_x', COLS, step=1.0, unit='um'),
    )
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector='Camera',
        storage_axes=('frame', 'detector_y', 'detector_x'),
        event_loops=loops,
        traversal=tuple(TraversalRule(loop.id, 'forward') for loop in loops),
        provenance='recorded',
    )


class _Dock:
    """The Parameters dock, reduced to the two things this exercises."""

    def __init__(self):
        self.shown = []
        self.parTree = object()

    def setSourceInspection(self, summary, severity='warning'):
        self.shown.append((summary, severity))


def _controller(dock):
    from imswitch.improcess.controller.ReconstructorManagerController import (
        ReconstructorManagerController,
    )

    controller = ReconstructorManagerController.__new__(ReconstructorManagerController)
    controller._widget = dock
    controller._logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    return controller


def test_an_incomplete_recording_is_reported_in_the_parameters_dock():
    dock = _Dock()
    inspection = SourceInspection(
        source_kind='image',
        issues=(
            LayoutIssue(
                'warning', 'FRAME_COUNT_MISMATCH',
                f'Layout selects {PLANNED} frames but source contains {RECORDED}',
                'shape',
            ),
        ),
    )

    _controller(dock)._showSourceInspection(inspection)

    summary, severity = dock.shown[-1]
    assert str(PLANNED) in summary and str(RECORDED) in summary
    assert severity == 'warning'
    # Said once. ``warning`` is derived from the issues, so reporting both
    # printed every message twice.
    assert summary.count('Layout selects') == 1


def test_an_error_level_issue_is_reported_as_one():
    dock = _Dock()

    _controller(dock)._showSourceInspection(SourceInspection(
        source_kind='image',
        issues=(LayoutIssue('error', 'STORAGE_RANK_MISMATCH', 'rank disagrees', 'shape'),),
    ))

    assert dock.shown[-1][1] == 'error'


def test_a_clean_source_clears_the_banner():
    dock = _Dock()

    _controller(dock)._showSourceInspection(SourceInspection(source_kind='image'))

    assert dock.shown[-1][0] == ''


class _StoppedEarlySource:
    """A data object whose layout plans more positions than the file holds."""

    def __init__(self):
        self.acquisition_layout = SimpleNamespace(
            layout=_layout(), is_authoritative=True, is_usable=True
        )


def test_selecting_a_position_that_was_never_recorded_says_so():
    from imswitch.improcess.reconstructors.smlm.localizer import SmlmLocalizer

    data = np.zeros((RECORDED, 4, 4), dtype=np.uint16)

    with pytest.raises(ValueError) as error:
        SmlmLocalizer()._select_frame_stream(
            _StoppedEarlySource(), data,
            {'loop_selection': {'scan_y': 1, 'scan_x': 2}},
        )

    message = str(error.value)
    assert 'stopped' in message
    assert str(RECORDED) in message
    # Not a numpy message about an axis.
    assert 'out of bounds' not in message


def test_a_recorded_position_still_selects():
    from imswitch.improcess.reconstructors.smlm.localizer import SmlmLocalizer

    data = np.arange(RECORDED * 4 * 4, dtype=np.uint16).reshape(RECORDED, 4, 4)

    frames, selection = SmlmLocalizer()._select_frame_stream(
        _StoppedEarlySource(), data,
        {'loop_selection': {'scan_y': 0, 'scan_x': 1}},
    )

    assert frames.shape == (1, 4, 4)
    assert selection == {'scan_y': 0, 'scan_x': 1}


def test_a_page_less_tiff_reports_no_datasets_rather_than_inventing_one(tmp_path):
    import tifffile

    from imswitch.improcess.model import DataObj

    path = tmp_path / 'stopped_Camera.ome.tiff'
    with tifffile.TiffWriter(str(path), bigtiff=True):
        pass

    with pytest.raises(RuntimeError, match='does not contain any datasets'):
        DataObj.getDatasetNames(str(path))
