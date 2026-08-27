"""Tests for method-dependent parameter visibility and sweep choices."""

import pytest

pytest.importorskip("PyQt5")
from qtpy import QtWidgets

from imswitch.improcess.reconstructors.monalisa.params_widget import (
    MonalisaParamsWidget,
)


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _visible(root, *names):
    param = root
    for name in names:
        param = param.param(name)
    return bool(param.opts.get("visible", True))


def _set_method(widget, method):
    widget.p.param("Reconstruction method").setValue(method)


def test_fast_gauss_shows_only_its_groups(qapp):
    widget = MonalisaParamsWidget()
    root = widget.p

    assert _visible(root, "Fast Gauss options")
    assert not _visible(root, "ISM reassignment options")
    assert _visible(root, "Parameter sweep")
    assert not _visible(root, "Auto-detect scan orientation")
    assert _visible(root, "Reconstruction options", "BG modelling")

    choices = root.param("Parameter sweep").param("Sweep parameter").opts["limits"]
    assert "Pinhole radius (×σ)" in choices
    assert "ISM shift" not in choices


def test_ism_reassignment_switches_groups_and_sweep_choices(qapp):
    widget = MonalisaParamsWidget()
    _set_method(widget, "ISM reassignment")
    root = widget.p

    assert not _visible(root, "Fast Gauss options")
    assert _visible(root, "ISM reassignment options")
    assert _visible(root, "Parameter sweep")
    # The fast-Gauss constant-background fit does not apply here.
    assert not _visible(root, "Reconstruction options", "BG modelling")

    sweep_parameter = root.param("Parameter sweep").param("Sweep parameter")
    choices = sweep_parameter.opts["limits"]
    assert set(choices) == {"ISM shift", "Oversampling", "PSF FWHM (nm)"}
    # The previously selected fast-Gauss parameter is invalid now; the value
    # must have moved onto a valid choice.
    assert sweep_parameter.value() in choices


def test_classic_method_hides_sweep_and_shows_orientation_toggle(qapp):
    widget = MonalisaParamsWidget()
    _set_method(widget, "MoNaLISA")
    root = widget.p

    assert not _visible(root, "Fast Gauss options")
    assert not _visible(root, "ISM reassignment options")
    assert not _visible(root, "Parameter sweep")
    assert _visible(root, "Auto-detect scan orientation")


def test_get_values_returns_all_keys_regardless_of_visibility(qapp):
    widget = MonalisaParamsWidget()
    _set_method(widget, "ISM reassignment")
    values = widget.get_values()

    # Hidden groups still contribute their keys, so switching methods never
    # changes the params-dict contract.
    for key in (
        "fast_gauss_pinhole_radius_sigma",
        "ism_reassign_shift",
        "ism_reassign_oversampling",
        "sweep_enabled",
        "sweep_parameter",
        "sweep_values_text",
    ):
        assert key in values
    assert "ism_reassignment_factor" not in values
    assert "ism_background" not in values
