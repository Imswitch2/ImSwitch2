"""A MoNaLISA result saves to every image container, not only TIFF.

``MonalisaProcessingResult`` listed TIFF and the ImageJ hyperstack as its
formats, so the save dialog's HDF5 and Zarr choices were refused for MoNaLISA
results alone, with the same ``(T, Z, C, Y, X)`` layout every other image
result writes to them through the shared writers.
"""

import json

import numpy as np
import pytest

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.save_protocol import UnsupportedSaveFormat
from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult


def _result():
    data = np.random.default_rng(0).random((1, 2, 1, 3, 8, 6)).astype(np.float32)
    scan_params = {
        "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
        "directions": ["pos", "neg", "pos"],
        "steps": ["6", "8", "3", "1"],
        "step_sizes": ["40", "40", "50", "1"],
        "n_linesteps": 1,
        "unidirectional": True,
    }
    return MonalisaProcessingResult(
        "probe", data, scan_params, output_pixel_size_nm=(40.0, 40.0)
    )


def test_every_image_container_is_offered():
    assert set(MonalisaProcessingResult.supported_formats) == {"tiff", "hdf5", "zarr", "imagej"}


@pytest.mark.parametrize(
    "name, fmt", [("probe.h5", "hdf5"), ("probe.ome.zarr", "zarr"), ("probe.ome.tif", "tiff")]
)
def test_the_hyperstack_layout_round_trips(tmp_path, name, fmt):
    result = _result()
    receipt = result.save(tmp_path / name, fmt)
    assert [path.name for path in receipt.files] == [name]

    reloaded = DataObj(name, "data", path=str(tmp_path / name))
    reloaded.checkAndLoadData()
    # (T, Z, C, Y, X): C = Dataset x Base, the layout every MoNaLISA file
    # has; the TIFF reader drops the T axis of length 1.
    assert np.squeeze(np.asarray(reloaded.data)).shape == (3, 2, 8, 6)
    to_um = {"um": 1.0, "nm": 1e-3}[reloaded.scale_unit]
    scales = {
        label: scale * to_um
        for label, scale in zip(reloaded.axis_labels, reloaded.axis_scales)
    }
    assert (scales["Z"], scales["Y"], scales["X"]) == pytest.approx((0.05, 0.04, 0.04))
    folded, _sizes, _names = result._fold_for_disk()
    np.testing.assert_allclose(
        np.squeeze(np.asarray(reloaded.data)), np.squeeze(folded), rtol=1e-6
    )


def test_hdf5_carries_the_channel_names_and_the_scan(tmp_path):
    import h5py

    _result().save(tmp_path / "probe.h5", "hdf5")
    with h5py.File(tmp_path / "probe.h5") as handle:
        assert json.loads(handle.attrs["channel_names"]) == ["ds0-signal", "ds0-background"]
        assert handle["data"].attrs["axis_labels"] == "T,Z,C,Y,X"
        assert json.loads(handle.attrs["scan_params"])["steps"] == ["6", "8", "3", "1"]
        assert "ome_xml" in handle.attrs


def test_a_format_the_type_cannot_write_is_still_refused(tmp_path):
    with pytest.raises(UnsupportedSaveFormat):
        _result().save(tmp_path / "probe.csv", "csv")


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
