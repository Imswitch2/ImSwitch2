"""Where the napari-storm viewer branch and the endpoints/workflows branch meet.

* a localization table imported from another program's file carries the
  file as a fingerprinted source in its provenance, and an opaque import
  node that replay refuses with a clear reason;
* the localization precision columns the storm branch added travel to a
  napari endpoint as Points properties.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.localization_result import LocalizationResult  # noqa: E402
from imswitch.improcess.model.localization_schema import localizations_from_columns  # noqa: E402
from imswitch.improcess.model.napari_layers import result_to_layer_data  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node, record_external_table  # noqa: E402
from imswitch.improcess.model.provenance_io import read_provenance  # noqa: E402

TS_HEADER = '"id","frame","x [nm]","y [nm]","sigma [nm]","intensity [photon]","uncertainty [nm]"'
TS_ROWS = ["1,1,1000.0,2000.0,130.0,850.0,12.5", "2,2,3000.0,4000.0,140.0,900.0,9.0"]


@pytest.fixture(scope="module", autouse=True)
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _controller():
    from imswitch.improcess.controller.FileIOController import FileIOController

    instance = FileIOController.__new__(FileIOController)
    published = []
    instance._main = SimpleNamespace(
        reconstructionController=SimpleNamespace(resultProduced=lambda r, n: published.append(r)),
        _currentDataObj=None, _activeReconstructor=None,
    )
    instance._logger = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                       warning=lambda *a, **k: None, error=lambda *a, **k: None)
    return instance, published


def test_an_imported_table_records_its_file_and_an_opaque_import(tmp_path):
    path = tmp_path / "locs.csv"
    path.write_text("\n".join([TS_HEADER, *TS_ROWS]) + "\n")
    controller, published = _controller()
    assert controller._loadFromPath(str(path)) == "current"
    result = published[0]
    graph = graph_of(result)
    node = output_node(result)
    assert node["op"] == "opaque" and node["replayable"] is False
    assert node["table_format"] == "thunderstorm-csv" and "thunderstorm-csv" in node["reasons"][0]
    source = graph["nodes"][node["inputs"][0]["node"]]
    assert source["op"] == "source" and source["source"]["kind"] == "file"
    assert Path(source["source"]["path"]) == path
    assert source["source"]["fingerprint"]["size"] == path.stat().st_size

    # The origin survives a save, and replay refuses with the reason.
    receipt = result.save(tmp_path / "out.csv")
    document = read_provenance(receipt.primary)
    assert Path(document.graph["nodes"][node["inputs"][0]["node"]]["source"]["path"]) == path
    from imswitch.improcess.workflows import bootstrap_registry
    from imswitch.improcess.workflows.replay import ReplayError, workflow_from_file

    with pytest.raises(ReplayError, match="external localization table"):
        workflow_from_file(receipt.primary, registry=bootstrap_registry(user_plugins=False))


def test_record_external_table_is_reusable_for_any_reader():
    result = LocalizationResult("t", localizations_from_columns({
        "frame": np.array([0]), "x_nm": np.array([10.0]), "y_nm": np.array([20.0])}), pixel_size_nm=100.0)
    record_external_table(result, "/data/run.hdf5", table_format="picasso-hdf5", reader="custom")
    node = output_node(result)
    assert node["reader"] == "custom" and node["label"] == "Imported localization table (picasso-hdf5)"


def test_precision_columns_reach_a_napari_endpoint_as_point_properties():
    locs = localizations_from_columns({
        "frame": np.array([0, 1]), "x_nm": np.array([100.0, 200.0]), "y_nm": np.array([50.0, 75.0]),
        "photons": np.array([1000.0, 2000.0]), "lp_x_nm": np.array([12.5, 9.0]), "lp_y_nm": np.array([12.5, 9.0]),
    })
    result = LocalizationResult("locs", locs, pixel_size_nm=100.0)
    coords, kwargs, layer_type = result_to_layer_data(result)[0]
    assert layer_type == "points"
    np.testing.assert_allclose(kwargs["properties"]["lp_x_nm"], [12.5, 9.0])
    assert "x_nm" not in kwargs["properties"]                    # coordinates are the points themselves
    assert json.dumps(kwargs["metadata"]["coordinate_transform"])  # still serialisable


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
