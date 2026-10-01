"""Tests for the point-cloud render controls.

Two halves, tested separately: the widget's own logic (what it enables, what it
emits) and the adapter's response (what it plans, and what it refuses to plan).

The refusals matter most. napari-storm *raises* on variable-width mode without
uncertainty and on colour-by-depth without a z axis, so a control left in a
stale state must not be able to reach the planner with one.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns

pytest.importorskip("qtpy.QtWidgets")

from imswitch.improcess.view.SmlmRenderWidget import SmlmRenderWidget  # noqa: E402

storm_core = pytest.importorskip(
    "napari_storm.core", reason="optional 'storm' extra not installed"
)

from imswitch.improcess.view.NapariStormDisplay import NapariStormDisplay  # noqa: E402


def _result(*, with_z=False, precision=True, photons=True, count=400):
    rng = np.random.default_rng(3)
    columns = {
        "frame": np.arange(count),
        "x_nm": rng.uniform(0, 10000, count),
        "y_nm": rng.uniform(0, 5000, count),
        "sigma_x_nm": np.full(count, 140.0),
        "sigma_y_nm": np.full(count, 140.0),
        "photons": np.full(count, 1000.0 if photons else 0.0),
    }
    if precision:
        columns["lp_x_nm"] = np.full(count, 12.0)
        columns["lp_y_nm"] = np.full(count, 12.0)
    if with_z:
        columns["z_nm"] = rng.uniform(-400, 400, count)
        if precision:
            columns["lp_z_nm"] = np.full(count, 30.0)
    return LocalizationResult(
        "r", localizations_from_columns(columns), pixel_size_nm=100.0,
        dims="3D" if with_z else "2D",
    )


class _Renderer:
    def __init__(self):
        self.updates = 0
        self.appearance = []

    def open(self, dataset_id, request):
        self.request = request

    def update(self, dataset_id, request):
        self.updates += 1
        self.request = request

    def set_appearance(self, dataset_id, appearance):
        self.appearance.append(appearance)

    def close(self, dataset_id):
        pass

    def close_all(self):
        pass


@pytest.fixture
def display():
    adapter = NapariStormDisplay(type("V", (), {"dims": type("D", (), {"ndisplay": 2})()})())
    adapter._renderer = _Renderer()
    adapter._load()
    return adapter


# -- the widget -------------------------------------------------------------


def test_controls_are_disabled_without_a_localization_result(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=False)
    assert not widget.isEnabled()


def test_variable_mode_is_unavailable_without_uncertainty(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=False, has_uncertainty=False)

    assert not widget._mode.model().item(1).isEnabled()
    assert widget.overrides()["mode"] == 0
    assert "only fixed width" in widget._mode.toolTip()


def test_depth_colouring_is_unavailable_without_z(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=False, has_uncertainty=True)

    assert not widget._zColor.isEnabled()
    assert widget.overrides()["z_color_encoding"] is False


def test_depth_colouring_is_unavailable_in_variable_mode(qtbot):
    """It is a fixed-mode feature; the planner refuses the combination."""
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=True, has_uncertainty=True)
    widget._mode.setCurrentIndex(widget._mode.findData(1))

    assert not widget._zColor.isEnabled()
    assert widget.overrides()["z_color_encoding"] is False


def test_changing_the_width_emits_the_new_settings(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=True, has_uncertainty=True)

    with qtbot.waitSignal(widget.sigSettingsChanged, timeout=1000) as caught:
        widget._sigmaXY.setValue(25.0)

    overrides, _ranges = caught.args
    assert overrides["fixed_sigma_xy_nm"] == pytest.approx(25.0)


def test_the_fwhm_hint_tracks_the_sigma(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget._sigmaXY.setValue(10.0)
    assert "24 nm FWHM" in widget._sigmaXYHint.text()


def test_seeding_the_controls_does_not_echo_back(qtbot):
    """Reflecting state decided elsewhere must not look like user intent."""
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    emitted = []
    widget.sigSettingsChanged.connect(lambda *a: emitted.append(a))

    widget.setValues({"mode": 0, "fixed_sigma_xy_nm": 42.0})
    widget.setResultContext(available=True, has_z=True, has_uncertainty=True)

    assert emitted == []
    assert widget.overrides()["fixed_sigma_xy_nm"] == pytest.approx(42.0)


def test_render_range_reports_only_narrowed_axes(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    assert widget.renderRange() == {}

    widget._range["x"][0].setValue(25)
    widget._range["x"][1].setValue(75)

    assert widget.renderRange() == {"x": (0.25, 0.75)}


def test_an_inverted_range_is_read_the_right_way_round(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget._range["y"][0].setValue(80)
    widget._range["y"][1].setValue(20)

    assert widget.renderRange() == {"y": (0.2, 0.8)}


def test_show_all_clears_the_range(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget._range["x"][0].setValue(30)

    with qtbot.waitSignal(widget.sigSettingsChanged, timeout=1000):
        widget.resetRange()

    assert widget.renderRange() == {}


def test_the_colormap_choice_is_never_empty(qtbot):
    """napari resolves 'no colormap' to black on the instanced backend."""
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    choices = [widget._colormap.itemText(i) for i in range(widget._colormap.count())]
    assert "" not in choices
    assert widget.appearance()["colormap"]


# -- the adapter's response -------------------------------------------------


def test_a_width_change_updates_rather_than_reopens(display):
    result = _result()
    display.show(result)
    before = display._renderer.updates

    assert display.apply_settings(result, {"mode": 0, "fixed_sigma_xy_nm": 30.0}) is True
    assert display._renderer.updates == before + 1


def test_the_chosen_fixed_width_reaches_the_plan(display):
    result = _result()
    display.show(result)
    display.apply_settings(result, {"mode": 0, "fixed_sigma_xy_nm": 30.0})

    # sigmas are normalized to the largest, and size is 5x it: a fixed 30 nm
    # sigma must therefore set the billboard edge deterministically.
    assert display._renderer.request.size == pytest.approx(150.0, rel=1e-3)


def test_variable_mode_is_refused_when_the_data_cannot_support_it(display):
    """A stale control must not be able to make the planner raise."""
    result = _result(precision=False, photons=False)
    result.locs.sigma_x_nm[:] = 0.0
    result.locs.sigma_y_nm[:] = 0.0
    display.show(result)

    settings = display._settings(display._traits(result))
    assert settings.mode == 0

    display._overrides = {"mode": 1}
    assert display._settings(display._traits(result)).mode == 0


def test_depth_colouring_is_refused_without_a_z_axis(display):
    result = _result(with_z=False)
    display._overrides = {"mode": 0, "z_color_encoding": True}

    assert display._settings(display._traits(result)).z_color_encoding is False


def test_depth_colouring_survives_when_the_data_supports_it(display):
    result = _result(with_z=True)
    display._overrides = {"mode": 0, "z_color_encoding": True}

    assert display._settings(display._traits(result)).z_color_encoding is True


def test_a_render_range_narrows_what_is_drawn(display):
    result = _result()
    display.show(result)
    full = len(display._renderer.request.coords)

    display.apply_settings(result, {}, {"x": (0.0, 0.5)})
    narrowed = len(display._renderer.request.coords)

    assert 0 < narrowed < full


def test_clearing_the_render_range_restores_everything(display):
    result = _result()
    display.show(result)
    full = len(display._renderer.request.coords)

    display.apply_settings(result, {}, {"x": (0.0, 0.5)})
    display.apply_settings(result, {}, {})

    assert len(display._renderer.request.coords) == full


def test_an_empty_render_range_is_ignored_rather_than_blanking_the_view(display):
    """The planner has nothing to normalize against an empty selection."""
    result = _result()
    display.show(result)
    full = len(display._renderer.request.coords)

    display.apply_settings(result, {}, {"x": (0.4, 0.4001), "y": (0.9, 0.9001)})

    assert len(display._renderer.request.coords) == full


def test_appearance_changes_do_not_replan(display):
    result = _result()
    display.show(result)
    before = display._renderer.updates

    assert display.set_appearance(result, colormap="green", opacity=0.5) is True
    assert display._renderer.updates == before
    assert display._renderer.appearance[-1].colormap == "green"


# -- depth colouring --------------------------------------------------------


def test_depth_colouring_switches_to_a_hue_colormap(qtbot):
    """On a grey ramp, depth reads as brightness and looks like a no-op."""
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=True, has_uncertainty=True)
    widget._mode.setCurrentIndex(widget._mode.findData(0))
    assert widget.appearance()["colormap"] == "gray"

    widget._zColor.setChecked(True)

    assert widget.appearance()["colormap"] == "hsv"


def test_switching_depth_colouring_off_restores_the_previous_colormap(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=True, has_uncertainty=True)
    widget._mode.setCurrentIndex(widget._mode.findData(0))
    widget._colormap.setCurrentText("green")

    widget._zColor.setChecked(True)
    assert widget.appearance()["colormap"] == "hsv"

    widget._zColor.setChecked(False)
    assert widget.appearance()["colormap"] == "green"


def test_a_hue_colormap_the_user_chose_is_left_alone(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=True, has_uncertainty=True)
    widget._mode.setCurrentIndex(widget._mode.findData(0))
    widget._colormap.setCurrentText("turbo")

    widget._zColor.setChecked(True)

    assert widget.appearance()["colormap"] == "turbo"


def test_hsv_is_offered_at_all(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    choices = [widget._colormap.itemText(i) for i in range(widget._colormap.count())]
    assert "hsv" in choices


def test_depth_colouring_reaches_the_plan_as_depth(display):
    """The rendered values must be the z coordinate, not the intensity."""
    result = _result(with_z=True)
    display.show(result)
    display.apply_settings(result, {"mode": 0, "z_color_encoding": True})

    values = np.asarray(display._renderer.request.values)
    z = np.asarray(result.locs.z_nm, dtype=float)
    # Normalized depth: perfectly rank-correlated with z, unlike intensity.
    assert np.corrcoef(values, z)[0, 1] == pytest.approx(1.0, abs=1e-6)


# -- mode seeding -----------------------------------------------------------


def test_an_untouched_panel_follows_the_renderer_default(qtbot):
    """Otherwise the panel claims 'fixed' while the canvas shows variable."""
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)

    widget.setResultContext(available=True, has_z=False, has_uncertainty=True)

    assert widget.overrides()["mode"] == 1


def test_a_users_choice_survives_a_result_change(qtbot):
    widget = SmlmRenderWidget()
    qtbot.addWidget(widget)
    widget.setResultContext(available=True, has_z=False, has_uncertainty=True)
    widget._mode.setCurrentIndex(widget._mode.findData(0))  # user picks fixed

    widget.setResultContext(available=True, has_z=False, has_uncertainty=True)

    assert widget.overrides()["mode"] == 0
