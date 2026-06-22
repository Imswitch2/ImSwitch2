"""Tests for make_live_source factory."""

import pytest

from imswitch.improcess.live import Hdf5LiveSource, ZarrLiveSource, make_live_source


def test_make_live_source_zarr_by_suffix():
    """Factory returns ZarrLiveSource when path ends with .zarr."""
    source = make_live_source("/data/stack.zarr")
    assert isinstance(source, ZarrLiveSource)


def test_make_live_source_hdf5_by_suffix_h5():
    """Factory returns Hdf5LiveSource when path ends with .h5."""
    source = make_live_source("/data/stack.h5")
    assert isinstance(source, Hdf5LiveSource)


def test_make_live_source_hdf5_by_suffix_hdf5():
    """Factory returns Hdf5LiveSource when path ends with .hdf5."""
    source = make_live_source("/data/stack.hdf5")
    assert isinstance(source, Hdf5LiveSource)


def test_make_live_source_hdf5_by_suffix_hdf():
    """Factory returns Hdf5LiveSource when path ends with .hdf."""
    source = make_live_source("/data/stack.hdf")
    assert isinstance(source, Hdf5LiveSource)


def test_make_live_source_explicit_zarr():
    """Factory returns ZarrLiveSource when fmt='zarr' is explicit."""
    source = make_live_source("/data/anything", fmt="zarr")
    assert isinstance(source, ZarrLiveSource)


def test_make_live_source_explicit_hdf5():
    """Factory returns Hdf5LiveSource when fmt='hdf5' is explicit."""
    source = make_live_source("/data/anything", fmt="hdf5")
    assert isinstance(source, Hdf5LiveSource)


def test_make_live_source_explicit_h5():
    """Factory returns Hdf5LiveSource when fmt='h5' is explicit."""
    source = make_live_source("/data/anything", fmt="h5")
    assert isinstance(source, Hdf5LiveSource)


def test_make_live_source_passes_detector_name():
    """Factory passes detector_name to the source constructor."""
    source = make_live_source("/data/stack.zarr", detector_name="CAM1")
    assert source._detector_name == "CAM1"


def test_make_live_source_passes_chunk_size():
    """Factory passes chunk_size to the source constructor."""
    source = make_live_source("/data/stack.zarr", chunk_size=10)
    assert source._chunk_size_override == 10


def test_make_live_source_tiff_raises_not_implemented():
    """Factory raises NotImplementedError for .tif suffix."""
    with pytest.raises(NotImplementedError, match="TIFF live sources are not supported"):
        make_live_source("/data/stack.tif")


def test_make_live_source_tiff_variant_raises_not_implemented():
    """Factory raises NotImplementedError for .tiff suffix."""
    with pytest.raises(NotImplementedError, match="TIFF live sources are not supported"):
        make_live_source("/data/stack.tiff")


def test_make_live_source_unknown_suffix_raises_value_error():
    """Factory raises ValueError for unknown suffix."""
    with pytest.raises(ValueError, match="Cannot determine format from path suffix"):
        make_live_source("/data/stack.unknown")


def test_make_live_source_unsupported_explicit_format_raises_value_error():
    """Factory raises ValueError for unsupported explicit format."""
    with pytest.raises(ValueError, match="Unsupported format"):
        make_live_source("/data/stack", fmt="jpeg")


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
