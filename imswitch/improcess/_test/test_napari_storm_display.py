"""Tests for the retained napari-storm display adapter.

The lifecycle is the contract that matters — open once, update in place, hide
on deselect, close only on removal, and never reuse a dataset id. It is
verified against a recording stand-in for the renderer so it runs without a GL
context, and separately against napari-storm's own ``NullRenderer`` so the
calls are known to be ones the real interface accepts.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.view.NapariStormDisplay import NapariStormDisplay

storm_core = pytest.importorskip(
    "napari_storm.core", reason="optional 'storm' extra not installed"
)


class _RecordingRenderer:
    """Records the renderer calls the adapter makes."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.open_ids: list[int] = []
        self.closed_ids: list[int] = []

    def open(self, dataset_id, request):
        self.calls.append(("open", dataset_id))
        self.open_ids.append(dataset_id)

    def update(self, dataset_id, request):
        self.calls.append(("update", dataset_id))

    def set_appearance(self, dataset_id, appearance):
        self.calls.append(("visible", dataset_id, appearance.visible))

    def close(self, dataset_id):
        self.calls.append(("close", dataset_id))
        self.closed_ids.append(dataset_id)

    def close_all(self):
        self.calls.append(("close_all",))


class _FakeViewer:
    def __init__(self):
        self.dims = SimpleNamespace(ndisplay=2)


def _result(name="locs", *, count=64, with_z=False, precision=True):
    rng = np.random.default_rng(4)
    columns = {
        "frame": np.arange(count),
        "x_nm": rng.uniform(0, 10000, count),
        "y_nm": rng.uniform(0, 10000, count),
        "sigma_x_nm": np.full(count, 140.0),
        "sigma_y_nm": np.full(count, 150.0),
        "photons": rng.uniform(500, 2000, count),
    }
    if precision:
        columns["lp_x_nm"] = np.full(count, 12.0)
        columns["lp_y_nm"] = np.full(count, 13.0)
    if with_z:
        columns["z_nm"] = rng.uniform(-300, 300, count)
        columns["sigma_z_nm"] = np.full(count, 300.0)
        if precision:
            columns["lp_z_nm"] = np.full(count, 30.0)
    return LocalizationResult(
        name,
        localizations_from_columns(columns),
        pixel_size_nm=100.0,
        dims="3D" if with_z else "2D",
    )


@pytest.fixture
def display():
    viewer = _FakeViewer()
    adapter = NapariStormDisplay(viewer)
    renderer = _RecordingRenderer()
    adapter._renderer = renderer          # skip GL probing
    adapter._load()                       # real core, real planner
    adapter.renderer = renderer
    adapter.viewer = viewer
    return adapter


# -- lifecycle --------------------------------------------------------------


def test_first_show_opens_a_dataset(display):
    assert display.show(_result()) is True
    assert display.renderer.open_ids == [1]


def test_showing_the_same_result_again_reveals_rather_than_reopens(display):
    result = _result()
    display.show(result)
    display.renderer.calls.clear()

    display.show(result)

    assert not any(call[0] == "open" for call in display.renderer.calls)
    assert ("visible", 1, True) in display.renderer.calls


def test_switching_results_hides_rather_than_closes(display):
    first, second = _result("a"), _result("b")
    display.show(first)
    display.show(second)

    assert display.renderer.open_ids == [1, 2]
    assert display.renderer.closed_ids == []
    assert ("visible", 1, False) in display.renderer.calls


def test_hide_does_not_close(display):
    display.show(_result())
    display.hide()

    assert display.renderer.closed_ids == []
    assert ("visible", 1, False) in display.renderer.calls


def test_update_keeps_the_dataset_and_its_resources(display):
    """The acquisition path: open once, update as the table grows."""
    result = _result(count=32)
    display.show(result)
    display.renderer.calls.clear()

    assert display.update(result) is True
    assert ("update", 1) in display.renderer.calls
    assert not any(call[0] == "open" for call in display.renderer.calls)


def test_update_on_an_unopened_result_opens_it(display):
    assert display.update(_result()) is True
    assert display.renderer.open_ids == [1]


def test_removing_a_result_closes_its_dataset(display):
    kept, removed = _result("kept"), _result("removed")
    display.show(kept)
    display.show(removed)

    display.retain_only([kept])

    assert display.renderer.closed_ids == [2]


def test_ids_are_never_reused(display):
    """A recycled id is how a stale handle gets mistaken for a live one."""
    first = _result("first")
    display.show(first)
    display.retain_only([])
    assert display.renderer.closed_ids == [1]

    display.show(_result("second"))

    assert display.renderer.open_ids == [1, 2]


def test_close_all_releases_everything(display):
    display.show(_result("a"))
    display.show(_result("b"))

    display.close_all()

    assert sorted(display.renderer.closed_ids) == [1, 2]
    assert ("close_all",) in display.renderer.calls


def test_a_recycled_object_id_does_not_return_a_stale_layer(display):
    """id() is reused after a collection; the map must notice.

    A new result landing on a dead result's id must not be handed the dead
    one's layer, which is the same stale-handle hazard that makes dataset ids
    non-reusable in the first place.
    """
    original = _result("first")
    display.show(original)
    stale_entry = display._datasets[id(original)]

    replacement = _result("replacement")
    # The collected original's id is now the replacement's, so the map points
    # at an entry whose weak reference no longer resolves to it.
    display._datasets.pop(id(original))
    display._datasets[id(replacement)] = stale_entry

    assert display._entry(replacement) is None
    # ...and the stale dataset was released rather than left dangling.
    assert stale_entry.dataset_id in display.renderer.closed_ids


# -- what gets declared -----------------------------------------------------


def test_three_dimensional_results_switch_the_canvas(display):
    display.show(_result(with_z=True))
    assert display._viewer.dims.ndisplay == 3


def test_two_dimensional_results_leave_the_canvas_alone(display):
    display.show(_result(with_z=False))
    assert display._viewer.dims.ndisplay == 2


def test_an_unfitted_axial_width_is_not_declared_as_real(display):
    """Our 2D localizer zero-fills sigma_z; declaring it would raise."""
    result = _result(with_z=True, precision=False)
    result.locs.sigma_z_nm[:] = 0.0

    traits = display._traits(result)

    assert traits.zdim_present is True
    assert traits.sigma_present is False
    # Still renderable, because photons carry the uncertainty instead.
    assert display.show(result) is True


def test_a_table_with_nothing_usable_still_renders_at_a_fixed_width(display):
    result = _result(precision=False)
    result.locs.sigma_x_nm[:] = 0.0
    result.locs.sigma_y_nm[:] = 0.0
    result.locs.photons[:] = 0.0

    traits = display._traits(result)
    settings = display._settings(traits)

    assert traits.uncertainty_defined is False
    assert settings.mode == 0
    assert display.show(result) is True


# -- degradation ------------------------------------------------------------


def test_an_unrenderable_result_falls_back_rather_than_raising(display):
    class _Exploding(_RecordingRenderer):
        def open(self, dataset_id, request):
            raise RuntimeError("no GL for you")

    display._renderer = display.renderer = _Exploding()

    assert display.show(_result()) is False


def test_a_missing_package_reports_itself_unavailable():
    adapter = NapariStormDisplay(_FakeViewer())
    adapter._import_failed = True

    assert adapter.importable is False
    assert adapter.show(_result()) is False


# -- against the real interface ---------------------------------------------


def test_the_call_sequence_is_one_the_real_renderer_accepts():
    """NullRenderer implements the true interface without needing a GL context."""
    adapter = NapariStormDisplay(_FakeViewer())
    adapter._load()
    adapter._renderer = storm_core.NullRenderer()

    result = _result(with_z=True)
    assert adapter.show(result) is True
    assert adapter.update(result) is True
    adapter.hide()
    adapter.retain_only([])
    adapter.close_all()
