"""The panel registry agrees with every other list of panels.

``model/panels.py`` says which panels can be added while ImSwitch runs. The
panels themselves are listed in four other places, each for its own purpose:
the dock tables of the main view, the lazy widget and controller tables, and
the Config Studio's catalog. This keeps the registry from becoming a fifth
list that drifts away from them.
"""

import json
from importlib import import_module
from pathlib import Path

import pytest

from imswitch.imcontrol.controller.controllers import _CONTROLLER_MODULES
from imswitch.imcontrol.model import panels
from imswitch.imcontrol.model.panels import PANELS, PanelKind
from imswitch.imcontrol.view.ImConMainView import (
    _DEFAULT_LEFT_DOCK_INFOS, _DEFAULT_RIGHT_DOCK_INFOS, _DOCK_DISPLAY_NAMES,
)
from imswitch.imcontrol.view.widgets import _WIDGET_MODULES


pytestmark = pytest.mark.nohardware

_SECTIONS_DIR = (Path(__file__).resolve().parents[2]
                 / 'view' / 'configeditor' / 'builtin_templates' / 'sections')
_DOCK_INFOS = {**_DEFAULT_RIGHT_DOCK_INFOS, **_DEFAULT_LEFT_DOCK_INFOS}
#: Built by the main view itself rather than through the dock tables.
_BUILT_EXPLICITLY = {'Image', 'Log'}
#: Not a widget/controller pair: the Log dock views the process-wide buffer;
#: Scan's classes are named by its scan type.
_NOT_A_WIDGET_PAIR = {'Log', 'Scan'}


def _knownPanelKeys():
    return set(_DOCK_INFOS) | set(_DOCK_DISPLAY_NAMES) | _BUILT_EXPLICITLY


def test_every_panel_the_app_knows_is_in_the_registry():
    missing = sorted(_knownPanelKeys() - set(PANELS))
    assert not missing, (
        f'{missing} can be docked but model/panels.py does not say whether '
        f'they can be added while ImSwitch runs; add a PanelSpec for each'
    )


def test_the_registry_names_no_panel_the_app_cannot_build():
    unknown = sorted(set(PANELS) - _knownPanelKeys())
    assert not unknown, f'{unknown} are in no dock table'


@pytest.mark.parametrize('key', sorted(PANELS))
def test_display_name_is_the_dock_title(key):
    expected = (_DOCK_INFOS[key].name if key in _DOCK_INFOS
                else _DOCK_DISPLAY_NAMES[key])
    assert PANELS[key].displayName == expected


@pytest.mark.parametrize('key', sorted(set(PANELS) - _NOT_A_WIDGET_PAIR))
def test_every_panel_resolves_to_a_widget_and_a_controller(key):
    assert f'{key}Widget' in _WIDGET_MODULES
    assert f'{key}Controller' in _CONTROLLER_MODULES


def test_groups_match_the_config_studio_catalog():
    from imswitch.imcontrol.view.configeditor import editor

    groupOf = {name: group for group, entries in editor.WIDGET_GROUPS.items()
               for name, _ in entries}
    wrong = {key: (spec.group, groupOf.get(key, 'Other'))
             for key, spec in PANELS.items() if spec.group != groupOf.get(key, 'Other')}
    assert not wrong, f'(registry, Config Studio) groups differ: {wrong}'
    assert set(panels.GROUP_ORDER) >= {spec.group for spec in PANELS.values()}


def test_sections_match_the_config_studio_requires_widget_map():
    from imswitch.imcontrol.view.configeditor import editor

    for widget, section in editor._WIDGET_REQUIRES_SECTION.items():
        assert PANELS[widget].section == section, widget
    for key, spec in PANELS.items():
        if spec.kind is PanelKind.CONFIGURED:
            assert editor._WIDGET_REQUIRES_SECTION.get(key) == spec.section, key


@pytest.mark.parametrize('key', sorted(k for k, s in PANELS.items() if s.kind is PanelKind.CONFIGURED))
def test_a_configured_panel_has_a_form_the_runtime_can_show(key):
    """The form is loaded without the Config Studio's placeholder resolution,
    so its options must be literal."""
    spec = PANELS[key]
    schema = json.loads((_SECTIONS_DIR / f'{spec.section}.json').read_text(encoding='utf-8'))
    assert schema['key'] == spec.section
    for field in schema['fields']:
        for option in field.get('opts', []):
            assert not (isinstance(option, str) and option.startswith('__')), (key, field['key'])


@pytest.mark.parametrize('key', sorted(k for k, s in PANELS.items() if s.runtimeLoadable))
def test_a_runtime_panel_brings_no_keyboard_shortcuts(key):
    """Shortcuts are collected once at startup; a panel added later would
    silently lack its own. None of the runtime-loadable panels has any."""
    for name, table, package in (
        (f'{key}Widget', _WIDGET_MODULES, 'imswitch.imcontrol.view.widgets'),
        (f'{key}Controller', _CONTROLLER_MODULES, 'imswitch.imcontrol.controller.controllers'),
    ):
        cls = getattr(import_module(f'{package}.{table[name]}'), name)
        decorated = [attr for attr in dir(cls)
                     if getattr(getattr(cls, attr, None), '_Shortcut', False)]
        assert not decorated, f'{name} has shortcuts {decorated}'


@pytest.mark.parametrize('key', sorted(PANELS))
def test_a_panel_that_cannot_be_added_says_why(key):
    spec = PANELS[key]
    if not spec.runtimeLoadable:
        assert spec.reason.strip().endswith('.')
    if spec.kind is PanelKind.CONFIGURED:
        assert spec.section
    for peer in spec.requiresPanels + spec.wiredPeers:
        assert peer in PANELS


def test_core_panels_stay_fatal_at_startup():
    assert {'Image', 'Settings', 'View', 'Recording'} <= panels.FATAL_AT_STARTUP
    assert not panels.isFatalAtStartup('Tiling')
    assert not panels.isFatalAtStartup('SomethingUnknown')


def test_build_order_puts_wired_peers_first():
    assert panels.buildOrder(['ViewerTools', 'Console', 'LineProfile']) == [
        'LineProfile', 'ViewerTools', 'Console']
    assert panels.buildOrder(['ViewerTools']) == ['ViewerTools']
