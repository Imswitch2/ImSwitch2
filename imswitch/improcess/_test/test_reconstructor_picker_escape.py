"""The reconstructor picker must never become a control with no way out.

Opening any file inside a tiling run resolves to that run's ``tiles.json`` --
a deliberate convenience, so a tile can be dropped in and the mosaic comes
back. But the picker also filters itself to reconstructors that accept the
*resolved* source, and only the tiling reconstructor accepts a manifest. The
two together closed a loop: every file in the folder became a manifest, the
picker collapsed to one entry, and nothing in that folder could restore an
image source again.

The way out is the user's own selection. It is remembered, offered, and
honoured by reopening the path once a different reconstructor is active.
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController as Manager,
)
from imswitch.improcess.model.dataset_sources import (
    resolve_dataset_source,
    specs_for_reconstructor,
)
from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES


@pytest.fixture
def reconstructors(monkeypatch):
    """Exactly the two plugins this is about, injected rather than registered.

    The real registry is a process-wide singleton; standing a stub in its place
    keeps the assertions about *which* entries the picker offers exact.
    """
    plugins = {
        plugin_id: cls()
        for plugin_id, cls in _AVAILABLE_RECONSTRUCTOR_CLASSES.items()
        if plugin_id in ("tiling-mosaic", "view-only")
    }
    ordered = [plugins["view-only"], plugins["tiling-mosaic"]]
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.registry.get_registry",
        lambda: SimpleNamespace(reconstructors=lambda: list(ordered)),
    )
    return plugins


@pytest.fixture
def run_folder(tmp_path):
    run = tmp_path / "tiling_20260806_120000"
    run.mkdir()
    tifffile.imwrite(run / "tile-3-APDgreen.tiff", np.zeros((4, 4), np.uint16))
    (run / "tiles.json").write_text(
        json.dumps({"format": "imswitch-tiling/2", "tiles": []}), encoding="utf-8"
    )
    return run


def _manager(reconstructors, active, data_obj):
    """The controller's own methods over a minimal surface."""
    published = []
    reopened = []
    main = SimpleNamespace(
        _activeReconstructor=reconstructors[active],
        _currentDataObj=data_obj,
        fileIOController=SimpleNamespace(
            _loadFromPath=lambda path, **kwargs: reopened.append((path, kwargs))
        ),
    )
    manager = SimpleNamespace(
        _main=main,
        _widget=SimpleNamespace(
            setReconstructorChoices=lambda choices, current: published.append(
                [plugin_id for plugin_id, _name in choices]
            )
        ),
        _logger=SimpleNamespace(warning=lambda *a: None, debug=lambda *a: None,
                                info=lambda *a: None),
        _install_reconstructor_params=lambda reconstructor: None,
        _inspect_current_source=lambda: None,
    )
    for name in (
        "_accepts_current_source", "_offerable", "_reopen_path_for",
        "_publishReconstructorChoices", "_reopen_current_source",
        "_on_user_changed_reconstructor",
    ):
        bound = getattr(Manager, name)
        setattr(manager, name, (lambda fn: lambda *a, **k: fn(manager, *a, **k))(bound))
    manager.published = published
    manager.reopened = reopened
    return manager, main


def _opened_tile(run_folder):
    """The stuck state: a tile was selected, the manifest was loaded."""
    return SimpleNamespace(
        sourceKind="tiling-manifest",
        dataPath=str(run_folder / "tiles.json"),
        sourceOriginalPath=str(run_folder / "tile-3-APDgreen.tiff"),
    )


def _opened_run(run_folder):
    """The run directory itself was selected; there is no file to fall back to."""
    return SimpleNamespace(
        sourceKind="tiling-manifest",
        dataPath=str(run_folder / "tiles.json"),
        sourceOriginalPath=str(run_folder),
    )


def test_opening_a_tile_still_loads_the_run(run_folder, reconstructors):
    """The convenience this all rests on is unchanged."""
    resolved = resolve_dataset_source(
        run_folder / "tile-3-APDgreen.tiff",
        allowed_specs=specs_for_reconstructor(reconstructors["tiling-mosaic"]),
    )

    assert resolved.path.name == "tiles.json"
    assert resolved.format_id == "tiling-manifest"


def test_view_only_is_offered_for_a_tile_opened_as_a_run(
    run_folder, reconstructors
):
    manager, _main = _manager(
        reconstructors, "tiling-mosaic", _opened_tile(run_folder)
    )

    manager._publishReconstructorChoices()

    assert "view-only" in manager.published[-1]
    assert "tiling-mosaic" in manager.published[-1]


def test_choosing_view_only_reopens_the_file_the_user_picked(
    run_folder, reconstructors
):
    data_obj = _opened_tile(run_folder)
    manager, main = _manager(reconstructors, "tiling-mosaic", data_obj)

    manager._on_user_changed_reconstructor("view-only")

    assert main._activeReconstructor.id == "view-only"
    assert manager.reopened == [
        (data_obj.sourceOriginalPath, {"prefer_as_current": True})
    ]


def test_an_explicitly_opened_run_offers_only_what_can_read_it(
    run_folder, reconstructors
):
    """Nothing was selected that could be reopened, so nothing is promised."""
    manager, _main = _manager(
        reconstructors, "tiling-mosaic", _opened_run(run_folder)
    )

    manager._publishReconstructorChoices()

    assert manager.published[-1] == ["tiling-mosaic"]


def test_an_explicitly_opened_manifest_offers_only_what_can_read_it(
    run_folder, reconstructors
):
    manifest = run_folder / "tiles.json"
    manager, _main = _manager(
        reconstructors,
        "tiling-mosaic",
        SimpleNamespace(
            sourceKind="tiling-manifest",
            dataPath=str(manifest),
            sourceOriginalPath=str(manifest),
        ),
    )

    manager._publishReconstructorChoices()

    assert manager.published[-1] == ["tiling-mosaic"]


def test_a_reconstructor_that_cannot_read_the_file_is_not_offered(
    run_folder, reconstructors
):
    """The offer has to be real, not merely an escape from the dead end."""
    manager, _main = _manager(
        reconstructors, "tiling-mosaic", _opened_tile(run_folder)
    )
    unreadable = SimpleNamespace(
        id="hdf5-only", name="HDF5 only",
        file_extensions=["hdf5"], accepted_source_kinds=("image",),
    )

    assert manager._offerable(unreadable) is False


def test_container_ids_map_onto_the_kinds_reconstructors_declare():
    """The two vocabularies are separate, and mixing them silently filters."""
    from imswitch.improcess.model.dataset_sources import source_kind_for

    assert source_kind_for("tiff") == "image"
    assert source_kind_for("hdf5") == "image"
    assert source_kind_for("zarr") == "image"
    assert source_kind_for("tiling-manifest") == "tiling-manifest"
