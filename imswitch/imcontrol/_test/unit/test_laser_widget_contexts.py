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

    widget.setPresetNames(['Manual A', 'Manual B'], selected='Manual A')
    widget.setPowerContext('scan', emitSignal=False)
    widget.setPresetNames(['Scan A'], selected='Scan A')

    assert contexts == []
    assert loaded == []
    assert widget.getPowerContext() == 'scan'
    assert widget.getCurrentPreset() == 'Scan A'


def test_scan_view_hides_manual_controls_and_ao_replaces_direct_value(qtbot):
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
    assert widget.manualModeButton.minimumHeight() >= 38
    assert widget.scanModeButton.minimumHeight() >= 38
    assert widget.manualModeButton.minimumWidth() >= 115
    assert widget.scanModeButton.minimumWidth() >= 115
    assert widget.manualModeButton.font().bold()
    assert widget.scanModeButton.font().bold()


def test_scan_context_shows_amber_editing_banner_without_restyling_content(qtbot):
    widget = _widget(qtbot)
    default_style = widget.lasersGridContainer.styleSheet()

    assert widget.scanEditingLabel.isHidden()
    widget.setPowerContext('scan', emitSignal=False)

    assert not widget.scanEditingLabel.isHidden()
    assert widget.scanEditingLabel.text() == 'Editing scan powers'
    assert '#d6b15d' in widget.scanEditingLabel.styleSheet().lower()
    assert widget.lasersGridContainer.styleSheet() == default_style

    widget.setPowerContext('manual', emitSignal=False)
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
