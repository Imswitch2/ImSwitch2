import pytest

from imswitch.imcontrol.model import configfiletools


def test_setup_list_is_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(configfiletools, '_setupFilesDir', str(tmp_path))
    for name in ('z_scope.json', 'a_scope.json', 'm_scope.json'):
        (tmp_path / name).write_text('{}')

    assert configfiletools.getSetupList() == [
        'a_scope.json',
        'm_scope.json',
        'z_scope.json',
    ]


def test_first_run_prefers_no_hardware_setup(tmp_path, monkeypatch):
    setup_dir = tmp_path / 'setups'
    setup_dir.mkdir()
    for name in ('example_coolLED.json', 'example_no_hardware.json', 'other.json'):
        (setup_dir / name).write_text('{}')

    monkeypatch.setattr(configfiletools, '_setupFilesDir', str(setup_dir))
    monkeypatch.setattr(configfiletools, '_optionsFilePath', str(tmp_path / 'options.json'))
    monkeypatch.setattr(configfiletools, '_options', None)

    options, did_not_exist = configfiletools.loadOptions()

    assert did_not_exist is True
    assert options.setupFileName == 'example_no_hardware.json'


def test_first_run_falls_back_to_first_sorted_setup(tmp_path, monkeypatch):
    setup_dir = tmp_path / 'setups'
    setup_dir.mkdir()
    for name in ('z_scope.json', 'a_scope.json'):
        (setup_dir / name).write_text('{}')

    monkeypatch.setattr(configfiletools, '_setupFilesDir', str(setup_dir))
    monkeypatch.setattr(configfiletools, '_optionsFilePath', str(tmp_path / 'options.json'))
    monkeypatch.setattr(configfiletools, '_options', None)

    options, _ = configfiletools.loadOptions()

    assert options.setupFileName == 'a_scope.json'


def test_imcontrol_load_error_view_exposes_hardware_recovery(qtbot):
    pytest.importorskip('PyQt5')
    from qtpy import QtCore, QtWidgets
    from imswitch.imcontrol.view.ImControlLoadErrorView import ImControlLoadErrorView

    view = ImControlLoadErrorView(RuntimeError('camera failed'))
    qtbot.addWidget(view)

    menus = view.menuBar().findChildren(
        QtWidgets.QMenu,
        None,
        QtCore.Qt.FindDirectChildrenOnly,
    )
    hardware = next(menu for menu in menus if menu.title() == '&Hardware')

    assert [action.text() for action in hardware.actions()] == [
        'Pick hardware setup…',
        'Edit hardware configuration…',
    ]
    assert 'Hardware Control could not start' in view.recoveryHeaderLabel.text()
    assert 'choose another setup' in view.recoveryHelpLabel.text()
    assert view.pickSetupButton.text() == 'Pick hardware setup…'
    assert view.configEditorButton.text() == 'Edit hardware configuration…'
    assert 'Startup error details' in view.errorHeaderLabel.text()
    assert 'RuntimeError: camera failed' in view.errorMessage.toPlainText()
