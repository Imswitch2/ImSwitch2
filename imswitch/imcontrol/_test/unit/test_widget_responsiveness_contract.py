from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[4]
MAIN_VIEW_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'ImConMainView.py'
SCAN_BASE_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'ScanWidgetBase.py'
SCAN_ADVANCED_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'ScanWidgetAdvanced.py'
SCAN_ADVANCED_CONTROLLER_PATH = (
    ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'ScanControllerAdvanced.py'
)
LASER_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'LaserWidget.py'
RECORDING_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'RecordingWidget.py'
POSITIONER_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'PositionerWidget.py'
BEAD_REC_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'BeadRecWidget.py'
WIDGETS_DIR = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets'


def test_main_view_keeps_direct_dock_widget_insertion():
    source = MAIN_VIEW_PATH.read_text()

    assert 'def _addScrollableWidgetToDock(self, dock, widget):' not in source
    assert 'class _DockScrollArea(QtWidgets.QScrollArea):' not in source
    assert 'self._addScrollableWidgetToDock(self.docks[widgetKey], self.widgets[widgetKey])' not in source
    assert 'self.docks[widgetKey].addWidget(self.widgets[widgetKey])' in source
    assert "self.docks['Image'].addWidget(self.widgets['Image'])" in source


def test_scan_widget_base_does_not_force_minimum_width_or_hide_horizontal_scrollbar():
    source = SCAN_BASE_PATH.read_text()

    assert 'self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.scrollArea.setMinimumWidth(width)' not in source
    assert 'self.setMinimumWidth(width)' not in source
    assert 'QtCore.Qt.ScrollBarAlwaysOff' not in source


def test_laser_widget_does_not_force_minimum_width_or_hide_horizontal_scrollbar():
    source = LASER_WIDGET_PATH.read_text()

    assert 'self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.scrollArea.setMinimumWidth(width)' not in source
    assert 'self.setMinimumWidth(width)' not in source
    assert 'QtCore.Qt.ScrollBarAlwaysOff' not in source


def test_recording_widget_uses_internal_scroll_area():
    source = RECORDING_WIDGET_PATH.read_text()

    assert 'self.scrollArea = QtWidgets.QScrollArea()' in source
    assert 'self.scrollArea.setWidget(self.recGridContainer)' in source
    assert 'self.scrollArea.setWidgetResizable(True)' in source
    assert 'layout.addWidget(self.scrollArea)' in source
    assert 'self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.setMinimumSize(0, 0)' in source


def test_positioner_widget_uses_internal_scroll_area():
    source = POSITIONER_WIDGET_PATH.read_text()

    assert 'self.gridContainer = QtWidgets.QWidget()' in source
    assert 'self.gridContainer.setLayout(self.grid)' in source
    assert 'self.scrollArea = QtWidgets.QScrollArea()' in source
    assert 'self.scrollArea.setWidget(self.gridContainer)' in source
    assert 'self.scrollArea.setWidgetResizable(True)' in source
    assert 'self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.setMinimumSize(0, 0)' in source


def test_bead_rec_widget_has_responsive_list_and_status_controls():
    source = BEAD_REC_WIDGET_PATH.read_text()

    assert 'listPanel.setMaximumWidth(100)' not in source
    assert 'listPanel.setMinimumWidth(140)' in source
    assert 'self.cwidget.setMinimumSize(0, 0)' in source
    assert 'self.imageListWidget.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.statusLabel = QtWidgets.QLabel("Idle")' in source
    assert 'self.progressBar = QtWidgets.QProgressBar()' in source
    assert 'def setStatusText(self, text):' in source
    assert 'def updateProgress(self, current, total):' in source
    assert 'return name' in source
    assert 'self.setMinimumSize(0, 0)' in source


def test_advanced_scan_centers_allow_negative_positions():
    source = SCAN_ADVANCED_PATH.read_text()

    assert 'centerPar.setRange(-1e9, 1e9)' in source


def test_advanced_scan_has_no_partial_bead_rec_controls():
    widget_source = SCAN_ADVANCED_PATH.read_text()
    controller_source = SCAN_ADVANCED_CONTROLLER_PATH.read_text()

    assert 'sigUpdateBeadRecCenter' not in widget_source
    assert 'sigShowBeadRecCenterCross' not in widget_source
    assert 'sigAutoAxialToggled' not in widget_source
    assert '_showBeadCenterBox' not in widget_source
    assert '_beadCenterXEdit' not in widget_source
    assert '_beadCenterYEdit' not in widget_source
    assert '_emitBeadRecCenter' not in widget_source
    assert 'beadRecWorkflow.update_bead_rec_center' not in controller_source
    assert 'beadRecWorkflow.show_bead_rec_center_cross' not in controller_source
    assert 'beadRecWorkflow.set_auto_axial' not in controller_source


def test_widget_sizing_audit():
    """
    Source-level audit for problematic hard-sizing patterns in widget files.

    This test scans all widget source files for patterns that are known to
    break responsive layouts, specifically:
    - scrollArea.setMinimumWidth(...) - forces scroll areas to minimum width
    - ScrollBarAlwaysOff - prevents scrolling when content overflows

    NOTE: This is a conservative audit focused on narrow, agreed-upon
    problematic patterns. A broader hard-size audit (e.g., all setFixedHeight,
    all setMinimumWidth) would flag many legitimate uses in dialogs, numeric
    inputs, graph controls, and specialized widgets. Those require case-by-case
    review and are documented in docs/design/plans/widget-usability-improvements.md.

    Known legitimate sizing patterns NOT flagged here:
    - Small numeric field widths (e.g., coordinate inputs, power controls)
    - Graph/plot minimum heights (e.g., FFT displays, line profiles)
    - Dialog fixed sizes (e.g., about boxes, simple input dialogs)
    - Specialized widget constraints (e.g., BeadRec list panel minimum width)
    """
    # Patterns to flag (problematic for responsive layouts)
    problematic_patterns = [
        (r'scrollArea\.setMinimumWidth\s*\(', 'scrollArea.setMinimumWidth('),
        (r'ScrollBarAlwaysOff', 'ScrollBarAlwaysOff'),
    ]

    # Files with known, reviewed exceptions (allowlist)
    # These files contain sizing patterns that have been reviewed and deemed
    # legitimate for specific use cases. Add entries here only after review.
    allowlist = {
        'SLMsWidget.py': {
            'ScrollBarAlwaysOff': (
                'Existing pattern: disables horizontal scrolling for SLM image preview. '
                'Consider reviewing if this breaks small-screen usability.'
            ),
        },
        # TriggerScope-family scanner panels: dense fixed-column parameter
        # forms wrapped in a vertical-only scroll area. The min-width keeps all
        # parameter columns visible and the horizontal scrollbar is disabled by
        # design (vertical scrolling only). Reviewed: legitimate for these
        # instrument-control panels.
        'LightSheetMulticolorWidget.py': {
            'scrollArea.setMinimumWidth(': 'Fixed-column scanner parameter panel; keep columns visible.',
            'ScrollBarAlwaysOff': 'Vertical-only scroll for the scanner parameter panel.',
        },
        'TriggerScopeGalvoDetectionWidget.py': {
            'scrollArea.setMinimumWidth(': 'Fixed-column scanner parameter panel; keep columns visible.',
            'ScrollBarAlwaysOff': 'Vertical-only scroll for the scanner parameter panel.',
        },
        'TriggerScopeLSXYRWidget.py': {
            'scrollArea.setMinimumWidth(': 'Fixed-column scanner parameter panel; keep columns visible.',
            'ScrollBarAlwaysOff': 'Vertical-only scroll for the scanner parameter panel.',
        },
        'TriggerScopePLSRMulticolorWidget.py': {
            'scrollArea.setMinimumWidth(': 'Fixed-column scanner parameter panel; keep columns visible.',
            'ScrollBarAlwaysOff': 'Vertical-only scroll for the scanner parameter panel.',
        },
        'TriggerScopePLSRWidget.py': {
            'scrollArea.setMinimumWidth(': 'Fixed-column scanner parameter panel; keep columns visible.',
            'ScrollBarAlwaysOff': 'Vertical-only scroll for the scanner parameter panel.',
        },
        'TriggerScopeRasterWidget.py': {
            'scrollArea.setMinimumWidth(': 'Fixed-column scanner parameter panel; keep columns visible.',
            'ScrollBarAlwaysOff': 'Vertical-only scroll for the scanner parameter panel.',
        },
    }

    violations = []
    widget_files = sorted(WIDGETS_DIR.glob('*.py'))
    assert len(widget_files) > 0, "No widget files found for audit"

    for widget_file in widget_files:
        if widget_file.name in ('__init__.py', 'basewidgets.py'):
            continue

        source = widget_file.read_text()
        file_allowlist = allowlist.get(widget_file.name, {})

        for pattern_re, pattern_name in problematic_patterns:
            # Skip if this pattern is allowlisted for this file
            if pattern_name in file_allowlist:
                continue

            matches = list(re.finditer(pattern_re, source))
            if matches:
                for match in matches:
                    # Find line number
                    line_num = source[:match.start()].count('\n') + 1
                    # Get context line
                    lines = source.split('\n')
                    context_line = lines[line_num - 1].strip() if line_num <= len(lines) else ''

                    violations.append(
                        f"{widget_file.name}:{line_num}: {pattern_name}\n"
                        f"  Context: {context_line}"
                    )

    # Generate detailed failure message if violations found
    if violations:
        msg = (
            "\nProblematic widget sizing patterns found:\n\n"
            + "\n\n".join(violations)
            + "\n\nThese patterns break responsive layouts. "
            "If a use is legitimate, add it to the allowlist in this test "
            "with a clear justification."
        )
        assert False, msg
