"""The tabular output channel: results -> shared Results dock.

``ProcessingResult`` exposes three rendering channels: display_layers (napari),
plot_payloads (Graph panel) and table_columns/table_records (Results dock).
This module covers the third: that table-kind results project their rows, that
the main controller routes them into the shared dock, and that results which
have nothing tabular to say are left alone.
"""

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from imswitch.improcess.analysis.colocalization import colocalization_batch
from imswitch.improcess.analysis.psf_resolution import fit_psf_batch
from imswitch.improcess.controller.ImProcessMainController import (
    ImProcessMainController,
)
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.colocalization import ColocalizationResult
from imswitch.improcess.processors.psf_resolution import PSFResolutionResult


def _gaussian(shape=(31, 33), center=(14.2, 16.7), sigma=(2.4, 3.1)):
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    return 5.0 + 80.0 * np.exp(
        -(
            ((yy - center[0]) ** 2) / (2.0 * sigma[0] ** 2)
            + ((xx - center[1]) ** 2) / (2.0 * sigma[1] ** 2)
        )
    )


# ---------------------------------------------------------------------------
# Result-side projection
# ---------------------------------------------------------------------------


def test_base_result_has_no_table_by_default():
    class _Plain(ProcessingResult):
        def save(self, path: Path, fmt: str):
            pass

    result = _Plain("plain", np.zeros((4, 4)), axis_labels=["Y", "X"])

    assert result.table_columns() == []
    assert result.table_records() == []


def test_psf_result_projects_fits_as_table_rows():
    analysis = fit_psf_batch(_gaussian(), pixel_size=100.0, unit="nm")
    result = PSFResolutionResult("psf", analysis, params={"pixel_size": 100.0})

    columns = result.table_columns()
    records = result.table_records()

    assert len(records) == len(analysis.fits)
    # Unit-scaled columns must follow the analysis unit, not a hard-coded one.
    assert "fwhm_x_nm" in columns
    assert set(records[0].keys()) == set(columns)
    assert records[0]["fwhm_x_nm"] > 0


def test_colocalization_result_projects_regions_as_table_rows():
    a = np.arange(100, dtype=float).reshape(10, 10)
    b = 2.0 * a + 5.0
    result = ColocalizationResult("coloc", colocalization_batch(a, b), params={})

    columns = result.table_columns()
    records = result.table_records()

    assert len(records) == 1
    assert "pearson" in columns
    assert set(records[0].keys()) == set(columns)


def test_table_columns_are_empty_when_analysis_has_no_rows():
    empty = SimpleNamespace(rows=lambda: [], records=[], scatter_a=np.array([]),
                            scatter_b=np.array([]), metadata={})
    result = ColocalizationResult.__new__(ColocalizationResult)
    result.analysis = empty
    result.params = {}

    assert result.table_columns() == []
    assert result.table_records() == []


# ---------------------------------------------------------------------------
# Controller routing
# ---------------------------------------------------------------------------


class _FakeView:
    def __init__(self):
        self.appended = []
        self.raised = []

    def appendResultTableRecords(self, columns, records):
        self.appended.append((list(columns), list(records)))

    def raiseDockByTitle(self, title):
        self.raised.append(title)
        return True


def _controller_with_view(view):
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = view
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    return controller


class _TableResult(ProcessingResult):
    kind = "table"

    def __init__(self, name, records, columns=None):
        super().__init__(name, np.zeros((len(records), 1)), axis_labels=["Row", "Col"])
        self._records = records
        self._columns = columns

    def table_records(self):
        return list(self._records)

    def table_columns(self):
        if self._columns is None:
            return list(self._records[0].keys()) if self._records else []
        return list(self._columns)

    def save(self, path: Path, fmt: str):
        pass


class _CurveResult(ProcessingResult):
    kind = "curve"

    def __init__(self, name, with_payload=True):
        super().__init__(name, np.zeros((3, 4)), axis_labels=["C", "Point"])
        self._with_payload = with_payload

    def plot_payloads(self):
        if not self._with_payload:
            return []
        return [
            PlotPayload(
                title="curve",
                series=[PlotSeries(name="s", y=np.arange(4, dtype=float))],
            )
        ]

    def save(self, path: Path, fmt: str):
        pass


def test_table_result_rows_go_to_the_results_dock():
    view = _FakeView()
    controller = _controller_with_view(view)
    records = [{"name": "roi-1", "pearson": 0.9}, {"name": "roi-2", "pearson": 0.4}]

    controller._routeResultToAnalysisPanels(_TableResult("coloc", records))

    assert len(view.appended) == 1
    columns, appended = view.appended[0]
    assert appended == records
    assert columns == ["name", "pearson"]


def test_table_result_with_no_rows_is_not_pushed():
    view = _FakeView()
    controller = _controller_with_view(view)

    controller._routeResultToAnalysisPanels(_TableResult("empty", []))

    assert view.appended == []


def test_table_columns_fall_back_to_record_keys():
    view = _FakeView()
    controller = _controller_with_view(view)
    records = [{"a": 1, "b": 2}]

    controller._routeResultToAnalysisPanels(_TableResult("t", records, columns=[]))

    assert view.appended[0][0] == ["a", "b"]


def test_curve_result_reveals_the_graph_dock():
    view = _FakeView()
    controller = _controller_with_view(view)

    controller._routeResultToAnalysisPanels(_CurveResult("frc"))

    assert view.raised == ["Graph"]
    assert view.appended == []


class _FittedCurveResult(_CurveResult):
    """A curve that also reports the parameters of its fit."""

    publishes_table_rows = True

    def table_columns(self):
        return ["source", "t_half_ms", "r2"]

    def table_records(self):
        return [{"source": self.name, "t_half_ms": 12.5, "r2": 0.99}]


def test_fitted_curve_publishes_its_parameters_and_shows_the_curve():
    """An analysis that fits something produces two things — the curve and
    the parameters — and the parameters are the answer. Both channels fire."""
    view = _FakeView()
    controller = _controller_with_view(view)

    controller._routeResultToAnalysisPanels(_FittedCurveResult("off-switch"))

    assert view.raised == ["Graph"]
    assert len(view.appended) == 1
    columns, records = view.appended[0]
    assert columns == ["source", "t_half_ms", "r2"]
    assert records == [{"source": "off-switch", "t_half_ms": 12.5, "r2": 0.99}]


def test_curve_rows_stay_opt_in():
    """Bulk rows (a localization table runs to six figures) must not be
    published just because the result can produce them."""
    view = _FakeView()
    controller = _controller_with_view(view)
    result = _FittedCurveResult("quiet")
    result.publishes_table_rows = False

    controller._routeResultToAnalysisPanels(result)

    assert view.appended == []
    assert view.raised == ["Graph"]


def test_curve_result_without_payloads_does_not_reveal_graph():
    view = _FakeView()
    controller = _controller_with_view(view)

    controller._routeResultToAnalysisPanels(_CurveResult("frc", with_payload=False))

    assert view.raised == []


def test_image_result_touches_neither_panel():
    """Image results often expose plot payloads too (WidefieldSTARSS); they
    must never steal focus from the reconstruction viewer."""
    view = _FakeView()
    controller = _controller_with_view(view)

    class _ImageWithPlots(ProcessingResult):
        def plot_payloads(self):
            return [PlotPayload(title="hist", series=[])]

        def save(self, path: Path, fmt: str):
            pass

    controller._routeResultToAnalysisPanels(
        _ImageWithPlots("wfs", np.zeros((4, 4)), axis_labels=["Y", "X"])
    )

    assert view.appended == []
    assert view.raised == []


def test_broken_table_projection_is_contained():
    view = _FakeView()
    controller = _controller_with_view(view)

    class _Broken(ProcessingResult):
        kind = "table"

        def table_records(self):
            raise RuntimeError("boom")

        def save(self, path: Path, fmt: str):
            pass

    controller._routeResultToAnalysisPanels(
        _Broken("broken", np.zeros((2, 2)), axis_labels=["Y", "X"])
    )

    assert view.appended == []


# ---------------------------------------------------------------------------
# View-side append + reveal
# ---------------------------------------------------------------------------


class _FakeTableWidget:
    def __init__(self):
        self.columns = []
        self.records = []

    def append_records(self, columns, records):
        for column in columns:
            if column not in self.columns:
                self.columns.append(column)
        self.records.extend(records)


def _view_stub():
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    stub = SimpleNamespace(
        resultsTableWidget=_FakeTableWidget(),
        raised=[],
    )
    stub.raiseDockByTitle = lambda title: stub.raised.append(title)
    return ImProcessMainView.appendResultTableRecords, stub


def test_view_append_accumulates_instead_of_replacing():
    """Profile pushes, ROI stats and table results share one accumulating log;
    a second push must not wipe the first."""
    append, view = _view_stub()

    append(view, ["a"], [{"a": 1}])
    append(view, ["a", "b"], [{"a": 2, "b": 3}])

    assert view.resultsTableWidget.records == [{"a": 1}, {"a": 2, "b": 3}]
    assert view.resultsTableWidget.columns == ["a", "b"]


def test_view_append_reveals_the_results_dock():
    append, view = _view_stub()

    append(view, ["a"], [{"a": 1}])

    assert view.raised == ["Results"]


def test_view_append_ignores_empty_records():
    append, view = _view_stub()

    append(view, ["a"], [])

    assert view.resultsTableWidget.records == []
    assert view.raised == []


# ---------------------------------------------------------------------------
# Graph seeding
# ---------------------------------------------------------------------------


def test_graph_controller_is_seeded_with_the_active_result():
    seeded = []
    active = object()
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: None
    )
    controller.mainViewController = SimpleNamespace(
        graphController=SimpleNamespace(
            currentResultChanged=lambda result: seeded.append(result)
        ),
        reconstructionController=SimpleNamespace(getActiveResult=lambda: active),
    )

    controller._seed_graph_controller()

    assert seeded == [active]


def test_graph_seeding_without_controller_is_a_noop():
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: None
    )
    controller.mainViewController = SimpleNamespace(
        graphController=None,
        reconstructionController=SimpleNamespace(getActiveResult=lambda: object()),
    )

    controller._seed_graph_controller()  # must not raise
