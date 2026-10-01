"""The reconstructor decides which recordings the live path may hand it.

Two pieces: ``Reconstructor.accepts_raw_source`` (the declared capability) and
``source_factory`` (which reader serves an accepted recording). Both are pure
-- no store is opened here.

    pytest imswitch/improcess/_test/test_raw_source_compatibility.py -v
"""

import pytest

from imswitch.improcess.live.source_factory import make_live_source, reader_class_for
from imswitch.improcess.live.source_type import (
    FORMAT_HDF5,
    FORMAT_ZARR,
    LAYOUT_MULTIFILE_LAPSE,
    LAYOUT_SINGLE,
    LAYOUT_SINGLE_LAPSE_FILE,
    LAYOUTS,
    RawSourceType,
)
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.monalisa.reconstructor import (
    MonalisaReconstructor,
)
from imswitch.improcess.reconstructors.view_only.reconstructor import (
    ViewOnlyReconstructor,
)
from imswitch.improcess.live.sources import (
    Hdf5LapseSource,
    Hdf5LiveSource,
    Hdf5MultiFileLapseSource,
    ZarrLapseSource,
    ZarrLiveSource,
    ZarrMultiFileLapseSource,
)


SCAN_LAPSE = RawSourceType(FORMAT_ZARR, LAYOUT_MULTIFILE_LAPSE, 1225, 10)
SCAN_ONCE = RawSourceType(FORMAT_ZARR, LAYOUT_SINGLE, 1225, 1)
SCAN_ONE_FILE = RawSourceType(FORMAT_HDF5, LAYOUT_SINGLE_LAPSE_FILE, 1225, 10)
CAMERA_LAPSE = RawSourceType(FORMAT_ZARR, LAYOUT_MULTIFILE_LAPSE, 1, 50)
SNAP = RawSourceType(FORMAT_HDF5, LAYOUT_SINGLE, 1, 1)


class _Plain(Reconstructor):
    """Minimal concrete reconstructor: defaults only."""

    name = "Plain"
    id = "plain"

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        return None


# --- the default: accept everything ----------------------------------------

@pytest.mark.parametrize(
    "source_type", [SCAN_LAPSE, SCAN_ONCE, SCAN_ONE_FILE, CAMERA_LAPSE, SNAP]
)
def test_a_plugin_that_declares_nothing_accepts_everything(source_type):
    """Most plugins care about the shape of the data, not how it was filed."""
    assert _Plain().accepts_raw_source(source_type) is True


# --- requires_frame_stacks --------------------------------------------------

def test_requiring_stacks_rejects_one_frame_per_timepoint():
    class _Scanning(_Plain):
        requires_frame_stacks = True

    plugin = _Scanning()

    assert plugin.accepts_raw_source(SCAN_LAPSE) is True
    assert plugin.accepts_raw_source(CAMERA_LAPSE) is False


def test_requiring_stacks_is_independent_of_being_a_timelapse():
    """A camera lapse is fifty timepoints with nothing to reassemble; a single
    scan is one timepoint with plenty."""
    class _Scanning(_Plain):
        requires_frame_stacks = True

    plugin = _Scanning()

    assert CAMERA_LAPSE.is_timelapse and not plugin.accepts_raw_source(CAMERA_LAPSE)
    assert not SCAN_ONCE.is_timelapse and plugin.accepts_raw_source(SCAN_ONCE)


# --- accepted_layouts -------------------------------------------------------

def test_restricting_layouts_rejects_the_others():
    class _LapsesOnly(_Plain):
        accepted_layouts = (LAYOUT_MULTIFILE_LAPSE,)

    plugin = _LapsesOnly()

    assert plugin.accepts_raw_source(SCAN_LAPSE) is True
    assert plugin.accepts_raw_source(SCAN_ONCE) is False
    assert plugin.accepts_raw_source(SCAN_ONE_FILE) is False


def test_none_means_unrestricted_not_nothing():
    """The default is a real value, so it must not read as an empty set."""
    assert _Plain.accepted_layouts is None
    assert all(_Plain().accepts_raw_source(
        RawSourceType(FORMAT_ZARR, layout, 4, 2)) for layout in LAYOUTS)


def test_both_constraints_must_pass():
    class _Strict(_Plain):
        accepted_layouts = (LAYOUT_MULTIFILE_LAPSE,)
        requires_frame_stacks = True

    plugin = _Strict()

    assert plugin.accepts_raw_source(SCAN_LAPSE) is True
    assert plugin.accepts_raw_source(CAMERA_LAPSE) is False     # layout ok, shape not
    assert plugin.accepts_raw_source(SCAN_ONCE) is False        # shape ok, layout not


def test_accepts_raw_source_can_be_overridden():
    class _Odd(_Plain):
        def accepts_raw_source(self, source_type):
            return source_type.num_timepoints == 7

    assert _Odd().accepts_raw_source(RawSourceType(FORMAT_ZARR, LAYOUT_SINGLE, 1, 7))
    assert not _Odd().accepts_raw_source(SCAN_LAPSE)


# --- the real plugins -------------------------------------------------------

def test_monalisa_needs_frame_stacks():
    plugin = MonalisaReconstructor()

    assert plugin.accepts_raw_source(SCAN_LAPSE) is True
    assert plugin.accepts_raw_source(SCAN_ONE_FILE) is True
    assert plugin.accepts_raw_source(CAMERA_LAPSE) is False
    assert plugin.accepts_raw_source(SNAP) is False


@pytest.mark.parametrize(
    "source_type", [SCAN_LAPSE, SCAN_ONCE, SCAN_ONE_FILE, CAMERA_LAPSE, SNAP]
)
def test_view_only_takes_anything_it_can_read(source_type):
    """A pass-through viewer has no shape requirement -- that is the point."""
    assert ViewOnlyReconstructor().accepts_raw_source(source_type) is True


# --- factory dispatch -------------------------------------------------------

@pytest.mark.parametrize("format_id,layout,expected", [
    (FORMAT_ZARR, LAYOUT_SINGLE, ZarrLiveSource),
    (FORMAT_ZARR, LAYOUT_MULTIFILE_LAPSE, ZarrMultiFileLapseSource),
    (FORMAT_ZARR, LAYOUT_SINGLE_LAPSE_FILE, ZarrLapseSource),
    (FORMAT_HDF5, LAYOUT_SINGLE, Hdf5LiveSource),
    (FORMAT_HDF5, LAYOUT_MULTIFILE_LAPSE, Hdf5MultiFileLapseSource),
    (FORMAT_HDF5, LAYOUT_SINGLE_LAPSE_FILE, Hdf5LapseSource),
])
def test_every_pair_maps_to_its_reader(format_id, layout, expected):
    assert reader_class_for(RawSourceType(format_id, layout, 4, 2)) is expected


def test_the_mapping_covers_every_pair_the_classifier_can_produce():
    """A missing entry should be a bug, not a silently wrong reader."""
    for format_id in (FORMAT_ZARR, FORMAT_HDF5):
        for layout in LAYOUTS:
            reader_class_for(RawSourceType(format_id, layout, 4, 2))


def test_an_unknown_pair_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        reader_class_for(RawSourceType("tiff", LAYOUT_SINGLE, 4, 2))


def test_the_factory_builds_without_touching_disk(tmp_path):
    """No store exists at this path; a classification was supplied, so none is
    needed -- which is what keeps the gate from opening every store twice."""
    source = make_live_source(str(tmp_path / "nothing.zarr"), SCAN_ONCE)

    assert isinstance(source, ZarrLiveSource)


def test_a_multifile_lapse_reader_gets_the_seed_and_the_timepoint_total():
    """It derives the sibling-name template from the seed, and is left to
    work out the timepoint total for itself."""
    seed = "root/lapse/exp_scan00_Cam.zarr"

    source = make_live_source(seed, SCAN_LAPSE)

    assert isinstance(source, ZarrMultiFileLapseSource)
    assert source._first_path == seed
    # NOT capped by the classification: its count is only what was on disk
    # when the watcher looked, and forcing it would truncate a lapse still
    # being written. The reader derives the real total when it opens.
    assert source._num_timepoints is None


def test_an_unclassifiable_path_raises(tmp_path):
    with pytest.raises(ValueError):
        make_live_source(str(tmp_path / "mystery.txt"))


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


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
