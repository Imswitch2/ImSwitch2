import os

import pytest

from imswitch.imcommon.model.dirtools import DataFileDirs
from imswitch.imcontrol.view.guitools.ViewSetupInfo import ViewSetupInfo


def _example_setup_files():
    setup_dir = os.path.join(DataFileDirs.UserDefaults, 'imcontrol_setups')
    return [
        os.path.join(setup_dir, file_name)
        for file_name in sorted(os.listdir(setup_dir))
        if file_name.endswith('.json')
    ]


@pytest.mark.parametrize('setup_path', _example_setup_files(), ids=os.path.basename)
def test_bundled_example_setup_schema_parses(setup_path):
    with open(setup_path) as setup_file:
        setup_info = ViewSetupInfo.from_json(setup_file.read())

    assert setup_info is not None


def _get_registered_widgets():
    """Return the set of registered widget keys from the lazy export map."""
    from imswitch.imcontrol.view import widgets
    # Widget keys in the export map have 'Widget' suffix; strip it for comparison
    # Special case: ScanWidget variants (ScanWidgetPointScan, etc.) all map to key 'Scan'
    keys = set()
    for key in widgets._WIDGET_MODULES.keys():
        if key == 'WidgetFactory':
            continue
        if key.startswith('ScanWidget'):
            keys.add('Scan')
        elif key.endswith('Widget'):
            keys.add(key[:-6])
    return keys


def _get_registered_controllers():
    """Return the set of registered controller keys from the lazy export map."""
    from imswitch.imcontrol.controller import controllers
    # Controller keys in the export map have 'Controller' suffix; strip it for comparison
    # Special case: ScanController variants (ScanControllerPointScan, etc.) all map to key 'Scan'
    keys = set()
    for key in controllers._CONTROLLER_MODULES.keys():
        if key.startswith('ScanController'):
            keys.add('Scan')
        elif key.endswith('Controller'):
            keys.add(key[:-10])
    return keys


def _get_default_layout_keys():
    """Return all widget keys from the built-in default layout."""
    from imswitch.imcontrol.view.ImConMainView import (
        _DEFAULT_RIGHT_DOCK_INFOS,
        _DEFAULT_LEFT_DOCK_INFOS,
    )
    return set(_DEFAULT_RIGHT_DOCK_INFOS.keys()) | set(_DEFAULT_LEFT_DOCK_INFOS.keys())


@pytest.mark.parametrize('setup_path', _example_setup_files(), ids=os.path.basename)
def test_bundled_setup_available_widgets_resolve(setup_path):
    """Verify that every availableWidgets entry resolves to a registered widget AND controller."""
    with open(setup_path) as setup_file:
        setup_info = ViewSetupInfo.from_json(setup_file.read())

    if not isinstance(setup_info.availableWidgets, list):
        # If availableWidgets is True or not a list, skip this test
        return

    registered_widgets = _get_registered_widgets()
    registered_controllers = _get_registered_controllers()

    for widget_key in setup_info.availableWidgets:
        assert widget_key in registered_widgets, \
            f"{widget_key} in availableWidgets but {widget_key}Widget not in lazy export map ({setup_path})"
        assert widget_key in registered_controllers, \
            f"{widget_key} in availableWidgets but {widget_key}Controller not in lazy export map ({setup_path})"


@pytest.mark.parametrize('setup_path', _example_setup_files(), ids=os.path.basename)
def test_bundled_setup_widget_layout_resolves(setup_path):
    """Verify that every widgetLayout key resolves to a registered widget AND controller."""
    with open(setup_path) as setup_file:
        setup_info = ViewSetupInfo.from_json(setup_file.read())

    if setup_info.widgetLayout is None:
        # If no custom layout, skip this test
        return

    registered_widgets = _get_registered_widgets()
    registered_controllers = _get_registered_controllers()

    # Collect all widget keys from the layout (both left and right panels)
    layout_keys = set()
    for tab_group in setup_info.widgetLayout.right:
        layout_keys.update(tab_group)
    for tab_group in setup_info.widgetLayout.left:
        layout_keys.update(tab_group)

    for widget_key in layout_keys:
        assert widget_key in registered_widgets, \
            f"{widget_key} in widgetLayout but {widget_key}Widget not in lazy export map ({setup_path})"
        assert widget_key in registered_controllers, \
            f"{widget_key} in widgetLayout but {widget_key}Controller not in lazy export map ({setup_path})"


def test_default_layout_keys_resolve():
    """Verify that every key in the built-in default layout resolves to a registered widget AND controller."""
    registered_widgets = _get_registered_widgets()
    registered_controllers = _get_registered_controllers()
    default_layout_keys = _get_default_layout_keys()

    for widget_key in default_layout_keys:
        assert widget_key in registered_widgets, \
            f"{widget_key} in default layout but {widget_key}Widget not in lazy export map"
        assert widget_key in registered_controllers, \
            f"{widget_key} in default layout but {widget_key}Controller not in lazy export map"
