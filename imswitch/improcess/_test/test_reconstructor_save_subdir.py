"""Contract tests for ``Reconstructor.default_save_subdir``.

This attribute declares the output subdirectory a reconstructor prefers for
saved results. Its former consumer -- the batch ``WatcherFrameController``
("Watch and run") -- was removed along with the rest of the old file-watcher,
so nothing reads it today; it is kept as declarative plugin metadata for a
future save path. These tests lock in the declared values so a plugin's
override keeps meaning what it says.
"""

from imswitch.improcess.reconstructors.base import Reconstructor


def test_base_reconstructor_default_save_subdir_is_rec():
    assert Reconstructor.default_save_subdir == 'rec'


def test_view_only_inherits_default_save_subdir():
    from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor

    assert ViewOnlyReconstructor.default_save_subdir == 'rec'


def test_modality_reconstructors_inherit_default_save_subdir():
    from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
    from imswitch.improcess.reconstructors.snouty import SnoutyReconstructor

    # Plugins are free to override later — this just locks in that the
    # current built-ins inherit the base 'rec' default, so changing the
    # base attribute name in the future is a single-line edit.
    for cls in (MonalisaReconstructor, SnoutyReconstructor):
        assert cls.default_save_subdir == 'rec', cls.__name__


def test_plugins_may_override_default_save_subdir():
    """Overriding reconstructors declare their own output folder name."""
    from imswitch.improcess.reconstructors.beadrec.reconstructor import (
        BeadRecReconstructor,
    )

    assert BeadRecReconstructor.default_save_subdir == 'beadrec'


if __name__ == "__main__":
    import sys

    import pytest

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
