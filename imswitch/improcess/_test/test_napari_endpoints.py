"""Endpoint descriptors, discovery, gating and export selection."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.model.napari_endpoints import (
    BUILTIN_ENDPOINTS,
    EndpointError,
    InstalledPlugins,
    InstalledWidget,
    NapariEndpoint,
    NapariWriterFormat,
    OutputMapping,
    availability,
    discover_endpoints,
    endpoint_from_dict,
    endpoints_for,
    endpoints_from_config,
    export_filename,
    export_result,
    resolve_widget,
    writer_formats_for,
)
from imswitch.improcess.model.points_table_result import PointsTableResult


def _installed(**kwargs):
    base = dict(
        plugin_names=frozenset({"napari-storm", "napari-skimage", "some-plugin"}),
        widgets=(
            InstalledWidget("napari-storm", "Napari STORM"),
            InstalledWidget("napari-skimage", "Gaussian filter"),
            InstalledWidget("napari-skimage", "Automated Threshold"),
            InstalledWidget("some-plugin", "Do Things"),
        ),
        reader_plugins=frozenset({"napari-storm"}),
        writers=(NapariWriterFormat("some-plugin", "some-plugin.write", "Write", ("image+", "labels?"), (".foo",)),),
    )
    base.update(kwargs)
    return InstalledPlugins(**base)


def _image():
    return ArrayProcessingResult("img", np.zeros((4, 4), dtype=np.float32), ["Y", "X"])


def _locs():
    return LocalizationResult(
        "locs",
        localizations_from_columns({
            "frame": np.array([0]), "x_nm": np.array([10.0]), "y_nm": np.array([20.0]),
        }),
        pixel_size_nm=100.0,
    )


# -- descriptors ----------------------------------------------------------------

def test_a_reader_endpoint_must_be_a_verified_pair():
    with pytest.raises(EndpointError, match="verified"):
        NapariEndpoint(id="x", label="x", lane="reader", plugin_name="p",
                       reader_plugin="p", export_format="hdf5", kinds=("image",), verified=False)
    with pytest.raises(EndpointError, match="unknown export format"):
        NapariEndpoint(id="x", label="x", lane="reader", plugin_name="p",
                       reader_plugin="p", export_format="nope", kinds=("image",), verified=True)
    with pytest.raises(EndpointError, match="cannot write"):
        NapariEndpoint(id="x", label="x", lane="reader", plugin_name="p",
                       reader_plugin="p", export_format="picasso-hdf5", kinds=("image",), verified=True)


def test_a_verified_dock_endpoint_declares_kinds():
    with pytest.raises(EndpointError, match="declare kinds"):
        NapariEndpoint(id="x", label="x", lane="dock", plugin_name="p", widget_name="W", verified=True)
    NapariEndpoint(id="x", label="x", lane="dock", plugin_name="p", widget_name="W")   # unverified is fine


def test_builtin_adapters_are_consistent():
    assert all(endpoint.verified for endpoint in BUILTIN_ENDPOINTS)
    assert len({endpoint.id for endpoint in BUILTIN_ENDPOINTS}) == len(BUILTIN_ENDPOINTS)


# -- config -------------------------------------------------------------------------

def test_config_entries_become_verified_adapters_and_bad_ones_are_reported():
    config = {"napariEndpoints": [
        {"id": "my:dock", "label": "Mine", "lane": "dock", "plugin": "some-plugin",
         "widget": "Do Things", "kinds": ["image"],
         "outputMappings": [{"pattern": "mask*", "layerType": "labels", "kind": "labels", "preservesGrid": True}]},
        {"id": "broken", "lane": "reader", "plugin": "p"},
        "not-an-object",
    ]}
    endpoints, errors = endpoints_from_config(config)
    assert [e.id for e in endpoints] == ["my:dock"]
    assert endpoints[0].verified is True
    assert endpoints[0].output_mappings[0] == OutputMapping("mask*", "labels", "labels", True, "")
    assert len(errors) == 2
    assert "napariEndpoints[1]" in errors[0]


def test_config_overrides_a_builtin_with_the_same_id():
    config = {"napariEndpoints": [{"id": "napari-storm:dock", "label": "Custom storm", "lane": "dock",
                                   "plugin": "napari-storm", "widget": "Napari STORM", "kinds": ["localization"]}]}
    endpoints, _ = discover_endpoints(installed=_installed(), config=config)
    storm = [e for e in endpoints if e.id == "napari-storm:dock"]
    assert len(storm) == 1 and storm[0].label == "Custom storm"


# -- discovery ------------------------------------------------------------------------

def test_discovery_adds_unverified_docks_only_for_widgets_no_adapter_covers():
    endpoints, errors = discover_endpoints(installed=_installed())
    assert errors == []
    unverified = [e for e in endpoints if not e.verified]
    assert [e.id for e in unverified] == ["some-plugin:do-things"]
    assert unverified[0].kinds == ()
    assert "(unverified)" in unverified[0].label
    # covered by adapters: no duplicate entries for storm / skimage widgets
    assert sum(e.plugin_name == "napari-storm" and e.lane == "dock" for e in endpoints) == 1
    assert endpoints[0].verified is True          # verified first


def test_discovery_never_invents_reader_endpoints():
    endpoints, _ = discover_endpoints(installed=_installed(), builtin=())
    assert all(e.lane == "dock" for e in endpoints)


def test_widget_patterns_resolve_against_installed_names():
    endpoint = NapariEndpoint(id="s:g", label="g", lane="dock", plugin_name="napari-skimage",
                              widget_name="Gaussian*", kinds=("image",), verified=True)
    assert resolve_widget(endpoint, _installed()) == "Gaussian filter"
    assert availability(endpoint, _installed()) == (True, "")


# -- gating -----------------------------------------------------------------------------

def test_dock_endpoints_are_gated_by_layerability_then_kinds():
    endpoints, _ = discover_endpoints(installed=_installed())
    image_offers = {e.id for e in endpoints_for(_image(), endpoints)}
    assert "napari-skimage:gaussian" in image_offers
    assert "some-plugin:do-things" in image_offers            # unverified: any layerable kind
    assert "napari-storm:dock" not in image_offers            # declared localization only
    table_offers = endpoints_for(PointsTableResult("t", np.zeros((1, 2))), endpoints)
    assert table_offers == []                                 # not layerable, even for unverified


def test_reader_endpoints_need_the_verified_pair_for_the_kind():
    endpoints, _ = discover_endpoints(installed=_installed())
    offers = {e.id for e in endpoints_for(_locs(), endpoints)}
    assert {"napari-storm:dock", "napari-storm:reader"} <= offers
    assert "napari-skimage:gaussian" not in offers


def test_availability_names_the_missing_plugin():
    endpoints, _ = discover_endpoints(installed=_installed(plugin_names=frozenset({"napari-storm"}), widgets=()))
    gaussian = next(e for e in endpoints if e.id == "napari-skimage:gaussian")
    ok, reason = availability(gaussian, _installed(plugin_names=frozenset({"napari-storm"}), widgets=()))
    assert not ok and "pip install napari-skimage" in reason
    storm_dock = next(e for e in endpoints if e.id == "napari-storm:dock")
    ok, reason = availability(storm_dock, _installed(widgets=()))
    assert not ok and "no widget named" in reason


# -- export -----------------------------------------------------------------------------------

def test_export_writes_the_format_the_reader_expects(tmp_path):
    endpoint = next(e for e in BUILTIN_ENDPOINTS if e.id == "napari-storm:reader")
    assert export_filename(endpoint, _locs()) == "locs.hdf5"
    path = export_result(endpoint, _locs(), tmp_path / "exp")
    assert path.exists() and path.suffix == ".hdf5"
    assert (tmp_path / "exp" / "locs.yaml").exists()      # Picasso sidecar comes along


def test_export_refuses_a_kind_the_exporter_cannot_write(tmp_path):
    endpoint = next(e for e in BUILTIN_ENDPOINTS if e.id == "napari-storm:reader")
    with pytest.raises(EndpointError):
        export_result(endpoint, _image(), tmp_path)


def test_labels_export_through_hdf5(tmp_path):
    endpoint = endpoint_from_dict({"id": "l:r", "lane": "reader", "plugin": "some-plugin",
                                   "reader": "some-plugin", "export": "hdf5", "kinds": ["labels"]})
    path = export_result(endpoint, LabelsResult("m", np.ones((3, 3), dtype=np.int32)), tmp_path)
    assert path.suffix == ".h5" and path.exists()


def test_a_drop_in_plugin_file_can_contribute_adapters(tmp_path):
    """A NAPARI_ENDPOINTS list in a plugin file is discovered like a processor,
    survives a file with no Processor subclass, and disappears on rescan."""
    from imswitch.improcess.model.napari_endpoints import clear_user_endpoints, user_endpoints
    from imswitch.improcess.plugins.user_plugins import discover_processor_plugins

    (tmp_path / "my_endpoints.py").write_text(
        "from imswitch.improcess.model.napari_endpoints import NapariEndpoint\n"
        "NAPARI_ENDPOINTS = [\n"
        "    NapariEndpoint(id='mine:dock', label='Mine', lane='dock', plugin_name='some-plugin',\n"
        "                   widget_name='Do Things', kinds=('image',), verified=True),\n"
        "    {'id': 'mine:reader', 'lane': 'reader', 'plugin': 'napari-storm', 'reader': 'napari-storm',\n"
        "     'export': 'picasso-hdf5', 'kinds': ['localization']},\n"
        "    {'id': 'broken', 'lane': 'reader', 'plugin': 'p'},\n"
        "]\n"
    )
    clear_user_endpoints()
    classes, errors = discover_processor_plugins(str(tmp_path))
    assert classes == {}
    assert [e.id for e in user_endpoints()] == ["mine:dock", "mine:reader"]
    assert any("NAPARI_ENDPOINTS[2]" in error.message for error in errors)
    assert not any("No Processor subclass" in error.message for error in errors)

    endpoints, _ = discover_endpoints(installed=_installed())
    mine = next(e for e in endpoints if e.id == "mine:dock")
    assert mine.verified is True
    assert "some-plugin:do-things" not in {e.id for e in endpoints}   # covered by the adapter now

    clear_user_endpoints()
    assert user_endpoints() == []


def test_writer_formats_match_all_requested_layer_types():
    installed = _installed()
    assert [w.writer_id for w in writer_formats_for(["image"], installed)] == ["some-plugin.write"]
    assert [w.writer_id for w in writer_formats_for(["image", "image", "labels"], installed)] == ["some-plugin.write"]
    assert writer_formats_for(["points"], installed) == []
    assert writer_formats_for(["labels"], installed) == []          # image+ needs at least one image
    assert writer_formats_for(["image", "labels", "labels"], installed) == []   # labels? allows one
