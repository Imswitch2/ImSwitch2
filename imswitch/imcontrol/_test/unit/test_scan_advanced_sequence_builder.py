from pathlib import Path
from types import SimpleNamespace

from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)


ROOT = Path(__file__).resolve().parents[4]
WIDGET_PATH = ROOT / "imswitch" / "imcontrol" / "view" / "widgets" / "ScanWidgetAdvanced.py"
CONTROLLER_PATH = (
    ROOT / "imswitch" / "imcontrol" / "controller" / "controllers" / "ScanControllerAdvanced.py"
)
# The UI-state <-> scan-dict translation moved to this controller-free service
# (audit 07). Widget-access assertions live there now.
SERIALIZER_PATH = (
    ROOT / "imswitch" / "imcontrol" / "model" / "scan_parameters.py"
)


def _method_body(source: str, name: str) -> str:
    marker = f"    def {name}("
    start = source.index(marker)
    next_method = source.find("\n    def ", start + len(marker))
    return source[start:] if next_method == -1 else source[start:next_method]


def test_line_program_checkbox_only_changes_visibility():
    source = WIDGET_PATH.read_text()
    body = _method_body(source, "_onLineProgramDevicesChanged")

    assert "_refreshLineProgramDevicesVisibility()" in body
    assert "_refreshAdvancedProgramMode()" in body
    assert "setLineStepEnabled" not in body
    assert "unsetTTL" not in body
    assert ".setChecked(" not in body
    assert "sigSignalParChanged.emit" not in body


def test_sequence_builder_state_keys_are_saved_and_restored():
    # The serializer owns the widget <-> dict translation for these keys.
    source = SERIALIZER_PATH.read_text()

    for key in (
        "linestep_power_enabled",
        "advanced_program_mode",
        "advanced_sequence_rows",
        "line_program_devices_enabled",
        "advanced_device_lock_master",
        "advanced_device_lock_target",
    ):
        assert key in source

    assert "widget.getAdvancedSequenceRows()" in source
    assert "widget.setAdvancedSequenceRows(" in source
    assert "widget.setLineProgramDevicesMode(" in source


def test_sequence_builder_start_delay_allows_negative_offsets():
    source = WIDGET_PATH.read_text()

    assert '["Device(s)", "Start delay (ms)", "Duration (ms)", "Value"]' in source
    assert "offset.setMinimum(-1e6)" in source
    assert "start_ms = previous_end_ms + offset_ms" in source


def test_pixel_graph_time_unit_selection_is_reused_for_replot():
    source = WIDGET_PATH.read_text()

    assert 'self._pixelXAxisUnitCombo.addItems(["ms", "us", "samples"])' in source
    assert "self._lastPixelPlotArgs = (list(labels), list(colors), sampleRate)" in source
    assert "def _onPixelXAxisUnitChanged(self):" in source
    assert "self._plotPerPixelProgram(labels=labels, colors=colors, sampleRate=sampleRate)" in source
    assert 'return "Time within single dwell time (us)"' in source


def test_sequence_deadtime_footer_does_not_share_value_editor_row():
    source = WIDGET_PATH.read_text()

    assert "advLayout.addWidget(self._analogLevelEdit, 5, 2, 1, 2)" in source
    assert "advLayout.addWidget(self._sequenceButtons, 5, 0, 1, 4" not in source
    assert "advLayout.addWidget(self._sequenceButtons, 6, 0, 1, 4" in source
    assert "advLayout.addWidget(self._pixelAxisUnitWidget, 6, 4, 1, 4" in source


def test_advanced_ttl_targets_filter_scanning_positioners():
    setup_info = SimpleNamespace(
        positioners={
            "x_stage": SimpleNamespace(forScanning=True),
            "filter_wheel": SimpleNamespace(forScanning=False),
        }
    )

    assert AdvancedScanTTLCycleDesigner._ttl_targets(
        ["laser", "x_stage", "filter_wheel"], setup_info
    ) == ["laser", "filter_wheel"]


def test_disabled_power_modulation_skips_scan_ao_injection():
    source = CONTROLLER_PATH.read_text()
    body = _method_body(source, "_inject_linestep_power_ao")

    assert 'power_enabled = (TTLParameters or {}).get("linestep_power_enabled", {}) or {}' in body
    assert 'if not bool(power_enabled.get(laserName, True)):' in body
    assert 'continue' in body
