"""What the update dialog tells a user to do.

The instructions went stale once already: they described the 1.x zip bundles
long after the standalone builds became installers, telling users to extract
into a new folder and *not* overwrite the old install -- the opposite of what
both installers want.  These pin the two halves to their platforms, which is
the part a copy-paste would invert silently.
"""

import pytest

import imswitch
from imswitch.imcommon.view.CheckUpdatesDialog import CheckUpdatesDialog

pytestmark = [pytest.mark.nohardware, pytest.mark.ui]


@pytest.fixture
def dialog(qtbot):
    widget = CheckUpdatesDialog()
    qtbot.addWidget(widget)
    return widget


def test_windows_is_told_to_run_the_installer(dialog, monkeypatch):
    monkeypatch.setattr(
        'imswitch.imcommon.view.CheckUpdatesDialog.sys.platform', 'win32'
    )
    dialog.showPyInstallerUpdate('9.9.9')
    text = dialog.informationLabel.text()

    assert 'installer' in text
    assert 'in place' in text
    assert 'disk image' not in text


def test_macos_is_told_to_drag_the_app_across(dialog, monkeypatch):
    monkeypatch.setattr(
        'imswitch.imcommon.view.CheckUpdatesDialog.sys.platform', 'darwin'
    )
    dialog.showPyInstallerUpdate('9.9.9')
    text = dialog.informationLabel.text()

    assert 'disk image' in text
    assert 'Applications' in text


def test_no_platform_is_told_to_keep_the_old_installation(dialog, monkeypatch):
    # The specific stale advice this replaced.  A bundle upgraded that way ends
    # up as two installations, and on Windows the second one's uninstaller
    # registration fights the first.
    for platform in ('win32', 'darwin', 'linux'):
        monkeypatch.setattr(
            'imswitch.imcommon.view.CheckUpdatesDialog.sys.platform', platform
        )
        dialog.showPyInstallerUpdate('9.9.9')
        text = dialog.informationLabel.text().lower()
        assert 'do not overwrite' not in text, platform
        assert 'new folder' not in text, platform


def test_both_routes_name_this_project_and_its_own_version(dialog):
    for show in (dialog.showPyInstallerUpdate, dialog.showPyPIUpdate):
        show('9.9.9')
        text = dialog.informationLabel.text()
        assert '9.9.9' in text
        assert imswitch.__version__ in text
        # "ImSwitch 9.9.9" would read as the upstream project, which is a
        # different distribution that installs over this one.
        assert 'ImSwitch2' in text


def test_the_pip_route_names_this_distribution(dialog):
    dialog.showPyPIUpdate('9.9.9')
    assert f'pip install --upgrade {imswitch.__distname__}' in dialog.informationLabel.text()


def test_the_link_points_at_this_repository(dialog):
    dialog.showPyInstallerUpdate('9.9.9')
    assert imswitch.__github_repo__ in dialog.linkLabel.text()
    assert '/releases/latest' in dialog.linkLabel.text()


# Copyright (C) 2020-2021 ImSwitch developers
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
