"""The Expert panel's composer (docs/design/plans/scan-engines-and-expert-scan.md
§7.11): the scan described top to bottom, every part editing the tables
underneath -- one state -- beside the engine's check and a lane preview.
"""

import dataclasses

import pytest
from qtpy import QtCore, QtGui, QtTest, QtWidgets

from imswitch.imcontrol.model.scan_program import presets
from imswitch.imcontrol.model.scan_program.program import Motion
from imswitch.imcontrol.model.scan_program.serialize import program_to_dict
from imswitch.imcontrol.view.widgets._expert_scan_parts import collapse, wavelength_color

from .test_scan_controller_expert import ExpertRig, _ms_resolft, _spiral


@pytest.fixture
def rig(qtbot):
    rig = ExpertRig()
    rig.scan.setProgram(_ms_resolft(rig))
    rig.widget.refreshParts()
    yield rig
    rig.close()


@pytest.fixture
def shown(rig, qtbot):
    rig.widget.resize(900, 900)
    rig.widget.show()
    qtbot.waitExposed(rig.widget)
    return rig


def _chips(rig):
    return rig.widget.order._chips


def _send_mouse(canvas, kind, point, buttons):
    QtWidgets.QApplication.sendEvent(canvas, QtGui.QMouseEvent(
        kind, QtCore.QPointF(point), QtCore.Qt.LeftButton, buttons, QtCore.Qt.NoModifier))


def _drag(canvas, start, end):
    """Press, move and release the left button, as a hand does (QTest's
    mouseMove sends no move event on the offscreen platform)."""
    _send_mouse(canvas, QtCore.QEvent.MouseButtonPress, start, QtCore.Qt.LeftButton)
    _send_mouse(canvas, QtCore.QEvent.MouseMove, end, QtCore.Qt.LeftButton)
    _send_mouse(canvas, QtCore.QEvent.MouseButtonRelease, end, QtCore.Qt.NoButton)


def _centre(rect):
    return QtCore.QPoint(int(rect.center().x()), int(rect.center().y()))


def _event(rig, row):
    return next(e for e in rig.widget.step.canvas.events() if e['row'] == row)


# --- the parts show the program ---------------------------------------------------------

def test_the_parts_describe_the_program(rig):
    w = rig.widget
    assert [chip.name.text() for chip in _chips(rig)] == ['timepoints', 'cycles', 'planes']
    assert [chip.count.value() for chip in _chips(rig)] == [2, 2, 3]
    editor = w.positions._editors[0]
    assert editor.device.currentText() == 'Z'
    assert editor.weights['cycle'][1].text() == '0.25' and editor.weights['plane'][1].text() == '0.5'
    assert editor.weights['time'][1].text() == ''          # time does not move Z
    assert editor.range.text() == '6 positions from 1 to 2.25 µm, 0.25 µm apart.'
    assert editor.weights['time'][0].isHidden() and not editor.alsoButton.isHidden()
    events, moving, _notes = w.stepContent()
    assert [e['device'] for e in events] == rig.lasers[:2] + [rig.lasers[1], 'Camera']
    assert list(w.step.canvas._rows) == rig.lasers[:2] + ['Camera']     # a row per device
    step = w.stepSeconds()
    assert moving == [(pytest.approx(step - 1e-3), step, 'Z moves')]   # move + settle, at the end
    assert w.recording._editors[0].detector.currentText() == 'Camera'
    assert w.titleLabel.text() == 'MS-RESOLFT (cycles x planes)'
    assert w.subtitleLabel.text() == '2 timepoints › 2 cycles › 3 planes'
    assert not w.templateValuesButton.isHidden()


def test_the_visit_strip_shows_the_order_of_the_loops_the_drive_moves_with(rig):
    positions, shades, _color, text = rig.widget.visitStrip(fit=100)
    # start 1 + 0.25 * (cycle + 2 * plane): cycle by cycle, its planes inside
    assert positions == pytest.approx([1.0, 1.5, 2.0, 1.25, 1.75, 2.25])
    assert shades == [0, 0, 0, 1, 1, 1]
    assert text == 'Z position at each step over the cycles and planes, in the order visited'
    positions, _shades, _color, text = rig.widget.visitStrip(fit=4)
    assert len(positions) == 3 and text.endswith('in the order visited (the first 3 of 6)')


def test_the_visit_strip_survives_what_does_not_concern_it(rig):
    w = rig.widget
    w.addLoop('condition')                        # a phase loop, innermost: Z does not move with it
    assert w.visitStrip(fit=100)[3].startswith('Z position at each step over the cycles and planes')
    w.timeline.table.item(0, 2).setText('soon')   # an event that does not read
    assert w.visitStrip(fit=100)[0] is not None


def test_lasers_are_drawn_in_the_colour_of_their_light(rig):
    assert rig.widget.deviceColor(rig.lasers[0]).name() == wavelength_color(405).name()
    violet, red, sted = wavelength_color(405), wavelength_color(640), wavelength_color(775)
    assert violet.blueF() > violet.redF() and red.redF() > red.blueF()
    assert abs(sted.blueF() - red.blueF()) > 0.1                    # 775 is not 640


# --- editing a part edits the tables -------------------------------------------------------

def test_a_count_edited_on_a_block_is_the_tables(rig):
    _chips(rig)[1].count.setValue(4)
    assert rig.widget.loops.rows(raw=True)[1]['count'] == '4'
    QtTest.QTest.qWait(rig.scan._reportTimer.interval() + 100)
    assert '24 steps' in rig.widget.reportText()          # 2 x 4 x 3 after the edit


def test_the_order_combo_offers_only_orders_the_model_takes(rig):
    w = rig.widget
    cycles, timepoints = _chips(rig)[1], _chips(rig)[0]
    cycles.order.setCurrentIndex(cycles.order.findData('serpentine'))
    row = w.loops.rows(raw=True)[1]
    assert (row['traversal'], row['parity_loops']) == ('serpentine', 'time')
    w.flushRefresh()
    QtTest.QTest.qWait(rig.scan._reportTimer.interval() + 100)
    assert w.statusChip.text() == 'Ready to run on nidaq', w.reportText()
    cycles.order.setCurrentIndex(cycles.order.findData('forward'))   # back: no parity
    assert w.loops.rows(raw=True)[1]['parity_loops'] == ''
    assert timepoints.order.findData('serpentine') < 0     # the slowest: never serpentine
    timepoints.order.setCurrentIndex(timepoints.order.findData('reverse'))
    assert w.loops.rows(raw=True)[0]['traversal'] == 'reverse'


def test_the_wheel_scrolls_past_a_field_not_clicked_into(shown):
    w = shown.widget
    count = _chips(shown)[1].count
    assert not count.hasFocus()
    wheel = QtGui.QWheelEvent(QtCore.QPointF(2, 2), QtCore.QPointF(count.mapToGlobal(
        QtCore.QPoint(2, 2))), QtCore.QPoint(0, 0), QtCore.QPoint(0, -120),
        QtCore.Qt.NoButton, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(count, wheel)
    assert count.value() == 2 and w.loops.rows(raw=True)[1]['count'] == '2'


def test_a_bar_dragged_moves_its_event_on_the_engines_grid(shown):
    w = shown.widget
    canvas = w.step.canvas
    first = _event(shown, 0)
    start = _centre(canvas._barRect(first))
    _drag(canvas, start, start + QtCore.QPoint(37, 0))
    row = w.timeline.rows()[0]
    grid = w.gridSeconds()
    assert grid == 1e-5
    assert row['start_s'] > first['start_s']
    assert row['start_s'] / grid == pytest.approx(round(row['start_s'] / grid))
    assert row['end_s'] - row['start_s'] == pytest.approx(first['end_s'] - first['start_s'])


def test_a_drag_stays_inside_the_step_and_keeps_a_length(shown):
    w = shown.widget
    canvas = w.step.canvas
    first = _event(shown, 0)
    rect = canvas._barRect(first)
    _drag(canvas, _centre(rect), _centre(rect) - QtCore.QPoint(600, 0))
    assert w.timeline.rows()[0]['start_s'] == 0.0             # not before the step
    edge = QtCore.QPoint(int(rect.left()), int(rect.center().y()))
    _drag(canvas, edge, edge + QtCore.QPoint(600, 0))          # the start past the end
    row = w.timeline.rows()[0]
    assert row['end_s'] - row['start_s'] == pytest.approx(w.gridSeconds())   # one tick left
    last = _event(shown, 3)
    _drag(canvas, _centre(canvas._barRect(last)), _centre(canvas._barRect(last))
          + QtCore.QPoint(600, 0))
    assert w.timeline.rows()[3]['end_s'] == pytest.approx(w.stepSeconds())   # not past it


def test_an_edge_dragged_resizes_the_event(shown):
    w = shown.widget
    canvas = w.step.canvas
    first = _event(shown, 0)
    rect = canvas._barRect(first)
    edge = QtCore.QPoint(int(rect.right()), int(rect.center().y()))
    _drag(canvas, edge, edge + QtCore.QPoint(40, 0))
    row = w.timeline.rows()[0]
    assert row['start_s'] == first['start_s'] and row['end_s'] > first['end_s']


def test_a_narrow_bar_moves_from_inside(shown):
    w = shown.widget
    w.setTableValue('timeline', 3, 'end_s', '5.01')            # the camera: 10 µs wide
    w.flushRefresh()
    canvas = w.step.canvas
    narrow = _event(shown, 3)
    assert canvas._barRect(narrow).width() < 3 * canvas.fontMetrics().averageCharWidth()
    _drag(canvas, _centre(canvas._barRect(narrow)), _centre(canvas._barRect(narrow))
          - QtCore.QPoint(30, 0))
    row = w.timeline.rows()[3]
    assert row['start_s'] < 5e-3 and row['end_s'] - row['start_s'] == pytest.approx(1e-5)


def test_a_value_typed_then_a_drag_keeps_both(shown):
    w = shown.widget
    w.step._select(0)
    end = w.step.inspector.end
    end.setFocus()
    end.selectAll()
    QtTest.QTest.keyClicks(end, '0.5')                     # typed, not yet committed
    canvas = w.step.canvas
    rect = canvas._barRect(_event(shown, 0))               # where the bar was drawn
    edge = QtCore.QPoint(int(rect.right()), int(rect.center().y()))
    edge -= QtCore.QPoint(int(rect.width() / 2), 0)        # where its new end will be
    end.clearFocus()          # the click commits it, with nothing drawn in between ...
    _drag(canvas, edge, edge + QtCore.QPoint(20, 0))     # ... and the drag starts from it
    row = w.timeline.rows()[0]
    assert 0.5e-3 < row['end_s'] < 1e-3


def test_the_arrow_keys_nudge_and_delete_removes(shown):
    w = shown.widget
    canvas = w.step.canvas
    w.step._select(1)
    before = w.timeline.rows()[1]
    canvas.setFocus()
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_Right)
    after = w.timeline.rows()[1]
    assert after['start_s'] - before['start_s'] == pytest.approx(w.gridSeconds())
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_Right, QtCore.Qt.ShiftModifier)
    assert w.timeline.rows()[1]['start_s'] - after['start_s'] == pytest.approx(
        10 * w.gridSeconds())
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key_Delete)
    assert len(w.timeline.rows()) == 3


def test_the_inspector_edits_the_selected_event(rig):
    w = rig.widget
    w.step._select(2)
    end = w.step.inspector.end
    end.setText('5.5')
    end.setModified(True)                                 # as typing does
    end.editingFinished.emit()
    assert w.timeline.rows(raw=True)[2]['end_s'] == '5.5'
    end.editingFinished.emit()                             # left untouched: writes nothing
    assert w.timeline.rows(raw=True)[2]['end_s'] == '5.5'


def test_the_menus_add_rows_with_the_models_defaults(rig):
    w = rig.widget
    row = w.addEvent(rig.lasers[2], 'ttl')
    added = w.timeline.rows()[row]
    # after the last event, to the step's end: to resize, not a guess
    assert (added['device'], added['start_s'], added['end_s']) == (
        rig.lasers[2], pytest.approx(6e-3), pytest.approx(w.stepSeconds()))
    w.addLoop('cycle')
    assert w.loops.rows()[-1]['id'] == 'cycle2'
    w.addDrive('X')
    motion = {k: w.drives.rows()[-1][k] for k in dataclasses.asdict(Motion())}
    assert motion == dataclasses.asdict(Motion())
    w.addBinding('APD')
    assert (w.bindings.rows()[-1]['window'], w.bindings.rows()[-1]['assembly']) == ('step',
                                                                                    'image')


def test_events_past_the_step_offer_to_lengthen_it(rig):
    w = rig.widget
    w.setTableValue('timeline', 3, 'end_s', '9')
    w.flushRefresh()
    offers = [n for n in w.stepContent()[2] if isinstance(n, tuple)]
    assert offers and offers[0][1] == 'Make the dwell 9 ms'
    offers[0][2]()
    assert w.stepPar.text() == '9'


# --- loops by name ---------------------------------------------------------------------

def test_removing_a_loop_takes_what_names_it(qtbot):
    rig = ExpertRig()
    try:
        rig.scan.setProgram(_ms_resolft(rig, pause_s=0.1))
        w = rig.widget
        assert [x['at'] for x in w.waits.rows(raw=True)] == ['time']
        w.removeLoop(0)
        assert w.waits.rows() == [] and 'time' not in w._loopIds()
        assert 'the pause before it' in w.noticeLabel.toolTip()
        w.undo()
        assert w._loopIds()[0] == 'time' and len(w.waits.rows()) == 1
        w.redo()
        assert w.waits.rows() == []
    finally:
        rig.close()


def test_renaming_a_loop_keeps_the_weights_on_it(rig):
    w = rig.widget
    w.renameLoop(1, 'c')
    assert w.drives.rows()[0]['w:c'] == 0.25
    w.loops.table.item(2, 0).setText('p')                   # renamed in the tables too
    assert w.drives.rows()[0]['w:p'] == 0.5


def test_undo_takes_back_an_edit(rig):
    w = rig.widget
    _chips(rig)[2].count.setValue(5)
    w.flushRefresh()
    w.undo()
    assert w.loops.rows(raw=True)[2]['count'] == '3'
    w.redo()
    assert w.loops.rows(raw=True)[2]['count'] == '5'


# --- the header and the check follow the engine ----------------------------------------------

def test_the_header_and_the_check_say_what_the_engine_makes_of_it(rig):
    w = rig.widget
    assert w.statusChip.text() == 'Ready to run on nidaq'
    assert w.check.verdict.text() == 'Yes, on nidaq'
    facts = [w.facts.itemAt(i).widget().text() for i in range(w.facts.count())]
    assert facts[0] == '12 steps' and facts[1].endswith('ms')
    assert facts[2:] == ['12 frames, Camera', 'exact timing, 10 µs clock']
    assert w.check.problems.isHidden() and w.noticeLabel.isHidden()
    assert w.engineLabel.isHidden()                          # one engine: the chip names it


def test_a_refused_program_says_why_once_and_knows_the_clock(qtbot):
    rig = ExpertRig()                     # refused from the start: no earlier check to recall
    try:
        w = rig.widget
        program = _ms_resolft(rig)
        rig.scan.setProgram(dataclasses.replace(program, step_s=program.step_s + 3e-6))
        assert w.statusChip.text() == 'Cannot run on nidaq'
        assert w.check.verdict.text() == 'No, on nidaq'
        problems = w.check.problems.text()
        assert 'not on' in problems and not w.noticeLabel.isHidden()
        details = [w.check.details.itemAt(i).widget().text()
                   for i in range(w.check.details.count())]
        assert details and not any(line.split(':', 1)[-1].strip() in problems
                                   for line in details if ':' in line)
        assert w.gridSeconds() == 1e-5                      # snapping knows the clock
    finally:
        rig.close()


def test_the_same_problem_in_many_steps_is_said_once():
    lines = ['L1 in step 3 leaves its step.', 'L1 in step 4 leaves its step.', 'Other.']
    assert collapse(lines) == ['L1 in step 3 leaves its step. (and 1 more like it)', 'Other.']


def test_running_shows_in_the_header(rig):
    w = rig.widget
    w.showScanProgress(0.5, 'Step 6 of 12')
    assert w.statusChip.text() == 'Running'
    w.hideScanProgress()
    assert w.statusChip.text() == 'Ready to run on nidaq'


def test_a_trajectory_is_shown_as_its_path(rig):
    w = rig.widget
    rig.scan.setProgram(_spiral(rig))
    assert not w.trajectoryCard.isHidden() and w.order.isHidden() and w.step.isHidden()
    assert w.titleLabel.text() == 'Trajectory: Spiral'
    assert 'follow a spiral' in w.trajectoryCard.text.text()
    requested = []
    w.askPresetParams = lambda preset, initial, validate, **_rig: requested.append(
        (preset.id, initial['radius_um']))                 # the form, filled in
    w.trajectoryCard.editButton.click()
    assert requested == [('trajectory-spiral', 10.0)]


def test_an_empty_program_offers_the_templates(rig):
    w = rig.widget
    rig.scan.setProgram(None)
    assert not w.start.isHidden() and w.order.isHidden()
    assert w.waitsCard.isHidden() and w.recording.isHidden()   # nothing to pause or record
    assert w.statusChip.text() == 'Empty' and w.check.verdict.isHidden()
    assert w.titleLabel.text() == 'New scan' and w.templateValuesButton.isHidden()
    titles = [w.start.flow.itemAt(i).widget().text() for i in range(w.start.flow.count())]
    assert 'MS-RESOLFT (cycles x planes)' in titles and 'Trajectory ▾' in titles
    facts = [w.facts.itemAt(i).widget().text() for i in range(w.facts.count())]
    assert facts == ['exact timing, 10 µs clock']           # the panel's own policy, strict


def test_a_missing_step_says_so_in_words(rig):
    rig.widget.stepPar.setText('')
    with pytest.raises(ValueError, match='Set the dwell'):
        rig.widget.programDict()


def test_a_refusal_is_said_in_the_panels_units_and_its_first_reason_in_the_header(rig):
    w = rig.widget
    w.setTableValue('timeline', 0, 'end_s', '50')
    w.flushRefresh()
    QtTest.QTest.qWait(rig.scan._reportTimer.interval() + 100)
    assert w.statusChip.text() == 'Cannot run on nidaq'
    assert 'outside its step (0 to 7 ms)' in w.check.problems.text()      # ms, not 0.007 s
    assert w.noticeLabel.text().startswith('event pulse1')                 # the reason, not the heading
    facts = [w.facts.itemAt(i).widget().text() for i in range(w.facts.count())]
    assert 'exact timing, 10 µs clock' in facts     # strict-v1 still: the panel's policy combo


def test_change_template_values_reopens_the_form(rig):
    w = rig.widget
    asked = []
    # the form itself is modal: stub it, or the click would wait for a hand
    w.askPresetParams = lambda preset, initial, validate, **_rig: asked.append(
        (preset.id, initial['cycles'])) or None
    w.templateValuesButton.click()
    assert asked == [('ms-resolft', 2)]                    # the values it was made from
    assert 'edited' not in w.templateValuesButton.toolTip()
    _chips(rig)[2].count.setValue(4)
    w.flushRefresh()
    QtTest.QTest.qWait(rig.scan._reportTimer.interval() + 100)
    assert w.titleLabel.text().endswith(', edited')
    assert not w.templateValuesButton.isHidden() and 'edited' in w.templateValuesButton.toolTip()


def test_choosing_another_kind_of_detector_takes_its_window_and_assembly(rig):
    w = rig.widget
    w.setBindingDetector(0, 'APD')
    row = w.bindings.rows()[0]
    assert (row['detector'], row['events'], row['window'], row['assembly']) == (
        'APD', 'APD', 'step', 'image')
    w.refreshParts()
    editor = w.recording._editors[0]
    assert not editor.pixels.isHidden() and editor.frames.isHidden()


def test_the_template_form_fills_in_from_the_rig_and_guesses_nothing(rig):
    from imswitch.imcontrol.view.widgets.ScanWidgetExpert import PresetDialog
    dialog = PresetDialog(presets.PRESETS['raster'], rig.widget.devices, {}, lambda p: '',
                          positions={'X': 3.0, 'Y': -1.5}, setup={'move_time': 0.002})
    field = {name: editor for name, (_f, editor) in dialog._editors.items()}
    assert (field['x_device'].currentText(), field['y_device'].currentText()) == ('X', 'Y')
    assert (field['x_start_um'].text(), field['y_start_um'].text()) == ('3', '-1.5')
    assert field['move_s'].text() == '2'                               # declared: 2 ms
    assert field['settle_s'].text() == '' and field['settle_s'].placeholderText() == 'required'
    field['x_device'].setCurrentText('Y')                              # follows the positioner
    assert field['x_start_um'].text() == '-1.5'
    field['y_start_um'].setText('7')                                   # typed: left alone
    field['y_device'].setCurrentText('X')
    assert field['y_start_um'].text() == '7'
    titles = [dialog.layout().itemAt(1).layout().itemAt(i).widget().text()
              for i in range(dialog.layout().itemAt(1).layout().count())
              if dialog.layout().itemAt(1).layout().itemAt(i).widget() is not None
              and dialog.layout().itemAt(1).layout().itemAt(i).widget().objectName()
              == 'expertCardTitle']
    assert titles == ['Positions', 'Pulses', 'Camera', 'Motion']


def test_the_last_template_values_are_offered_again_and_kept_in_the_state(rig):
    params = dict(x_device='X', y_device='Y', x_pixels=3, y_pixels=2, x_step_um=0.5,
                  y_step_um=0.5, x_start_um=0.0, y_start_um=0.0,
                  pulses=[[rig.lasers[0], 1e-3, 0.0]], camera='', exposure_pulse=1,
                  move_s=5e-4, settle_s=5e-4, return_s=1e-3, traversal='forward',
                  transition='jump')
    rig.widget.askPresetParams = lambda preset, initial, validate, **_rig: params
    rig.scan.makeFromPreset('raster')
    rig.scan.setProgram(_ms_resolft(rig))                 # another scan in between
    offered = []
    rig.widget.askPresetParams = lambda preset, initial, validate, **_rig: offered.append(initial)
    rig.scan.makeFromPreset('raster')
    assert offered == [params]
    state = rig.scan.getComponentState()
    assert state['templateValues']['raster'] == params
    rig.scan._templateValues = {}
    rig.scan.applyComponentState(state, applyMode=None)
    assert rig.scan._templateValues['raster'] == params


# --- one state, two views ------------------------------------------------------------------

def test_the_tables_are_the_same_program(rig):
    w = rig.widget
    w.tablesButton.click()
    assert w.pages.currentIndex() == 1
    w.loops.table.item(0, 2).setText('5')
    w.refreshParts()
    assert _chips(rig)[0].count.value() == 5
    assert program_to_dict(rig.scan.program())['loops'][0]['count'] == 2     # until read back
    w.tablesButton.click()
    assert w.pages.currentIndex() == 0


# --- the layout and the preview ----------------------------------------------------------------

def test_the_panel_puts_the_check_beside_the_editor_only_when_both_fit(rig, qtbot):
    w = rig.widget
    w.show()
    w.resize(w.columns.threshold() + 200, 900)
    qtbot.waitUntil(lambda: w.columns.twoColumns(), timeout=2000)
    w.resize(w.columns.threshold() // 2, 900)
    qtbot.waitUntil(lambda: not w.columns.twoColumns(), timeout=2000)


def test_the_preview_draws_every_device_on_its_lane_and_zooms_to_a_step(rig):
    w = rig.widget
    rig.scan.plotProgram()
    ticks = [label for _pos, label in w.plot.getAxis('left')._tickLevels[0]]
    assert ticks[0].startswith('Z (') and set(ticks[1:]) == set(rig.lasers[:2]) | {'Camera'}
    assert len(w.plot.listDataItems()) == len(ticks)
    step_window = w.preview._windows[0]
    (lo, hi), _ = w.plot.getViewBox().viewRange()
    assert (lo, hi) == pytest.approx(step_window)
    assert step_window[1] - step_window[0] == pytest.approx(w.stepSeconds())
    w.preview.zoom.button(2).click()
    (lo, hi), _ = w.plot.getViewBox().viewRange()
    assert hi - lo > 2 * w.stepSeconds()


def test_one_pass_runs_to_the_end_of_its_return(qtbot):
    rig = ExpertRig()
    try:
        rig.scan.setProgram(presets.make('raster', dict(
            x_device='X', y_device='Y', x_pixels=3, y_pixels=2, x_step_um=0.5, y_step_um=0.5,
            x_start_um=0.0, y_start_um=0.0, pulses=[[rig.lasers[0], 1e-3, 0.0]], camera='',
            exposure_pulse=1, move_s=5e-4, settle_s=5e-4, return_s=1e-3, traversal='forward',
            transition='jump')))
        rig.scan.plotProgram()
        step, one_pass, _ = rig.widget.preview._windows
        assert one_pass[0] == step[0]
        assert one_pass[1] - one_pass[0] == pytest.approx(3 * rig.widget.stepSeconds() + 1e-3)
    finally:
        rig.close()


def test_a_preview_that_cannot_be_drawn_says_why_where_it_would_be(rig):
    w = rig.widget
    rig.scan.plotProgram()
    program = _ms_resolft(rig)
    rig.scan.setProgram(dataclasses.replace(program, step_s=program.step_s + 3e-6))
    rig.scan.plotProgram()
    assert w.plot.isHidden() and not w.preview.problem.isHidden()
    assert w.preview.problem.text().startswith('Nothing to draw:')


def test_an_edit_marks_the_preview_out_of_date(rig):
    w = rig.widget
    rig.scan.plotProgram()
    assert w.preview.stale.text() == ''
    _chips(rig)[2].count.setValue(4)
    assert w.preview.stale.text() == 'out of date'


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
