"""Phase 2 tests: opening a localization file routes it to the results list.

The controller is exercised through ``_loadFromPath`` with stand-ins for the
main controller and widget, so the routing decision is tested without a Qt
event loop or a real ImProcess window.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_export import export_picasso_hdf5
from imswitch.improcess.controller.FileIOController import FileIOController
from imswitch.improcess.model.dataset_sources import (
    LOCALIZATIONS_SPEC,
    source_kind_for,
    specs_for_reconstructor,
)
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.reconstructors.smlm.localizer import SmlmLocalizer

TS_HEADER = '"id","frame","x [nm]","y [nm]","sigma [nm]","intensity [photon]"'
TS_ROWS = ["1,1,1000.0,2000.0,130.0,850.0", "2,2,3000.0,4000.0,140.0,900.0"]


class _RecordingReconstructionController:
    def __init__(self):
        self.published = []

    def resultProduced(self, result, displayName):
        self.published.append((result, displayName))


@pytest.fixture
def controller():
    """A FileIOController with only the collaborators _loadFromPath touches."""
    instance = FileIOController.__new__(FileIOController)
    recon = _RecordingReconstructionController()
    instance._main = SimpleNamespace(
        reconstructionController=recon,
        _currentDataObj=None,
        _activeReconstructor=None,
    )
    instance._logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: instance.warnings.append(a[0] if a else ""),
        error=lambda *a, **k: instance.errors.append(a[0] if a else ""),
    )
    instance.warnings = []
    instance.errors = []
    instance._recon = recon
    return instance


@pytest.fixture
def thunderstorm_csv(tmp_path):
    path = tmp_path / "locs.csv"
    path.write_text("\n".join([TS_HEADER, *TS_ROWS]) + "\n")
    return path


# -- the spec registry ------------------------------------------------------


def test_localizations_are_their_own_source_kind():
    assert source_kind_for(LOCALIZATIONS_SPEC.id) == "localizations"
    assert source_kind_for("hdf5") == "image"


def test_the_localizer_offers_localization_files_in_its_open_dialog():
    specs = specs_for_reconstructor(SmlmLocalizer)
    assert LOCALIZATIONS_SPEC in specs


def test_other_reconstructors_do_not_offer_them():
    plain = SimpleNamespace(file_extensions=["tiff"], accepted_source_kinds=("image",))
    assert LOCALIZATIONS_SPEC not in specs_for_reconstructor(plain)


# -- routing ----------------------------------------------------------------


def test_opening_a_thunderstorm_csv_publishes_a_result(controller, thunderstorm_csv):
    outcome = controller._loadFromPath(str(thunderstorm_csv))

    assert outcome == 'current'
    assert len(controller._recon.published) == 1
    result, name = controller._recon.published[0]
    assert isinstance(result, LocalizationResult)
    assert len(result) == 2
    assert name == "locs"
    np.testing.assert_allclose(result.locs.x_nm, [1000.0, 3000.0], rtol=1e-5)


def test_opening_a_picasso_hdf5_publishes_a_result(controller, tmp_path):
    """A Picasso .hdf5 must not be mistaken for an image stack."""
    locs = localizations_from_columns(
        {"x_nm": [10.0, 20.0], "y_nm": [30.0, 40.0], "photons": [500.0, 600.0]}
    )
    source = LocalizationResult("p", locs, pixel_size_nm=100.0, dims="2D")
    path = export_picasso_hdf5(source, tmp_path / "picasso.hdf5")

    outcome = controller._loadFromPath(str(path))

    assert outcome == 'current'
    assert len(controller._recon.published) == 1
    assert len(controller._recon.published[0][0]) == 2


def test_an_assumed_pixel_size_is_reported(controller, thunderstorm_csv):
    """A guess that reaches a result must be visible, not silent."""
    controller._loadFromPath(str(thunderstorm_csv))
    assert any("pixel size" in message for message in controller.warnings)


def test_a_supplied_pixel_size_is_not_reported(controller, tmp_path):
    path = tmp_path / "px.csv"
    path.write_text(
        '"id","frame","x [nm]","y [nm]"\n1,1,1000.0,2000.0\n'
    )
    # Picasso files carry their pixel size, so no warning is due for them.
    locs = localizations_from_columns({"x_nm": [10.0], "y_nm": [30.0]})
    source = LocalizationResult("p", locs, pixel_size_nm=117.0, dims="2D")
    hdf5 = export_picasso_hdf5(source, tmp_path / "p.hdf5")

    controller._loadFromPath(str(hdf5))
    assert controller.warnings == []
    assert controller._recon.published[0][0].pixel_size_nm == pytest.approx(117.0)


def test_an_unreadable_localization_file_does_not_publish(controller, tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text('"id","frame","x [px]","y [px]"\n1,1,10.0,20.0\n')

    # Pixels with no pixel size: the reader refuses rather than guessing.
    outcome = controller._loadFromPath(str(path))

    assert outcome == 'empty'
    assert controller._recon.published == []
    assert controller.errors


def _generic_csv(tmp_path):
    path = tmp_path / "custom.csv"
    path.write_text("n,xpos,ypos,counts\n1,1000.0,2000.0,900\n2,3000.0,4000.0,700\n")
    return path


def _offer_localizations(controller, offered=True):
    """Make the active reconstructor accept (or not) localization tables."""
    controller._main._activeReconstructor = SmlmLocalizer if offered else SimpleNamespace(
        file_extensions=["tiff"], accepted_source_kinds=("image",)
    )


def test_a_generic_csv_asks_what_the_columns_mean(controller, tmp_path, monkeypatch):
    path = _generic_csv(tmp_path)
    _offer_localizations(controller)
    controller._widget = None

    from imswitch.improcess.view import LocalizationImportDialog as module

    monkeypatch.setattr(
        module.LocalizationImportDialog,
        "get_import_kwargs",
        classmethod(lambda cls, p, parent=None: {
            "mapping": {"frame": "n", "x_nm": "xpos", "y_nm": "ypos",
                        "photons": "counts"},
            "unit": "nm",
            "pixel_size_nm": 120.0,
            "frame_base": 1,
        }),
    )

    outcome = controller._loadFromPath(str(path))

    assert outcome == 'current'
    result, _name = controller._recon.published[0]
    np.testing.assert_allclose(result.locs.x_nm, [1000.0, 3000.0], rtol=1e-5)
    np.testing.assert_array_equal(result.locs.frame, [0, 1])
    assert result.pixel_size_nm == pytest.approx(120.0)


def test_cancelling_the_mapping_dialog_publishes_nothing(
    controller, tmp_path, monkeypatch
):
    path = _generic_csv(tmp_path)
    _offer_localizations(controller)
    controller._widget = None

    from imswitch.improcess.view import LocalizationImportDialog as module

    monkeypatch.setattr(
        module.LocalizationImportDialog,
        "get_import_kwargs",
        classmethod(lambda cls, p, parent=None: None),
    )

    assert controller._loadFromPath(str(path)) == 'cancelled'
    assert controller._recon.published == []


def test_a_generic_csv_is_not_offered_to_other_reconstructors(controller, tmp_path):
    """No dialog when localization tables are not on the menu."""
    path = _generic_csv(tmp_path)
    _offer_localizations(controller, offered=False)

    outcome = controller._loadFromPath(str(path))

    assert outcome == 'empty'
    assert controller._recon.published == []


def test_a_plain_hdf5_still_takes_the_image_path(controller, tmp_path):
    """The localization branch must not swallow ordinary image files."""
    import h5py

    path = tmp_path / "image.hdf5"
    with h5py.File(str(path), "w") as h5:
        h5.create_dataset("Camera", data=np.zeros((4, 4), dtype=np.uint16))

    controller.multiDataFrameController = SimpleNamespace(
        makeAndAddDataObj=lambda *a, **k: None
    )
    outcome = controller._loadFromPath(str(path))

    assert outcome != 'current' or controller._recon.published == []
    assert controller._recon.published == []
