"""The Expert panel's Simple view (docs/design/plans/expert-simple-view.md,
phase P1): the same scan read as a list -- a scan made from a template shows
the template's own form -- with the pulses drawn and the preview, and nothing
that adds, removes or reorders.

On the simulated rig (``ExpertRig``). Every modal is stubbed before anything
can open it: the template's form (``askPresetParams``), the approval of a
script (``askApproveScript``, and the approvals store), the file dialog and
the unreferenced-axes question -- a modal would hold the run at 0 % CPU.
"""

import copy
import json
import random
import struct

import pytest
from qtpy import QtCore, QtGui, QtTest, QtWidgets, sip

from imswitch.imcommon.model.state_contracts import (
    ComponentStateApplyMode, RestoreWarning, isCriticalRestoreWarning,
)
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.model.scan_program import presets
from imswitch.imcontrol.model.scan_program.script_approval import (
    FILENAME, ScriptApprovals, set_approvals,
)
from imswitch.imcontrol.model.scan_program.serialize import program_to_dict
from imswitch.imcontrol.view import guitools
from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
from imswitch.imcontrol.view.widgets.ScanWidgetExpert import (
    MS, VIEW_EXPERT, VIEW_SIMPLE, VIEWS, PresetDialog, TemplateForm, _Column, _RowTable,
    _SummaryEditor, exact_text, stored,
)

from .test_scan_controller_expert import ExpertRig, _advanced_state, _ms_resolft, _setup_dict
from .test_scan_widget_expert_composer import _centre, _drag, _event, _send_mouse

STARTUP = ComponentStateApplyMode.STARTUP_RESTORE
SETUP_MODE = ComponentStateApplyMode.SETUP_MODE_APPLY
TEMPLATE = 'MS-RESOLFT (cycles x planes)'


# ---------------------------------------------------------------------------
# The rig, with every modal stubbed
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    previous = set_approvals(ScriptApprovals(str(tmp_path / 'config' / FILENAME)))
    yield
    set_approvals(previous)


@pytest.fixture
def rig(qtbot, store, monkeypatch):
    rig = ExpertRig()
    rig.asked = []             # (template id, the values offered) of each form asked for
    rig.fileDialogs = []       # what the file dialog was asked for
    rig.fileAnswer = ''        # the file it answers with: none, as when cancelled
    rig.widget.askPresetParams = lambda preset, initial, validate, **_rig: \
        rig.asked.append((preset.id, copy.deepcopy(initial)))
    rig.widget.askApproveScript = lambda code: False
    rig.widget.confirmUnreferencedScan = lambda unreferenced: (True, False)

    def ask_for_file(*args, **kwargs):
        rig.fileDialogs.append(args[1] if len(args) > 1 else kwargs)
        return rig.fileAnswer

    monkeypatch.setattr(guitools, 'askForFilePath', ask_for_file)
    rig.scan.setProgram(_ms_resolft(rig))
    yield rig
    rig.close()


@pytest.fixture
def shown(rig, qtbot):
    """The panel shown and its window active: focus is real."""
    rig.widget.resize(900, 900)
    rig.widget.show()
    qtbot.waitExposed(rig.widget)
    rig.widget.activateWindow()
    qtbot.waitUntil(rig.widget.isActiveWindow, timeout=5000)
    yield rig
    rig.widget.close()


def _settle(rig, qtbot):
    """An edit made in the Expert parts, reported."""
    rig.widget.flushRefresh()
    qtbot.waitUntil(lambda: not rig.scan._reportTimer.isActive(), timeout=5000)
    qtbot.wait(20)


def _simple(rig):
    rig.widget.setView(VIEW_SIMPLE)
    return rig.widget.parameters.form()


def _editor(form, name):
    return form.editors[name][1]


def _type(field, text):
    """Type into a field and stay in it: nothing commits it yet."""
    field.setFocus()
    assert field.hasFocus(), 'the panel must be shown with its window active'
    field.selectAll()
    QtTest.QTest.keyClicks(field, text)


def _enter(field, text):
    """Type into a field and press Return."""
    _type(field, text)
    QtTest.QTest.keyClick(field, QtCore.Qt.Key_Return)


def _leave(field, text):
    """What typing ``text`` and leaving the field does, on a panel that is not
    shown: the text is changed and the field says it was edited."""
    field.setText(text)
    field.setModified(True)
    field.editingFinished.emit()


def _shown_values(form):
    """What each editor of the form shows."""
    out = {}
    for name, (field, editor) in form.editors.items():
        if isinstance(editor, QtWidgets.QCheckBox):
            out[name] = editor.isChecked()
        elif isinstance(editor, QtWidgets.QComboBox):
            out[name] = editor.currentText()
        elif isinstance(editor, _RowTable):
            out[name] = editor.rows(raw=True)
        elif isinstance(editor, _SummaryEditor):
            out[name] = editor.summary.text()
        else:
            out[name] = editor.text()
    return out


def _labels(form_layout):
    """A form layout's rows as one reads them: a group title, or a label."""
    out = []
    for row in range(form_layout.rowCount()):
        span = form_layout.itemAt(row, QtWidgets.QFormLayout.SpanningRole)
        if span is not None:
            out.append(('title', span.widget().text()))
        else:
            out.append(('label', form_layout.itemAt(
                row, QtWidgets.QFormLayout.LabelRole).widget().text()))
    return out


def _chips(rig):
    return rig.widget.order._chips


def _made(rig, **changes):
    """What the template makes from the MS-RESOLFT values of ``_ms_resolft``,
    with ``changes``."""
    params = copy.deepcopy(_ms_resolft(rig).requested_as['params'])
    params.update(changes)
    return presets.make('ms-resolft', params)


PARTS = ('start', 'trajectoryCard', 'parameters', 'order', 'positions', 'step', 'waitsCard',
         'recording', 'check', 'preview', 'undoButton', 'redoButton', 'tablesButton',
         'templateValuesButton')


def _hidden(w):
    return {name: getattr(w, name).isHidden() for name in PARTS}


# ---------------------------------------------------------------------------
# 1. The switch
# ---------------------------------------------------------------------------

def test_the_panel_opens_in_expert_and_simple_hides_what_adds_or_restructures(shown, qtbot):
    w = shown.widget
    assert w.view() == VIEW_EXPERT and VIEWS == (VIEW_SIMPLE, VIEW_EXPERT)
    assert w.expertViewButton.isChecked() and not w.simpleViewButton.isChecked()
    assert (w.simpleViewButton.objectName(), w.expertViewButton.objectName()) == (
        'expertViewSimple', 'expertViewExpert')
    expert = _hidden(w)
    assert not any(hidden for name, hidden in expert.items()
                   if name in ('order', 'positions', 'step', 'waitsCard', 'recording', 'check',
                               'preview', 'undoButton', 'redoButton', 'tablesButton',
                               'templateValuesButton'))
    assert expert['parameters'] and expert['start'] and expert['trajectoryCard']
    assert all(shortcut.isEnabled() for shortcut in w._shortcuts)
    qtbot.wait(50)
    place = w.simpleViewButton.mapTo(w, QtCore.QPoint(0, 0))
    w.tablesButton.click()                                  # Tables open, to be closed
    assert w.pages.currentIndex() == 1

    w.simpleViewButton.click()                              # the switch itself
    qtbot.wait(50)
    assert w.view() == VIEW_SIMPLE
    assert w.simpleViewButton.isChecked() and not w.expertViewButton.isChecked()
    simple = _hidden(w)
    for name in ('order', 'positions', 'waitsCard', 'recording', 'start', 'trajectoryCard',
                 'undoButton', 'redoButton', 'tablesButton', 'check', 'templateValuesButton'):
        assert simple[name], name
    for name in ('parameters', 'step', 'preview'):
        assert not simple[name], name
    assert w.pages.currentIndex() == 0 and not w.tablesButton.isChecked()
    assert not any(shortcut.isEnabled() for shortcut in w._shortcuts)
    assert w.simpleViewButton.mapTo(w, QtCore.QPoint(0, 0)) == place     # under the cursor

    w.expertViewButton.click()
    qtbot.wait(50)
    assert w.view() == VIEW_EXPERT and w.expertViewButton.isChecked()
    assert _hidden(w) == expert                              # every part as it was
    assert all(shortcut.isEnabled() for shortcut in w._shortcuts)
    assert w.simpleViewButton.mapTo(w, QtCore.QPoint(0, 0)) == place
    # ... and editable again: a bar dragged moves its event
    canvas = w.step.canvas
    first = _event(shown, 0)
    start = _centre(canvas._barRect(first))
    _drag(canvas, start, start + QtCore.QPoint(37, 0))
    assert w.timeline.rows()[0]['start_s'] > first['start_s']
    assert not w.step.inspector.isHidden() and not w.step.hint.isHidden()


def test_a_view_that_is_not_one_is_refused_and_the_same_view_changes_nothing(rig):
    w = rig.widget
    changed = []
    w.sigViewChanged.connect(changed.append)
    with pytest.raises(ValueError, match="Unknown view 'wide'"):
        w.setView('wide')
    w.setView(VIEW_EXPERT)                                   # already there
    assert changed == [] and w.expertViewButton.isChecked()
    w.setView(VIEW_SIMPLE)
    w.setView(VIEW_SIMPLE)
    assert changed == [VIEW_SIMPLE] and w.view() == VIEW_SIMPLE


def test_an_empty_scan_in_simple_offers_the_templates_and_no_way_to_build_one(rig):
    w = rig.widget
    rig.scan.setProgram(None)
    assert 'or add a dimension' in w.reportText()           # Expert: it can add one
    assert not w.start.buildHost.isHidden()
    w.setView(VIEW_SIMPLE)
    assert not w.start.isHidden() and w.start.buildHost.isHidden()
    assert w.parameters.isHidden() and w.check.isHidden() and w.step.isHidden()
    assert rig.scan._programError == 'The scan has no dimensions yet: start from a template.'
    assert w.reportText().endswith('start from a template.')
    assert 'add a dimension' not in w.reportText()
    w.setView(VIEW_EXPERT)
    assert not w.start.buildHost.isHidden()
    assert 'or add a dimension' in w.reportText()


# ---------------------------------------------------------------------------
# 2. A template's scan lists the template's fields
# ---------------------------------------------------------------------------

def test_a_templates_scan_lists_the_templates_fields_with_the_scans_values(rig):
    w = rig.widget
    form = _simple(rig)
    assert isinstance(form, TemplateForm) and w.parameters.showsTemplate()
    assert w.parameters.valuesLabel.text() == "the template's values"
    dialog = PresetDialog(presets.PRESETS['ms-resolft'], w.devices,
                          rig.scan.program().requested_as['params'], lambda p: '')
    assert _labels(form.layout().itemAt(0).layout()) == _labels(
        dialog.layout().itemAt(1).layout())
    assert [text for kind, text in _labels(form.layout().itemAt(0).layout())
            if kind == 'title'] == ['Positions', 'Pulses', 'Camera', 'Time lapse', 'Motion']
    labels = [text for kind, text in _labels(form.layout().itemAt(0).layout())
              if kind == 'label']
    assert 'Pause before each timepoint (ms)' in labels and 'Position spacing (µm)' in labels
    lasers = rig.lasers
    assert _shown_values(form) == {
        'device': 'Z', 'cycles': '2', 'planes': '3', 'cycle_step_um': '0.25', 'start_um': '1',
        'pulses': [{'device': lasers[0], 'on': '1', 'gap': '1'},
                   {'device': lasers[1], 'on': '2', 'gap': '1'},
                   {'device': lasers[1], 'on': '1', 'gap': '0'}],
        'camera': 'Camera', 'exposure_pulse': '3', 'timepoints': '2', 'pause_s': '0',
        'move_s': '0.5', 'settle_s': '0.5', 'transition': 'jump'}
    assert w.templateValuesButton.isHidden()                 # the form is that button
    assert w.titleLabel.text() == TEMPLATE
    w.setView(VIEW_EXPERT)
    assert not w.templateValuesButton.isHidden() and w.parameters.isHidden()


# ---------------------------------------------------------------------------
# 3. Each committed field makes the scan again from the template
# ---------------------------------------------------------------------------

def test_a_typed_value_makes_the_scan_again_from_its_template(shown):
    rig, w = shown, shown.widget
    before = rig.scan.program()
    form = _simple(rig)
    _enter(_editor(form, 'cycles'), '3')
    assert rig.scan.program() == _made(rig, cycles=3)
    assert rig.scan.program().requested_as['params']['cycles'] == 3
    assert w.titleLabel.text() == TEMPLATE and not w.titleLabel.text().endswith('edited')
    assert w.subtitleLabel.text() == '2 timepoints › 3 cycles › 3 planes'
    assert rig.scan._templateValues['ms-resolft'] == dict(
        copy.deepcopy(before.requested_as['params']), cycles=3)
    assert w.parameters.form() is form and w.parameters.showsTemplate()   # not rebuilt
    assert w.statusChip.text().startswith('Ready to run') and form.problem.isHidden()
    assert w.undoButton.isHidden() and not w.undoButton.isEnabled()      # undo starts again
    assert _editor(form, 'cycles').text() == '3'


def test_leaving_a_typed_field_commits_it_and_so_does_a_choice_picked(shown):
    rig, w = shown, shown.widget
    form = _simple(rig)
    _type(_editor(form, 'planes'), '4')
    _editor(form, 'cycles').setFocus()                       # leaving the field commits it
    assert rig.scan.program() == _made(rig, planes=4)
    combo = _editor(form, 'transition')
    combo.setCurrentText('smooth')                           # a choice picked
    assert rig.scan.program() == _made(rig, planes=4, transition='smooth')
    assert w.parameters.form() is form


# ---------------------------------------------------------------------------
# 4. A refused value
# ---------------------------------------------------------------------------

def test_a_refused_value_leaves_the_scan_as_it_was_and_not_runnable(shown, qtbot):
    rig, w = shown, shown.widget
    before = rig.scan.program()
    form = _simple(rig)
    field = _editor(form, 'cycles')

    def refused(reason):
        assert not form.problem.isHidden() and form.problem.text() == reason
        assert w.statusChip.text().startswith('Cannot run')
        assert reason in w.noticeLabel.text() and not w.noticeLabel.isHidden()
        assert w.check.isHidden()                  # the form's own: under it and in the header
        rig.rejected.clear()
        rig.ended.clear()
        rig.scan.runScanAdvanced()                 # Run refuses with it, runs nothing
        assert any(reason in str(r) for r in rig.rejected), rig.rejected
        assert not rig.scan.isRunning and rig.ended == []
        rig.scan.plotProgram()                     # so does the preview
        assert w.plot.isHidden() and reason in w.preview.problem.text()
        w.saveScanBtn.click()                      # and Save: nothing is written
        assert rig.fileDialogs == [] and w.noticeLabel.text() == f'Not saved: {reason}'
        assert rig.scan.program() == before        # the scan is as it was ...
        assert rig.scan.program().requested_as == before.requested_as

    _enter(field, 'x')
    refused("Cycles: 'x' is not a whole number")
    assert field.text() == 'x'                     # the typed value is still shown
    _enter(field, '0')
    refused('Cycles must be a whole number of at least 1.')
    assert field.text() == '0'

    _enter(field, '4')                             # a value that makes a program clears it
    assert form.problem.isHidden() and rig.scan._templateProblem == ''
    assert rig.scan.program() == _made(rig, cycles=4)
    assert w.statusChip.text().startswith('Ready to run') and w.noticeLabel.isHidden()
    w.saveScanBtn.click()
    assert rig.fileDialogs                          # saving asks for a file again

    _enter(field, 'x')                              # Expert drops the refused value ...
    w.setView(VIEW_EXPERT)
    assert rig.scan._templateProblem == '' and w.statusChip.text().startswith('Ready to run')
    assert 'whole number' not in w.noticeLabel.text()
    form = _simple(rig)                             # ... and Simple shows the scan's values
    assert _editor(form, 'cycles').text() == '4' and form.problem.isHidden()


def test_a_commit_made_by_the_switch_itself_leaves_no_problem_in_expert(shown):
    rig, w = shown, shown.widget
    edits = []
    w.sigTemplateEdited.connect(edits.append)
    form = _simple(rig)
    _type(_editor(form, 'cycles'), 'x')              # typed, not left: the switch hides it
    w.setView(VIEW_EXPERT)
    assert edits == ['ms-resolft']                   # it committed as it was hidden ...
    assert rig.scan._templateProblem == '' and rig.scan.program() == _made(rig)
    assert w.statusChip.text().startswith('Ready to run')     # ... and Expert has no problem
    form = _simple(rig)
    _type(_editor(form, 'cycles'), '5')              # a value that reads commits and is kept
    w.setView(VIEW_EXPERT)
    assert rig.scan.program() == _made(rig, cycles=5)
    assert w.subtitleLabel.text() == '2 timepoints › 5 cycles › 3 planes'


# ---------------------------------------------------------------------------
# 5. The pulse table
# ---------------------------------------------------------------------------

def test_the_pulse_table_edits_cells_only_and_makes_the_scan_again(rig):
    w = rig.widget
    form = _simple(rig)
    table = _editor(form, 'pulses')
    for button in (table.addButton, table.removeButton, table.upButton, table.downButton):
        assert button.isHidden()
    lasers = rig.lasers
    table.table.item(0, 1).setText('2')                      # an on time, in ms
    assert rig.scan.program() == _made(rig, pulses=[
        [lasers[0], 2e-3, 1e-3], [lasers[1], 2e-3, 1e-3], [lasers[1], 1e-3, 0.0]])
    picked = table.table.cellWidget(0, 0)                   # a laser, picked in the table
    picked.setCurrentText(lasers[2])
    assert rig.scan.program() == _made(rig, pulses=[
        [lasers[2], 2e-3, 1e-3], [lasers[1], 2e-3, 1e-3], [lasers[1], 1e-3, 0.0]])
    w.flushRefresh()
    assert table.table.cellWidget(0, 0) is picked           # not reset under its own signal
    assert w.parameters.form() is form
    assert table.rows(raw=True)[0] == {'device': lasers[2], 'on': '2', 'gap': '1'}


def test_a_pulse_table_inline_is_as_tall_as_its_rows(rig):
    form = _simple(rig)
    table = _editor(form, 'pulses')
    few = table.table.sizeHint().height()
    table.addRow({'device': rig.lasers[0], 'on': 1e-3, 'gap': 0.0})
    assert table.table.sizeHint().height() > few


# ---------------------------------------------------------------------------
# 6. A trajectory's points and code
# ---------------------------------------------------------------------------

def _points(rig, **changes):
    params = dict(x_device='X', y_device='Y', points_um=[[0.0, 0.0], [1.2345678, 2.0], [3.0, 1.0]],
                  duration_s=0.01, window_s=2e-5, approach_s=5e-3, pixel_um=0.25,
                  laser=rig.lasers[1])
    params.update(changes)
    return presets.make('trajectory-points', params)


def test_a_path_of_points_is_a_summary_with_edit_and_comes_back_exactly(shown):
    rig, w = shown, shown.widget
    rig.scan.setProgram(_points(rig))
    form = _simple(rig)
    editor = _editor(form, 'points_um')
    assert isinstance(editor, _SummaryEditor) and editor.summary.text() == '3 points'
    assert w.parameters.showsTemplate() and w.trajectoryCard.isHidden()
    editor.editButton.click()                               # the template's full form
    assert rig.asked == [('trajectory-points', rig.scan.program().requested_as['params'])]
    assert rig.asked[0][1]['points_um'][1] == [1.2345678, 2.0]
    # the other fields are inline; a commit keeps the points as they were
    _enter(_editor(form, 'pixel_um'), '0.5')
    assert rig.scan.program() == _points(rig, pixel_um=0.5)
    assert rig.scan.program().trajectory.params['points_um'][1] == [1.2345678, 2.0]
    assert w.parameters.form() is form and editor.summary.text() == '3 points'
    assert _editor(form, 'duration_s').text() == '10'       # seconds in ms
    assert w.titleLabel.text() == 'Trajectory: Points (a curve from a file)'
    w.setView(VIEW_EXPERT)                                  # as today, the Path card
    assert not w.trajectoryCard.isHidden() and w.parameters.isHidden()


def test_a_box_ticked_and_an_optional_choice_picked_commit_and_invent_nothing(shown):
    rig, w = shown, shown.widget
    rig.scan.setProgram(_points(rig))
    form = _simple(rig)
    box = _editor(form, 'record_measurements')
    assert isinstance(box, QtWidgets.QCheckBox) and not box.isChecked()
    assert 'record_measurements' not in form.params()       # the scan never set it: not invented
    box.setChecked(True)                                    # a box ticked
    assert rig.scan.program() == _points(rig, record_measurements=True)
    assert rig.scan.program().trajectory.record_measurements is True
    _editor(form, 'laser').setCurrentText('')               # an optional laser, none
    assert rig.scan.program() == _points(rig, record_measurements=True, laser='')
    assert rig.scan.program().timeline == ()
    assert w.parameters.form() is form and box.isChecked()


def test_a_script_is_a_summary_whose_code_is_never_read_back(shown, tmp_path):
    rig = shown
    marker = tmp_path / 'ran'
    code = ('open(%r, "a").write("ran\\n")\n'
            'def path(t, duration):\n'
            '    return np.sin(2 * np.pi * t / duration), np.cos(2 * np.pi * t / duration)\n'
            % str(marker))
    program = presets.make('trajectory-script', dict(
        x_device='X', y_device='Y', code=code, duration_s=0.02, window_s=1e-4,
        approach_s=5e-3, pixel_um=0.5, laser=rig.lasers[1]))
    rig.scan.setProgram(program)                             # held: not approved here
    assert not marker.exists()
    form = _simple(rig)
    editor = _editor(form, 'code')
    assert isinstance(editor, _SummaryEditor) and editor.summary.text() == 'a Python path, 3 lines'
    _enter(_editor(form, 'pixel_um'), '0.25')
    assert rig.scan.program().trajectory.params['code'] == code
    assert not marker.exists() and rig.asked == []           # nothing ran, nothing asked
    editor.editButton.click()
    assert [asked[0] for asked in rig.asked] == ['trajectory-script']
    assert rig.asked[0][1]['code'] == code


# ---------------------------------------------------------------------------
# 7. The template's form only for the template's output
# ---------------------------------------------------------------------------

def test_an_edited_scan_is_not_its_templates_and_an_edit_undone_is_again(rig, qtbot):
    w = rig.widget
    original = rig.scan.program()
    _chips(rig)[1].count.setValue(3)                         # an Expert edit
    _settle(rig, qtbot)
    assert rig.scan.program().requested_as is None
    assert w.titleLabel.text() == f'{TEMPLATE}, edited' and w.template() is None
    assert not w.templateValuesButton.isHidden()
    assert _simple(rig) is None                              # P1: nothing is listed for it
    assert w.parameters.isHidden() and not w.step.isHidden()
    w.setView(VIEW_EXPERT)
    _chips(rig)[1].count.setValue(2)                         # the edit undone
    _settle(rig, qtbot)
    assert rig.scan.program() == original                    # requested_as and all
    assert w.titleLabel.text() == TEMPLATE and w.template() is not None
    form = _simple(rig)
    assert _editor(form, 'cycles').text() == '2' and w.templateValuesButton.isHidden()


def test_a_file_whose_request_does_not_make_its_scan_is_edited_and_offers_its_values(rig):
    w = rig.widget
    state = {'program': program_to_dict(_ms_resolft(rig))}
    state['program']['requested_as']['params']['cycles'] = 5      # it makes another scan
    rig.scan._templateValues['ms-resolft'] = dict(rig.scan._templateValues.get(
        'ms-resolft') or {}, cycles=9)                              # another scan's values
    assert rig.scan.applyComponentState(state, applyMode=SETUP_MODE) == []
    assert w.titleLabel.text() == f'{TEMPLATE}, edited' and w.template() is None
    assert _simple(rig) is None
    w.templateValuesButton.click()                    # the values it was made from, not the last
    assert [(preset, initial['cycles']) for preset, initial in rig.asked] == [
        ('ms-resolft', 5)]


def test_the_values_an_edited_scan_was_made_from_are_offered_not_the_last_used(rig, qtbot):
    w = rig.widget
    opened = presets.make('ms-resolft', dict(
        copy.deepcopy(_ms_resolft(rig).requested_as['params']), cycles=5))
    rig.scan.setProgram(opened)
    rig.scan._templateValues['ms-resolft'] = dict(
        copy.deepcopy(opened.requested_as['params']), cycles=9)    # what was last used
    _chips(rig)[2].count.setValue(4)                         # edited since: no request left
    _settle(rig, qtbot)
    assert rig.scan.program().requested_as is None and w.titleLabel.text().endswith('edited')
    w.templateValuesButton.click()
    assert [(preset, initial['cycles']) for preset, initial in rig.asked] == [
        ('ms-resolft', 5)]


def _all_templates(rig):
    lasers = rig.lasers
    path = dict(x_device='X', y_device='Y', duration_s=0.01, window_s=2e-5, approach_s=5e-3,
                pixel_um=0.25, laser=lasers[1])
    params = {
        'ms-resolft': None,
        'pulse-sequence': dict(pulses=[[lasers[0], 1e-3, 2e-3]], camera='', exposure_pulse=1,
                               frames=5, pause_s=0.0),
        'raster': dict(x_device='X', y_device='Y', x_pixels=3, y_pixels=2, x_step_um=0.5,
                       y_step_um=0.5, x_start_um=0.0, y_start_um=0.0,
                       pulses=[[lasers[0], 1e-3, 0.0]], camera='', exposure_pulse=1,
                       move_s=5e-4, settle_s=5e-4, return_s=1e-3, traversal='serpentine',
                       transition='jump'),
        'trajectory-circle': dict(path, center_um=[0.0, 0.0], radius_um=5.0, turns=3.0),
        'trajectory-spiral': dict(path, center_um=[0.0, 0.0], radius_um=5.0, turns=3.0,
                                  speed='linear'),
        'trajectory-lissajous': dict(path, center_um=[0.0, 0.0], amplitude_um=[5.0, 5.0],
                                     frequency_hz=[1234.5678, 987.654321], phase_rad=0.5),
        'trajectory-points': None,
        'trajectory-script': dict(path, code='def path(t, duration):\n    return 0 * t, 0 * t\n'),
    }
    params['trajectory-points'] = _points(rig).requested_as['params']
    return {preset_id: (_ms_resolft(rig) if value is None else presets.make(preset_id, value))
            for preset_id, value in params.items()}


def test_every_built_in_template_survives_the_json_round_trip_as_its_template(rig):
    w = rig.widget
    programs = _all_templates(rig)
    assert set(programs) == set(presets.PRESETS)             # a new template needs a row here
    for preset_id, program in programs.items():
        rig.scan.setProgram(program)
        assert w.template()[0].id == preset_id, preset_id
        state = json.loads(json.dumps(rig.scan.getComponentState()))
        rig.scan.setProgram(None)
        assert w.template() is None
        assert rig.scan.applyComponentState(state, applyMode=SETUP_MODE) == [], preset_id
        assert rig.scan.program() == program, preset_id
        assert w.template()[0].id == preset_id, preset_id
        assert w.titleLabel.text() == presets.PRESETS[preset_id].title, preset_id
        assert _simple(rig) is not None and w.parameters.showsTemplate(), preset_id
        w.setView(VIEW_EXPERT)


def test_a_raster_saved_before_move_shape_existed_is_its_template_with_what_it_meant(rig):
    w = rig.widget
    raster = _all_templates(rig)['raster']
    old = presets.make('raster', dict(raster.requested_as['params'], transition='smooth'))
    assert old.drives[0].motion.transition == 'smooth'       # what it meant before
    state = json.loads(json.dumps({'program': program_to_dict(old)}))
    del state['program']['requested_as']['params']['transition']     # as an old file has it
    assert rig.scan.applyComponentState(state, applyMode=SETUP_MODE) == []
    assert w.template()[0].id == 'raster' and w.titleLabel.text() == 'Stepped raster'
    assert w.template()[1]['transition'] == 'smooth'
    form = _simple(rig)
    assert _editor(form, 'transition').currentText() == 'smooth'
    _leave(_editor(form, 'x_pixels'), '4')
    assert rig.scan.program().loop('x').count == 4
    assert rig.scan.program().drives[0].motion.transition == 'smooth'


# ---------------------------------------------------------------------------
# 8. Exactness
# ---------------------------------------------------------------------------

def test_exact_text_reads_back_through_stored_exactly():
    rng = random.Random(7)
    values = [0.1 + 0.2, 1 / 3, 1 / 30, 1 / 3000, 1e-5, 1e22, 5e-324, 1.7976931348623157e308,
              12345.6789, 0.0005, 0.25, 1.0, 1234.5678, 987.654321, 1.2345678, 1e-10 + 1e-12]
    values += [struct.unpack('<d', rng.randbytes(8))[0] for _ in range(300)]
    values += [round(rng.uniform(-1e3, 1e3), rng.randrange(1, 12)) for _ in range(300)]
    values += [rng.randrange(1, 10 ** 6) * 1e-6 for _ in range(300)]       # µs on a 10 µs grid
    for value in values:
        if value != value or value in (float('inf'), float('-inf')):
            continue
        for scale in (1.0, MS):
            text = exact_text(value, scale)
            assert stored(text, scale) == value, (value, scale, text)
            assert 'e' not in text.lower() and not text.endswith('.'), (value, text)
            assert '.' not in text or not text.endswith('0'), (value, text)    # no trailing zeros
        assert float(exact_text(value)) == value


def test_exact_text_is_plain_decimal_text():
    assert exact_text(0.0) == '0' and exact_text(-0.0) == '0' and exact_text(-0.0, MS) == '0'
    assert exact_text(2e-3, MS) == '2' and exact_text(1 / 30, MS) == '33.33333333333333'
    assert exact_text(1e22) == '10000000000000000000000'
    assert exact_text(1e-5, MS) == '0.01' and exact_text(-0.5, MS) == '-500'
    assert exact_text(3) == '3' and exact_text('2.5') == '2.5'
    assert exact_text(float('inf')) == 'inf'                 # shown, not read as a number


def test_untouched_fields_give_back_what_they_were_given_when_another_is_committed(shown):
    rig = shown
    lasers = rig.lasers
    pulses = [[lasers[0], 1 / 3000, 1e-3], [lasers[1], 2e-3, 1e-3], [lasers[1], 1e-3, 0.0]]
    rig.scan.setProgram(_ms_resolft(rig, pause_s=1 / 30, pulses=pulses))
    given = copy.deepcopy(rig.scan.program().requested_as['params'])
    form = _simple(rig)
    assert _editor(form, 'pause_s').text() == '33.33333333333333'    # exactly, in ms
    _enter(_editor(form, 'planes'), '4')
    assert rig.scan.program() == presets.make('ms-resolft', dict(given, planes=4))
    assert rig.scan.program().waits[0].interval_s == 1 / 30
    assert rig.scan.program().timeline[0].end_s == 1 / 3000
    table = _editor(form, 'pulses')
    table.table.item(1, 2).setText('3')                      # another cell of the table
    assert rig.scan.program() == presets.make('ms-resolft', dict(
        given, planes=4, pulses=[pulses[0], [lasers[1], 2e-3, 3e-3], pulses[2]]))
    assert rig.scan.program().requested_as['params']['pulses'][0] == pulses[0]


def test_a_path_keeps_its_frequencies_and_points_when_another_field_is_committed(shown):
    rig = shown
    path = dict(x_device='X', y_device='Y', duration_s=0.01, window_s=2e-5, approach_s=5e-3,
                pixel_um=0.25, laser=rig.lasers[1])
    lissajous = dict(path, center_um=[0.0, 0.0], amplitude_um=[5.0, 5.0],
                     frequency_hz=[1234.5678, 987.654321], phase_rad=0.5)
    rig.scan.setProgram(presets.make('trajectory-lissajous', lissajous))
    form = _simple(rig)
    assert _editor(form, 'frequency_hz').text() == '1234.5678, 987.654321'
    _enter(_editor(form, 'pixel_um'), '0.5')
    assert rig.scan.program() == presets.make('trajectory-lissajous', dict(
        lissajous, pixel_um=0.5))
    assert rig.scan.program().trajectory.params['frequency_hz'] == [1234.5678, 987.654321]
    rig.scan.setProgram(_points(rig))
    form = _simple(rig)
    _enter(_editor(form, 'pixel_um'), '0.5')
    assert rig.scan.program() == _points(rig, pixel_um=0.5)
    assert rig.scan.program().trajectory.params['points_um'][1][0] == 1.2345678


def test_the_template_dialog_shows_exactly_and_names_a_value_that_does_not_read(rig):
    w = rig.widget
    params = copy.deepcopy(_ms_resolft(rig, pause_s=1 / 30).requested_as['params'])
    dialog = PresetDialog(presets.PRESETS['ms-resolft'], w.devices, params, lambda p: '')
    field = {name: editor for name, (_f, editor) in dialog._editors.items()}
    assert field['pause_s'].text() == '33.33333333333333' and field['start_um'].text() == '1'
    assert dialog.params()['pause_s'] == 1 / 30           # the old 12-digit display rounded it
    pulses = field['pulses']
    assert not pulses.addButton.isHidden()                  # the dialog's table keeps its buttons
    assert pulses.rows(raw=True)[0]['on'] == '1'
    field['cycles'].setText('2.5')
    with pytest.raises(ValueError, match="Cycles: '2.5' is not a whole number"):
        dialog.params()
    field['cycles'].setText('2')
    field['pause_s'].setText('soon')
    with pytest.raises(ValueError, match="Pause before each timepoint: 'soon' is not a number"):
        dialog.params()
    field['pause_s'].setText('0')
    field['start_um'].setText('left')
    with pytest.raises(ValueError, match="First position: 'left' is not a number"):
        dialog.params()
    field['start_um'].setText('1')
    pulses.table.item(0, 1).setText('long')
    with pytest.raises(ValueError, match="Pulse sequence 1, On \\(ms\\): 'long' is not a number"):
        dialog.params()
    dialog._accept()                                      # the reason is said in the dialog
    assert "Pulse sequence 1, On (ms): 'long' is not a number" in dialog.error.text()
    pair = PresetDialog(presets.PRESETS['trajectory-circle'], w.devices, {
        'center_um': [0.5, 1 / 3]}, lambda p: '')
    center = pair._editors['center_um'][1]
    assert center.text() == '0.5, 0.3333333333333333'
    center.setText('1, west')
    with pytest.raises(ValueError, match="Centre \\(µm, µm\\): '1, west' is not two numbers"):
        pair.params()


# ---------------------------------------------------------------------------
# 9. Pending edits and scans from outside
# ---------------------------------------------------------------------------

def test_an_edit_waiting_for_its_report_does_not_make_the_next_scan_edited(rig, qtbot):
    w = rig.widget
    # the tables show 12 digits: read back, a pause of 1/30 s would not make this scan
    exact = dict(pause_s=1 / 30)
    _chips(rig)[1].count.setValue(3)                         # younger than the settle time
    assert rig.scan._edited and rig.scan._reportTimer.isActive()
    rig.scan.setProgram(_ms_resolft(rig, planes=4, **exact))
    assert rig.scan._edited is False
    _settle(rig, qtbot)
    assert rig.scan.program() == _ms_resolft(rig, planes=4, **exact)
    assert rig.scan.program().requested_as is not None and w.titleLabel.text() == TEMPLATE
    # ... nor a template commit
    form = _simple(rig)
    w.setTableValue('loops', 1, 'count', '3')                # a table edit, not yet reported
    assert rig.scan._edited
    _leave(_editor(form, 'planes'), '5')
    assert rig.scan._edited is False
    _settle(rig, qtbot)
    assert rig.scan.program() == _ms_resolft(rig, planes=5, **exact)
    assert w.titleLabel.text() == TEMPLATE


def test_entering_simple_reads_an_edit_waiting_first(rig, qtbot):
    w = rig.widget
    _chips(rig)[1].count.setValue(3)
    _chips(rig)[1].count.setValue(2)                          # undone before the report
    assert rig.scan._edited
    form = _simple(rig)                                       # read now: the template is current
    assert form is not None and w.titleLabel.text() == TEMPLATE
    assert rig.scan.program() == _ms_resolft(rig) and rig.scan._edited is False
    w.setView(VIEW_EXPERT)
    _chips(rig)[1].count.setValue(3)                          # edited and left so
    assert _simple(rig) is None and w.template() is None


@pytest.mark.parametrize('how', ['dialog', 'file'])
def test_a_same_template_scan_from_outside_replaces_the_form_and_its_refused_value(shown, how):
    rig, w = shown, shown.widget
    form = _simple(rig)
    _enter(_editor(form, 'cycles'), 'x')                     # refused, and still in the field
    assert rig.scan._templateProblem and _editor(form, 'cycles').hasFocus()
    params = copy.deepcopy(_ms_resolft(rig).requested_as['params'])
    params['planes'] = 4
    if how == 'dialog':
        w.askPresetParams = lambda preset, initial, validate, **_rig: params
        rig.scan.makeFromPreset('ms-resolft')
    else:
        state = {'program': program_to_dict(presets.make('ms-resolft', params))}
        assert rig.scan.applyComponentState(state, applyMode=SETUP_MODE) == []
    new = w.parameters.form()
    assert new is not form and rig.scan._templateProblem == ''
    assert _editor(new, 'cycles').text() == '2' and _editor(new, 'planes').text() == '4'
    assert new.problem.isHidden() and w.statusChip.text().startswith('Ready to run')
    assert rig.scan.program() == presets.make('ms-resolft', params)


# ---------------------------------------------------------------------------
# 10. The drawing in Simple
# ---------------------------------------------------------------------------

def test_the_drawing_in_simple_is_only_drawn(shown):
    rig, w = shown, shown.widget
    rows = w.timeline.rows(raw=True)
    w.setView(VIEW_SIMPLE)
    rig.scan.updateReport()                                   # a report: the parts follow it
    card, canvas = w.step, w.step.canvas
    assert not card.isHidden() and card.canvas._editable is False
    first = _event(rig, 0)
    start = _centre(canvas._barRect(first))
    _drag(canvas, start, start + QtCore.QPoint(37, 0))        # a drag on a bar ...
    canvas.setFocus()
    for key in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Left, QtCore.Qt.Key_Down, QtCore.Qt.Key_Delete,
                QtCore.Qt.Key_Backspace):
        QtTest.QTest.keyClick(canvas, key)                    # ... keys ...
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_Right, QtCore.Qt.ShiftModifier)
    assert w.timeline.rows(raw=True) == rows                  # ... change nothing
    assert canvas.selected() == -1
    assert card.hint.isHidden() and card.inspector.isHidden()
    assert all(button.isHidden() for button in card.addButtons.values())
    assert card.dwellGroup.isHidden() and not card.dwellText.isHidden()
    assert card.dwellText.text() == 'dwell 7 ms'
    _send_mouse(canvas, QtCore.QEvent.MouseMove, start, QtCore.Qt.NoButton)
    assert canvas.cursor().shape() == QtCore.Qt.ArrowCursor  # no hand over a bar
    assert canvas.events()                                    # it is drawn
    # a note that offers a button in Expert is text here
    w.setTableValue('timeline', 3, 'end_s', '9')
    rig.scan.updateReport()
    labels = card.noteHost.findChildren(QtWidgets.QLabel)
    assert any('end at 9 ms' in label.text() for label in labels)
    assert not card.noteHost.findChildren(QtWidgets.QToolButton)
    assert canvas.selected() == -1 and card.inspector.isHidden()
    w.setView(VIEW_EXPERT)                                    # everything is back, as it was
    assert canvas._editable and not card.inspector.isHidden() and card.canvas.selected() == 0
    assert not card.hint.isHidden() and not card.dwellGroup.isHidden()
    assert card.dwellText.isHidden() and all(not b.isHidden() for b in card.addButtons.values())
    assert card.noteHost.findChildren(QtWidgets.QToolButton)  # the note offers its button again
    hover = _centre(canvas._barRect(_event(rig, 1)))
    _send_mouse(canvas, QtCore.QEvent.MouseMove, hover, QtCore.Qt.NoButton)
    assert canvas.cursor().shape() == QtCore.Qt.OpenHandCursor


# ---------------------------------------------------------------------------
# 11. The view in the state
# ---------------------------------------------------------------------------

def test_the_view_is_saved_and_restored_at_startup_only(rig):
    w = rig.widget
    assert rig.scan.getComponentState()['view'] == VIEW_EXPERT
    w.setView(VIEW_SIMPLE)
    simple = rig.scan.getComponentState()
    assert simple['view'] == VIEW_SIMPLE
    w.setView(VIEW_EXPERT)
    assert rig.scan.applyComponentState(simple, applyMode=STARTUP) == []
    assert w.view() == VIEW_SIMPLE                            # restored at startup
    w.setView(VIEW_EXPERT)
    assert rig.scan.applyComponentState(simple, applyMode=None) == []
    assert w.view() == VIEW_SIMPLE                            # the persistence layer's default
    w.setView(VIEW_EXPERT)
    assert rig.scan.applyComponentState(simple, applyMode=SETUP_MODE) == []
    assert w.view() == VIEW_EXPERT                            # a scan file does not change it
    w.setView(VIEW_SIMPLE)
    expert = dict(simple, view=VIEW_EXPERT)
    assert rig.scan.applyComponentState(expert, applyMode=SETUP_MODE) == []
    assert w.view() == VIEW_SIMPLE
    assert rig.scan.applyComponentState(dict(simple, view=None), applyMode=STARTUP) == []
    assert w.view() == VIEW_SIMPLE
    state = {k: v for k, v in expert.items() if k != 'view'}  # a state without a view
    assert rig.scan.applyComponentState(state, applyMode=STARTUP) == []
    assert w.view() == VIEW_SIMPLE                            # keeps the current one


def test_an_unknown_view_is_a_warning_and_the_scan_still_applies(rig):
    w = rig.widget
    program = _ms_resolft(rig, planes=4)
    base = {'program': program_to_dict(program)}
    for odd in ('wide', 5, ['simple'], True):
        warnings = rig.scan.applyComponentState(dict(base, view=odd), applyMode=STARTUP)
        assert len(warnings) == 1 and isinstance(warnings[0], RestoreWarning)
        assert warnings[0] == f'Unknown panel view {odd!r}; kept the expert view.'
        assert not isCriticalRestoreWarning(warnings[0])
        assert rig.scan.program() == program and w.view() == VIEW_EXPERT
    # the warning survives the refusals that come after it
    warnings = rig.scan.applyComponentState({'view': 'wide'}, applyMode=STARTUP)
    assert len(warnings) == 2 and warnings[0].startswith('Unknown panel view')
    assert warnings[1] == 'Saved scan state holds no scan program; it was not applied.'
    warnings = rig.scan.applyComponentState(
        {'view': 'wide', 'program': program_to_dict(_ms_resolft(rig, device='RO'))},
        applyMode=STARTUP)
    assert len(warnings) == 2 and warnings[1] == (
        'This setup has no RO. Scan state was not applied.')
    warnings = rig.scan.applyComponentState({'view': 'wide', 'program': {'format': 'no'}},
                                            applyMode=STARTUP)
    assert len(warnings) == 2 and warnings[1].endswith('Scan state was not applied.')
    warnings = rig.scan.applyComponentState(
        {'view': 'wide', 'requiredFeatures': {'other': 'another feature'}}, applyMode=STARTUP)
    assert len(warnings) == 2 and warnings[0].startswith('Unknown panel view')
    # a scan that does not apply still opens in the view it was left in
    assert rig.scan.applyComponentState({'view': VIEW_SIMPLE, 'program': {'format': 'no'}},
                                        applyMode=STARTUP)
    assert w.view() == VIEW_SIMPLE


def test_a_state_with_a_view_still_opens_in_the_advanced_panel_when_unchanged(qtbot):
    setup, state, _signals = _advanced_state()
    setup['scan']['scanWidgetType'] = 'Expert'
    expert = ExpertRig(setup)
    try:
        assert expert.scan.applyComponentState(state, applyMode=SETUP_MODE) == []
        expert.widget.setView(VIEW_SIMPLE)
        saved = expert.scan.getComponentState()
        assert saved['view'] == VIEW_SIMPLE and 'requiredFeatures' not in saved
    finally:
        expert.close()
    advanced_setup = _setup_dict(
        scanWidgetType='Advanced', scanDesigner='BetaScanDesigner',
        TTLCycleDesigner='AdvancedScanTTLCycleDesigner',
        scanDesignerParams={'return_time': 1e-3, 'move_time': 1e-4, 'settle_time': 1e-4})
    advanced = ExpertRig(advanced_setup, ScanWidgetAdvanced, ScanControllerAdvanced)
    try:
        without = {k: v for k, v in saved.items() if k != 'view'}
        before = advanced.scan.applyComponentState(without, applyMode=SETUP_MODE)
        scan = advanced.scan.getComponentState()['analogParameterDict']
        after = advanced.scan.applyComponentState(saved, applyMode=SETUP_MODE)
        assert after == before                                  # the view is not a reason to refuse
        assert not any('requires' in warning or 'view' in warning for warning in after)
        assert advanced.scan.getComponentState()['analogParameterDict'] == scan
        assert scan['axis_length'] == state['analogParameterDict']['axis_length']
    finally:
        advanced.close()


# ---------------------------------------------------------------------------
# 12. Run, Show preview and Save with a value typed but not left
# ---------------------------------------------------------------------------

def _press(button, how):
    if how == 'mouse':
        QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
    elif how == 'click':
        button.click()
    else:                                  # the space bar on the button the user has tabbed to
        button.setFocus()
        QtTest.QTest.keyClick(button, QtCore.Qt.Key_Space)


def _wait_for_end(rig, qtbot):
    qtbot.waitUntil(lambda: bool(rig.ended), timeout=30_000)


@pytest.mark.parametrize('policy', ['strong', 'tab'])
@pytest.mark.parametrize('how', ['mouse', 'click', 'space'])
@pytest.mark.parametrize('which', ['run', 'plot', 'save'])
def test_a_value_typed_but_not_left_is_committed_before_the_button_acts(
        shown, qtbot, tmp_path, which, how, policy):
    """Where a push button takes no focus when clicked (macOS: ``TabFocus``)
    the field being typed in never sees ``editingFinished``, and Run would
    run the scan without it. The pressed button takes the focus, which ends
    the field's editing as leaving it does; clearing the field's focus
    instead would release the button, and the click would never come."""
    rig, w = shown, shown.widget
    button = {'run': w.scanButton, 'plot': w.plotButton, 'save': w.saveScanBtn}[which]
    if policy == 'tab':
        button.setFocusPolicy(QtCore.Qt.TabFocus)
    form = _simple(rig)
    seen, clicks = [], []
    button.clicked.connect(lambda: clicks.append(True))
    build = rig.scan._buildScanSignals

    def spy():
        seen.append(rig.scan.program().loop('cycle').count)
        return build()

    rig.scan._buildScanSignals = spy
    if which == 'save':
        rig.fileAnswer = str(tmp_path / 'scan.json')
    _type(_editor(form, 'cycles'), '3')
    assert rig.scan.program().loop('cycle').count == 2        # typed, not yet committed
    rig.ended.clear()
    _press(button, how)
    assert clicks == [True], 'the click arrived'
    assert rig.scan.program() == _made(rig, cycles=3)         # committed
    if which == 'run':
        _wait_for_end(rig, qtbot)
        assert seen == [3] and rig.rejected == []             # the run used the typed value
    elif which == 'plot':
        assert not w.plot.isHidden()                          # drawn from the typed value
        steps = rig.scan._scheduleOf(rig.scan._compile()).step_spans()
        assert len(steps) == 2 * 3 * 3
    else:
        assert len(rig.fileDialogs) == 1
        saved = json.loads((tmp_path / 'scan.json').read_text())
        assert saved['program']['loops'][1]['count'] == 3
        assert saved['program']['requested_as']['params']['cycles'] == 3


def test_a_refused_typed_value_refuses_the_run_with_its_reason(shown):
    rig, w = shown, shown.widget
    form = _simple(rig)
    _type(_editor(form, 'cycles'), 'x')
    rig.rejected.clear()
    w.scanButton.click()
    assert any("Cycles: 'x' is not a whole number" in str(r) for r in rig.rejected)
    assert not rig.scan.isRunning and rig.ended == []


def test_from_simple_run_ends_stop_stops_and_the_preview_draws(rig, qtbot):
    w = rig.widget
    _simple(rig)
    w.plotButton.click()
    assert not w.plot.isHidden() and w.preview.problem.isHidden()
    assert rig.run()                                           # Run ends
    assert rig.rejected == [] and w.scanButton.isEnabled() and not w.stopButton.isEnabled()
    rig.scan.setProgram(_ms_resolft(rig, timepoints=60))      # long enough to stop
    rig.ended.clear()
    w.scanButton.click()
    qtbot.waitUntil(lambda: rig.scan.isRunning, timeout=10_000)
    assert w.stopButton.isEnabled() and not w.scanButton.isEnabled()
    w.stopButton.click()                                       # Stop stops
    qtbot.waitUntil(lambda: bool(rig.ended), timeout=30_000)
    assert rig.scan._scanStopRequested and not w.stopButton.isEnabled()


def test_switching_views_during_a_run_changes_nothing_about_the_run(rig, qtbot):
    w = rig.widget
    program = _ms_resolft(rig, timepoints=60)
    rig.scan.setProgram(program)
    _simple(rig)
    rig.ended.clear()
    rig.scan.runScanAdvanced()
    qtbot.waitUntil(lambda: rig.scan.isRunning, timeout=10_000)
    for view in (VIEW_EXPERT, VIEW_SIMPLE, VIEW_EXPERT, VIEW_SIMPLE):
        w.setView(view)
        assert rig.scan.isRunning and w.statusChip.text() == 'Running'
        assert w.stopButton.isEnabled() and not w.scanButton.isEnabled()
        assert rig.scan.program() == program and rig.rejected == []
    assert rig.ended == []
    w.stopButton.click()
    qtbot.waitUntil(lambda: bool(rig.ended), timeout=30_000)
    qtbot.waitUntil(lambda: w.statusChip.text().startswith('Ready to run'), timeout=5000)


# ---------------------------------------------------------------------------
# 13. The wheel
# ---------------------------------------------------------------------------

def _wheel(widget):
    wheel = QtGui.QWheelEvent(QtCore.QPointF(2, 2), QtCore.QPointF(widget.mapToGlobal(
        QtCore.QPoint(2, 2))), QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
        QtCore.Qt.NoButton, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(widget, wheel)


def test_the_wheel_over_a_field_of_the_form_scrolls_the_panel_not_the_field(shown, qtbot):
    rig, w = shown, shown.widget
    w.resize(900, 300)                                         # more than fits: it scrolls
    form = _simple(rig)
    qtbot.wait(50)
    bar = w.scrollArea.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == 0
    combo = _editor(form, 'transition')
    before = rig.scan.program()
    assert not combo.hasFocus()
    _wheel(combo)
    assert combo.currentText() == 'jump' and rig.scan.program() == before
    assert bar.value() > 0                                     # the panel scrolled instead
    cell = _editor(form, 'pulses').table.cellWidget(0, 0)      # a laser box in the table
    bar.setValue(0)
    _wheel(cell)
    assert cell.currentText() == rig.lasers[0] and rig.scan.program() == before
    assert bar.value() > 0


# ---------------------------------------------------------------------------
# 14. Can it run?, and the quiet table
# ---------------------------------------------------------------------------

def test_can_it_run_shows_only_when_there_is_something_to_know(rig, qtbot):
    w = rig.widget
    _simple(rig)
    assert w.statusChip.text().startswith('Ready to run') and w.check.isHidden()
    # a scan that cannot run for a reason other than the form's
    rig.scan.setProgram(_ms_resolft(rig, start_um=80.0))      # past the Z scanner's volts
    assert w.statusChip.text().startswith('Cannot run') and not w.check.isHidden()
    assert w.check.problems.text()
    rig.scan.setProgram(_ms_resolft(rig))
    assert w.check.isHidden()
    # a scan that plays something differently (the header's amber chip)
    w.setCheck({**w._check, 'facts': {**w._check['facts'], 'changes': 2}})
    assert not w.check.isHidden()
    w.setCheck({**w._check, 'facts': {**w._check['facts'], 'changes': 0}})
    assert w.check.isHidden()
    # asked for: a click on the status chip shows it until the view changes
    w.statusChip.clicked.emit()
    assert not w.check.isHidden()
    rig.scan.updateReport()
    assert not w.check.isHidden()
    w.setView(VIEW_EXPERT)
    assert not w.check.isHidden()                              # Expert always has it
    w.setView(VIEW_SIMPLE)
    assert w.check.isHidden()
    # the form's own refused value is under the form and in the header, not here
    form = w.parameters.form()
    _leave(_editor(form, 'cycles'), 'x')
    assert w.statusChip.text().startswith('Cannot run') and w.check.isHidden()
    assert not form.problem.isHidden()


def test_an_empty_scan_has_no_check_card_in_simple(rig):
    w = rig.widget
    rig.scan.setProgram(None)
    w.setView(VIEW_SIMPLE)
    assert w.check.isHidden() and w.statusChip.text() == 'Empty'


def test_rows_written_together_are_one_quiet_write_and_a_lone_row_is_an_edit():
    columns = [_Column('a', 'A', 'float', exact=True), _Column('b', 'B', 'choice', ('x', 'y'))]
    table = _RowTable(columns)
    edits = []
    table.sigEdited.connect(lambda: edits.append(table.table.rowCount()))
    table.setRows([{'a': 1.0, 'b': 'x'}, {'a': 2.0, 'b': 'y'}, {'a': 0.1 + 0.2, 'b': 'x'}])
    assert edits == [] and table.rows(raw=True)[2]['a'] == '0.30000000000000004'
    table.setColumns(columns)                                  # rewritten with its rows: quiet too
    assert edits == [] and table.table.rowCount() == 3
    table.addRow({'a': 4.0, 'b': 'y'})                         # one row: one edit
    assert edits == [4]
    table.addButton.click()
    assert edits == [4, 5]
    table.moveRow(1, +1)                                       # a move: one edit, as it was
    assert len(edits) == 3
    table.setRows([])
    assert len(edits) == 3
    table.setRowButtonsVisible(False)
    assert all(b.isHidden() for b in (table.addButton, table.removeButton, table.upButton,
                                      table.downButton))
    table.setRowButtonsVisible(True)
    assert not any(b.isHidden() for b in (table.addButton, table.removeButton, table.upButton,
                                          table.downButton))


def test_only_an_exact_column_shows_a_float_exactly():
    exact = _RowTable([_Column('v', 'V', 'float', scale=MS, exact=True),
                       _Column('w', 'W', 'floats', scale=MS, exact=True)])
    plain = _RowTable([_Column('v', 'V', 'float', scale=MS), _Column('w', 'W', 'floats', scale=MS)])
    row = {'v': 1 / 30, 'w': [1 / 30, 2e-3]}
    exact.addRow(row)
    plain.addRow(row)
    assert exact.rows(raw=True) == [{'v': '33.33333333333333', 'w': '33.33333333333333, 2'}]
    assert plain.rows(raw=True) == [{'v': '33.3333333333', 'w': '33.3333333333, 2'}]   # as today
    assert exact.rows()[0]['v'] == 1 / 30


# ---------------------------------------------------------------------------
# The form and the card, on their own
# ---------------------------------------------------------------------------

def test_the_form_writes_nothing_while_committing_and_keeps_the_field_being_typed_in(qtbot):
    values = dict(device='Z', cycles=2, planes=3, cycle_step_um=0.25, start_um=1.0,
                  pulses=[['405', 1e-3, 1e-3], ['488', 2e-3, 1e-3], ['488', 1e-3, 0.0]],
                  camera='Camera', exposure_pulse=3, timepoints=2, pause_s=0.0, move_s=5e-4,
                  settle_s=5e-4, transition='jump')
    devices = {'positioner': ['Z'], 'laser': ['405', '488'], 'detector': ['Camera']}
    form = TemplateForm(presets.PRESETS['ms-resolft'], devices, values)
    host = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(host).addWidget(form)
    qtbot.addWidget(host)
    host.show()
    qtbot.waitExposed(host)
    host.activateWindow()
    qtbot.waitUntil(host.isActiveWindow, timeout=5000)
    cycles, planes = _editor(form, 'cycles'), _editor(form, 'planes')
    assert form.params() == values
    _type(cycles, '7')                                         # typed, not left
    form.setValues(dict(values, cycles=2, planes=9))
    assert cycles.text() == '7' and planes.text() == '9'      # the field in use keeps its text
    assert form.params()['cycles'] == 7 and form.params()['planes'] == 9
    seen = []

    def during():
        form.setValues(dict(values, planes=11))
        seen.append((form.isCommitting(), planes.text()))

    form.sigCommitted.connect(during)
    assert not form.isCommitting()
    QtTest.QTest.keyClick(cycles, QtCore.Qt.Key_Return)       # the commit of what was typed
    assert seen == [(True, '9')]                              # nothing written inside it
    assert not form.isCommitting()
    form.setValues(values)                                    # still in the field: it keeps its text
    assert cycles.text() == '7' and planes.text() == '3'
    host.setFocus()                                           # the field is free again
    form.setValues(values)
    assert cycles.text() == '2' and form.params() == values


def test_a_value_that_does_not_read_is_named_by_the_form(rig):
    form = _simple(rig)
    cycles = _editor(form, 'cycles')
    cycles.setText('2.5')
    with pytest.raises(ValueError, match="Cycles: '2.5' is not a whole number"):
        form.params()
    cycles.setText('2')
    _editor(form, 'planes').setText('')
    assert form.params()['planes'] is None                     # blank is blank: make says so
    with pytest.raises(presets.PresetError, match='fill in Planes per cycle'):
        presets.make('ms-resolft', form.params())
    rig.scan.setProgram(None)
    with pytest.raises(ValueError, match='No template form is shown'):
        rig.widget.templateParams()


def test_the_card_waits_for_the_focus_to_leave_before_it_changes_by_itself(shown, qtbot):
    rig, w = shown, shown.widget
    card = w.parameters
    form = _simple(rig)
    _editor(form, 'planes').setFocus()           # in a field, nothing typed
    w.setTemplate(None)                          # what shows would change under the user's hands
    w.refreshParts()
    assert w.parameters.form() is form and card._waiting and not card.isHidden()   # stays, live
    _editor(form, 'cycles').setFocus()           # moved inside the card: it still waits
    assert w.parameters.form() is form and card._waiting
    w.repeatBox.setFocus()                       # the focus leaves: the change is made
    assert not card._waiting and w.parameters.form() is None and card.isHidden()
    # an edit still waiting for its report leaves the change pending: the report decides
    rig.scan.updateReport()                      # the scan is its template's again
    form = w.parameters.form()
    assert form is not None
    _editor(form, 'planes').setFocus()
    w.setTemplate(None)
    w.refreshParts()
    assert card._waiting
    w._edited()                                  # an edit of the tables, not yet reported
    assert w.editPending()
    w.repeatBox.setFocus()
    assert not card._waiting and w.parameters.form() is form       # not decided at focus-out
    _settle(rig, qtbot)                          # its report: the scan is its template's still
    assert not w.editPending() and w.parameters.form() is form     # no flip of the form
    assert w.template() is not None


def test_a_new_scan_replaces_the_form_whole_and_the_old_one_is_deleted_later(shown, qtbot):
    rig, w = shown, shown.widget
    form = _simple(rig)
    _type(_editor(form, 'cycles'), '9')
    edits = []
    w.sigTemplateEdited.connect(edits.append)
    rig.scan.setProgram(_ms_resolft(rig, planes=4))
    new = w.parameters.form()
    assert new is not form and _editor(new, 'cycles').text() == '2'
    assert edits == []                         # the old form, taken away, wrote nowhere
    assert rig.scan.program() == _ms_resolft(rig, planes=4)
    qtbot.wait(20)
    assert sip.isdeleted(form)


def test_a_view_change_arranges_the_columns_again_for_the_cards_it_shows(shown, qtbot):
    w = shown.widget
    arranged = []
    rearrange = w.columns.rearrange
    w.columns.rearrange = lambda: arranged.append(w.view()) or rearrange()
    w.setView(VIEW_SIMPLE)
    w.setView(VIEW_EXPERT)
    w.setView(VIEW_EXPERT)                      # no change, no re-arranging
    assert arranged == [VIEW_SIMPLE, VIEW_EXPERT]
    w.columns.rearrange = rearrange
    # what a resize does, at the width now
    w.resize(w.columns.threshold() + 300, 900)
    qtbot.waitUntil(lambda: w.columns.twoColumns(), timeout=2000)
    w.columns.box.setDirection(QtWidgets.QBoxLayout.TopToBottom)    # as if the cards had changed
    w.columns.rearrange()
    assert w.columns.twoColumns()
    w.resize(w.columns.threshold() // 2, 900)
    qtbot.waitUntil(lambda: not w.columns.twoColumns(), timeout=2000)


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
