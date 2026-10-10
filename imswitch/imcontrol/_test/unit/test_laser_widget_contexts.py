from imswitch.imcontrol.view.widgets.LaserWidget import LaserWidget


def _widget(qtbot):
    widget = LaserWidget()
    qtbot.addWidget(widget)
    widget.addLaser(
        '488', 'mW', 1, 488,
        valueRange=(0.0, 100.0),
        valueRangeStep=0.5,
        frequencyRange=(1, 1000, 10),
    )
    return widget


def test_context_switch_is_separate_from_preset_loading(qtbot):
    widget = _widget(qtbot)
    loaded = []
    contexts = []
    widget.sigPresetSelected.connect(loaded.append)
    widget.sigPowerContextChanged.connect(contexts.append)

    widget.setPresetNames(['Live A', 'Live B'], selected='Live A')
    widget.setPowerContext('scan', emitSignal=False)
    widget.setPresetNames(['Scan A'], selected='Scan A')

    assert contexts == []
    assert loaded == []
    assert widget.getPowerContext() == 'scan'
    assert widget.getCurrentPreset() == 'Scan A'


def test_scan_view_hides_live_controls_and_ao_replaces_direct_value(qtbot):
    widget = _widget(qtbot)
    module = widget.laserModules['488']

    widget.setPowerContext('scan', emitSignal=False)
    widget.setLaserAOOwned(
        '488', True,
        tooltip='Power controlled through analog output by the scan configuration.',
    )

    assert module.enableButton.isHidden()
    assert module.modulationGroup.isHidden()
    assert not module.aoIndicator.isHidden()
    assert module.setPointEdit.isHidden()
    assert module.slider.isHidden()

    widget.setLaserAOOwned('488', False)
    widget.setLaserPowerEditable('488', True)
    assert module.aoIndicator.isHidden()
    assert not module.setPointEdit.isHidden()
    assert not module.slider.isHidden()


def test_inactive_scan_power_is_muted_but_stays_editable(qtbot):
    widget = _widget(qtbot)
    module = widget.laserModules['488']
    widget.setPowerContext('scan', emitSignal=False)

    widget.setLaserScanActive('488', False)
    widget.setLaserPowerEditable('488', True)

    assert module.setPointEdit.isEnabled()
    assert module.slider.isEnabled()
    assert module._powerOpacity.opacity() < 1.0


def test_context_bar_is_fixed_above_scroll_area_and_prominent(qtbot):
    widget = _widget(qtbot)

    assert widget.layout.itemAtPosition(0, 0).layout() is widget.topBar
    assert widget.layout.itemAtPosition(1, 0).widget() is widget.scrollArea
    assert widget.liveModeButton.minimumHeight() >= 38
    assert widget.scanModeButton.minimumHeight() >= 38
    assert widget.liveModeButton.minimumWidth() >= 115
    assert widget.scanModeButton.minimumWidth() >= 115
    assert widget.liveModeButton.font().bold()
    assert widget.scanModeButton.font().bold()


def test_scan_context_shows_amber_editing_banner_without_restyling_content(qtbot):
    widget = _widget(qtbot)
    default_style = widget.lasersGridContainer.styleSheet()

    assert widget.scanEditingLabel.isHidden()
    widget.setPowerContext('scan', emitSignal=False)

    assert not widget.scanEditingLabel.isHidden()
    assert widget.scanEditingLabel.text() == 'EDITING SCAN POWERS'
    assert '#d6b15d' in widget.scanEditingLabel.styleSheet().lower()
    assert widget.lasersGridContainer.styleSheet() == default_style

    widget.setPowerContext('live', emitSignal=False)
    assert widget.scanEditingLabel.isHidden()
    assert widget.lasersGridContainer.styleSheet() == default_style


def test_scan_active_state_has_no_per_row_badge(qtbot):
    widget = _widget(qtbot)
    module = widget.laserModules['488']

    assert not hasattr(widget, 'scanActiveBadges')
    widget.setPowerContext('scan', emitSignal=False)
    widget.setLaserScanActive('488', True)
    assert module._powerOpacity.opacity() == 1.0
    widget.setLaserScanActive('488', False)
    assert module._powerOpacity.opacity() < 1.0


def test_alternate_context_status_does_not_consume_row_width(qtbot):
    widget = _widget(qtbot)
    module = widget.laserModules['488']

    widget.setLaserSecondaryStatus(
        '488', 'AO', emphasized=True,
        tooltip='Power controlled through analog output by the scan configuration.',
        badge=True,
    )

    assert module.secondaryFrame.isHidden()
    assert module.layout.indexOf(module.secondaryFrame) == -1


def test_resume_live_button_is_context_independent_and_temporary(qtbot):
    widget = _widget(qtbot)
    clicked = []
    widget.sigResumeLiveClicked.connect(lambda: clicked.append(True))

    assert widget.resumeLiveButton.isHidden()
    widget.setResumeLiveAvailable(True)
    assert not widget.resumeLiveButton.isHidden()
    assert widget.resumeLiveButton.text() == '↶ Resume Live'

    widget.setPowerContext('scan', emitSignal=False)
    assert not widget.resumeLiveButton.isHidden()
    widget.resumeLiveButton.click()
    assert clicked == [True]

    widget.setPowerContext('live', emitSignal=False)
    assert not widget.resumeLiveButton.isHidden()
    widget.setResumeLiveAvailable(False)
    assert widget.resumeLiveButton.isHidden()


def test_coupled_mode_keeps_sticky_presets_but_hides_separated_controls(qtbot):
    widget = _widget(qtbot)
    widget.setResumeLiveAvailable(True)
    widget.setPowerContext('scan', emitSignal=False)

    widget.setPowerMode('coupled')

    assert widget.contextFrame.isHidden()
    assert widget.scanEditingLabel.isHidden()
    assert widget.resumeLiveButton.isHidden()
    assert widget.getPowerContext() == 'live'
    assert widget.presetsList.isVisible() or not widget.isVisible()
    module = widget.laserModules['488']
    assert not module.enableButton.isHidden()
    assert not module.modulationGroup.isHidden()


def test_separated_mode_restores_context_controls(qtbot):
    widget = _widget(qtbot)
    widget.setPowerMode('coupled')
    widget.setPowerMode('separated')

    assert not widget.contextFrame.isHidden()
    widget.setPowerContext('scan', emitSignal=False)
    assert not widget.scanEditingLabel.isHidden()


def test_preset_header_restores_label_reload_and_wider_combo(qtbot):
    widget = _widget(qtbot)

    assert widget.presetsLabel.text() == 'Presets:'
    assert widget.presetsList.minimumWidth() >= 200
    assert widget.reloadPresetButton.toolTip() == 'Reload selected preset'
    assert not widget.reloadPresetButton.isEnabled()

    widget.setPresetNames(['Screening'], selected='Screening')
    assert widget.reloadPresetButton.isEnabled()

    widget.setPowerMode('coupled')
    assert not widget.presetsLabel.isHidden()
    widget.setPowerMode('separated')
    assert not widget.presetsLabel.isHidden()


def test_reload_button_reloads_current_preset_through_existing_load_signal(qtbot):
    widget = _widget(qtbot)
    loaded = []
    widget.sigPresetSelected.connect(loaded.append)

    widget.setPresetNames(['Screening', 'Acquisition'], selected='Screening')
    assert loaded == []

    widget.reloadPresetButton.click()
    assert loaded == ['Screening']

    widget.setCurrentPreset(None)
    assert not widget.reloadPresetButton.isEnabled()


def test_scan_editing_banner_is_adjacent_to_context_selector(qtbot):
    widget = _widget(qtbot)

    context_index = widget.topBar.indexOf(widget.contextFrame)
    banner_index = widget.topBar.indexOf(widget.scanEditingLabel)
    preset_label_index = widget.topBar.indexOf(widget.presetsLabel)

    assert banner_index == context_index + 1
    assert preset_label_index > banner_index
