"""Drop-in trajectory patterns (docs/design/plans/
scan-engines-and-expert-scan.md §4, §7.8): a ``.py`` file in the
``scan_patterns`` folder defines ``PATTERNS``, and each becomes a Make…
preset beside the built-in ones. A file or pattern that cannot be used is
skipped with the reason; a gridded image records where its pattern came from.
"""

import hashlib
import textwrap

import numpy as np
import pytest

from imswitch.imcontrol.model.scan_program import pattern_plugins, presets
from imswitch.imcontrol.model.scan_program.pattern_plugins import load_user_patterns
from imswitch.imcontrol.model.scan_program.program import ProgramError
from imswitch.imcontrol.model.scan_program.trajectory import (
    PATTERNS, path_positions, realize_trajectory,
)

RATE = 100_000

ROSETTE = '''
import numpy as np
from imswitch.imcontrol.model.scan_program.trajectory import Pattern

def rosette(params, t, duration):
    cx, cy = params['center_um']
    angle = 2 * np.pi * t / duration
    r = float(params['radius_um']) * np.cos(float(params['petals']) * angle)
    return np.vstack([cx + r * np.cos(angle), cy + r * np.sin(angle)])

PATTERNS = [Pattern('user.rosette', 'Rosette', 2, (
    ('center_um', 'Centre', 'pair-micrometres'), ('radius_um', 'Radius', 'micrometres'),
    ('petals', 'Petals', 'number')), rosette)]
'''


@pytest.fixture(autouse=True)
def builtins_again(tmp_path_factory):
    """Every test leaves the built-in patterns and presets as it found them."""
    yield
    load_user_patterns(str(tmp_path_factory.mktemp('none')))


def _write(folder, name, text):
    path = folder / name
    path.write_text(textwrap.dedent(text))
    return path


def _rosette_params(**more):
    return {'x_device': 'X', 'y_device': 'Y', 'center_um': [1.0, 2.0], 'radius_um': 3.0,
            'petals': 4.0, 'duration_s': 0.01, 'window_s': 1e-4, 'approach_s': 1e-3,
            'pixel_um': 0.5, 'laser': None, 'record_measurements': False, **more}


def test_a_drop_in_becomes_a_preset_and_its_image_records_where_it_came_from(tmp_path):
    path = _write(tmp_path, 'rosette.py', ROSETTE)
    loaded = load_user_patterns(str(tmp_path))
    assert loaded.ids == ['user.rosette'] and not loaded.errors
    preset = presets.PRESETS['trajectory-user.rosette']
    assert preset.title == 'Trajectory: Rosette'
    assert {'center_um', 'radius_um', 'petals'} <= {f.name for f in preset.fields}

    program = presets.make('trajectory-user.rosette', _rosette_params())
    t = np.array([0.0, 0.0025])
    xy = path_positions(program.trajectory, t)
    assert xy[:, 0] == pytest.approx([1.0 + 3.0, 2.0])      # r = radius at angle 0

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    realized = realize_trajectory(program, RATE, captured_um={'X': 0.0, 'Y': 0.0})
    provenance = realized.provenance('counts')
    assert provenance['pattern'] == 'user.rosette'
    assert provenance['pattern_source'] == f'drop-in rosette.py sha256:{digest}'
    assert PATTERNS['circle'].source == 'built-in'


@pytest.mark.parametrize('name, text, reason', [
    ('broken.py', 'import no_such_module_anywhere\n', "it does not load: No module named"),
    ('empty.py', 'X = 1\n', 'it defines no PATTERNS list'),
    ('circle.py', ROSETTE.replace("'user.rosette'", "'circle'"),
     "'circle' is a built-in pattern's id"),
    ('cube.py', ROSETTE.replace("'Rosette', 2,", "'Rosette', 3,"),
     "moves 3 devices; a trajectory moves 2 (an X and a Y device)"),
    ('kind.py', ROSETTE.replace("'number'", "'degrees'"),
     "parameter 'petals' is of kind 'degrees'; the form takes micrometres"),
    ('notapattern.py', 'PATTERNS = ["rosette"]\n', "'rosette' is not a Pattern"),
])
def test_what_cannot_be_used_is_skipped_with_the_reason(tmp_path, name, text, reason):
    _write(tmp_path, 'rosette.py', ROSETTE)
    _write(tmp_path, name, text)
    loaded = load_user_patterns(str(tmp_path))
    assert loaded.ids == ['user.rosette']      # the good file still loads
    [error] = loaded.errors
    assert error.path.endswith(name) and reason in error.message


def test_the_second_file_defining_an_id_is_refused_by_the_first(tmp_path):
    _write(tmp_path, 'a.py', ROSETTE)
    _write(tmp_path, 'b.py', ROSETTE)
    loaded = load_user_patterns(str(tmp_path))
    assert loaded.ids == ['user.rosette']
    assert loaded.errors[0].message == "'user.rosette' is already defined by a.py"


def test_a_reload_drops_a_removed_file_and_unknown_patterns_say_where_they_load(tmp_path):
    path = _write(tmp_path, 'rosette.py', ROSETTE)
    load_user_patterns(str(tmp_path))
    program = presets.make('trajectory-user.rosette', _rosette_params())
    path.unlink()
    load_user_patterns(str(tmp_path))
    assert 'user.rosette' not in PATTERNS and 'trajectory-user.rosette' not in presets.PRESETS
    from imswitch.imcontrol.model.scan_program.program import validate
    with pytest.raises(ProgramError, match="Unknown pattern 'user.rosette'.*scan_patterns folder"):
        validate(program)


def test_the_example_written_into_the_folder_loads_once_renamed(tmp_path, monkeypatch):
    monkeypatch.setattr(pattern_plugins.dirtools.UserFileDirs, 'Root', str(tmp_path))
    folder = pattern_plugins.user_patterns_directory()
    template = tmp_path / 'scan_patterns' / pattern_plugins.TEMPLATE_NAME
    assert template.exists()
    assert load_user_patterns(folder).ids == []          # underscored: not loaded
    (tmp_path / 'scan_patterns' / 'rosette.py').write_text(template.read_text())
    loaded = load_user_patterns(folder)
    assert loaded.ids == ['user.rosette'] and not loaded.errors
    program = presets.make('trajectory-user.rosette', _rosette_params())
    assert np.all(np.isfinite(path_positions(program.trajectory, np.linspace(0, 0.01, 5))))


def test_the_expert_panel_offers_the_drop_ins(qtbot, tmp_path, monkeypatch):
    from .test_scan_controller_expert import ExpertRig
    _write(tmp_path, 'rosette.py', ROSETTE)
    monkeypatch.setattr(pattern_plugins, 'user_patterns_directory', lambda: str(tmp_path))
    rig = ExpertRig()
    try:
        assert rig.widget.presetTitles()['trajectory-user.rosette'] == 'Trajectory: Rosette'
    finally:
        rig.close()


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
