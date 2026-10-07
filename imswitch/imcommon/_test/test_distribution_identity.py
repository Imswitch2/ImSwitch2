"""Guards the PyPI distribution name.

This fork publishes as ``imswitch2`` because ``ImSwitch`` on PyPI is the
upstream project.  Both distributions install the same import package,
``imswitch``, so any place that still names the old distribution does not
merely fail: ``pip`` resolves it to upstream and overwrites this package with
upstream's.  That covers an install hint in an error message, a plugin's
dependency on the host, and the update check comparing against the wrong
project.
"""

import configparser
import pathlib
import re

import pytest

import imswitch

REPO_ROOT = pathlib.Path(imswitch.__file__).resolve().parent.parent
SETUP_CFG = REPO_ROOT / 'setup.cfg'

# "pip install" followed by the bare old name: `imswitch`, `ImSwitch[hardware]`,
# `"imswitch[full]"`.  `imswitch2` and `imswitch-device-x` do not match.
_STALE_INSTALL_HINT = re.compile(
    r'pip install\s+(?:-U\s+|--upgrade\s+)?["\']?imswitch(?![\w-])', re.IGNORECASE
)
# The `dependencies = [...]` array of a pyproject.toml, and the distribution
# name at the start of each quoted requirement in it.  (tomllib is 3.11+.)
_DEPENDENCIES = re.compile(r'^dependencies\s*=\s*\[(.*?)\]', re.MULTILINE | re.DOTALL)
_REQUIREMENT_NAME = re.compile(r'["\']\s*([A-Za-z0-9._-]+)')

_SCANNED_SUFFIXES = {'.py', '.json', '.rst', '.md', '.toml', '.cfg'}
# Historical records: they describe what was true when they were written.
_HISTORICAL = (REPO_ROOT / 'docs' / 'design', REPO_ROOT / 'docs' / 'changelog.rst')


def _require_checkout():
    if not SETUP_CFG.is_file():
        # Installed from a wheel rather than a checkout; setup.cfg, docs/ and
        # examples/ are not there to compare against.
        pytest.skip(f'{SETUP_CFG} not present (not a source checkout)')


def _setup_cfg_metadata(key):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(SETUP_CFG, encoding='utf-8')
    return parser['metadata'][key]


def _scanned_files():
    for top in ('imswitch', 'docs', 'examples'):
        for path in sorted((REPO_ROOT / top).rglob('*')):
            if path.suffix not in _SCANNED_SUFFIXES or not path.is_file():
                continue
            if any(path == h or h in path.parents for h in _HISTORICAL):
                continue
            yield path


@pytest.mark.nohardware
def test_distname_matches_setup_cfg():
    _require_checkout()
    assert _setup_cfg_metadata('name') == imswitch.__distname__


@pytest.mark.nohardware
def test_version_is_single_sourced():
    # setup.cfg reads the version out of the package, so there is exactly one
    # place to bump at release time, and the publish workflow's tag check
    # compares the tag against it.
    _require_checkout()
    assert _setup_cfg_metadata('version') == 'attr: imswitch.__version__'


@pytest.mark.nohardware
def test_github_repo_is_owner_slash_repo():
    # Interpolated into an api.github.com URL and into dialog links; a full URL
    # or a trailing slash here 404s, and users see "could not check for updates".
    owner, _, repo = imswitch.__github_repo__.partition('/')
    assert owner and repo and '/' not in repo
    assert not imswitch.__github_repo__.startswith('http')


@pytest.mark.nohardware
def test_no_install_hint_names_the_upstream_distribution():
    _require_checkout()
    stale = []
    for path in _scanned_files():
        text = path.read_text(encoding='utf-8', errors='replace')
        for match in _STALE_INSTALL_HINT.finditer(text):
            line = text.count('\n', 0, match.start()) + 1
            stale.append(f'{path.relative_to(REPO_ROOT)}:{line}: {match.group(0)}')
    assert not stale, (
        f'install hints naming the upstream distribution; use {imswitch.__distname__}:\n'
        + '\n'.join(stale)
    )


@pytest.mark.nohardware
def test_example_plugins_depend_on_this_distribution():
    _require_checkout()
    pyprojects = sorted((REPO_ROOT / 'examples' / 'plugins').glob('*/pyproject.toml'))
    if not pyprojects:
        pytest.skip('no example plugins in this checkout')
    for pyproject in pyprojects:
        block = _DEPENDENCIES.search(pyproject.read_text(encoding='utf-8'))
        names = {n.lower() for n in _REQUIREMENT_NAME.findall(block.group(1))} if block else set()
        where = pyproject.relative_to(REPO_ROOT)
        assert 'imswitch' not in names, f'{where} requires the upstream ImSwitch'
        assert imswitch.__distname__ in names, (
            f'{where} does not require {imswitch.__distname__}'
        )


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
