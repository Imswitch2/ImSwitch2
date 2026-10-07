"""Help → Documentation and Help → Check for updates… point at ImSwitch2.

This fork inherited both from upstream ImSwitch: the docs link opened
``imswitch.readthedocs.io`` and the update check asked upstream's GitHub
releases, so a user was sent to another project's manual and told about
another project's versions.
"""
from importlib import import_module
from unittest.mock import Mock

import pytest

import imswitch

# import_module, not `from ... import`: the controller package's lazy exports
# resolve these names to the classes, and the tests patch the modules.
checkupdates = import_module('imswitch.imcommon.controller.CheckUpdatesController')
multimodule = import_module('imswitch.imcommon.controller.MultiModuleWindowController')

# Upstream's docs host and repositories (both the current and the original owner).
_UPSTREAM = ('imswitch.readthedocs.io', 'kasasxav/', 'github.com/ImSwitch/')


@pytest.mark.nohardware
def test_documentation_opens_imswitch2_docs(monkeypatch):
    opened = []
    monkeypatch.setattr(multimodule.webbrowser, 'open', opened.append)

    multimodule.MultiModuleWindowController.showDocs(None)

    assert opened == [f'https://github.com/{imswitch.__github_repo__}/tree/main/docs']
    assert not any(u in opened[0] for u in _UPSTREAM)


def _run_check(monkeypatch, *, bundle, current='0.2.0', github_tag=None, pypi_version=None):
    """Run CheckUpdatesThread.run synchronously; return (requests, signals)."""
    requested = []

    def fake_get(url, timeout):
        requested.append(url)
        return Mock(json=Mock(return_value={'tag_name': github_tag}))

    def fake_pypi(distname):
        requested.append(distname)
        return pypi_version

    monkeypatch.setattr(imswitch, '__version__', current)
    monkeypatch.setattr(checkupdates.requests, 'get', fake_get)
    monkeypatch.setattr(checkupdates, 'get_version_pypi', fake_pypi)
    if bundle:
        monkeypatch.setenv('IMSWITCH_IS_BUNDLE', '1')
    else:
        monkeypatch.delenv('IMSWITCH_IS_BUNDLE', raising=False)

    thread = checkupdates.CheckUpdatesThread()
    emitted = []
    thread.sigFailed.connect(lambda: emitted.append(('failed',)))
    thread.sigNoUpdate.connect(lambda: emitted.append(('none',)))
    thread.sigNewVersionPyInstaller.connect(lambda v: emitted.append(('bundle', v)))
    thread.sigNewVersionPyPI.connect(lambda v: emitted.append(('pypi', v)))
    thread.run()
    return requested, emitted


@pytest.mark.nohardware
@pytest.mark.parametrize('tag, expected', [
    # Release tags are "v" + imswitch.__version__ (v0.2.0).
    ('v0.2.0', [('none',)]),
    ('v0.1.0', [('none',)]),
    ('v0.2.1', [('bundle', '0.2.1')]),
    ('v0.10.0', [('bundle', '0.10.0')]),  # numeric, not string, comparison
])
def test_bundle_update_check_asks_imswitch2_releases(monkeypatch, tag, expected):
    requested, emitted = _run_check(monkeypatch, bundle=True, github_tag=tag)

    assert requested == ['https://api.github.com/repos/Imswitch2/Imswitch2/releases/latest']
    assert emitted == expected


@pytest.mark.nohardware
@pytest.mark.parametrize('latest, expected', [
    ('0.2.0', [('none',)]),
    ('0.3.0', [('pypi', '0.3.0')]),
])
def test_pip_update_check_asks_pypi_for_imswitch2(monkeypatch, latest, expected):
    requested, emitted = _run_check(monkeypatch, bundle=False, pypi_version=latest)

    # `ImSwitch` on PyPI is upstream, whose 2.x would always look "newer".
    assert requested == ['imswitch2']
    assert emitted == expected


@pytest.mark.nohardware
def test_unreadable_release_reports_failure(monkeypatch):
    _, emitted = _run_check(monkeypatch, bundle=True, github_tag=None)

    assert emitted == [('failed',)]


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
