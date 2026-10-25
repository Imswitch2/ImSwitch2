"""The Expert panel's Simple view (docs/design/plans/expert-simple-view.md):
the scan's values as a list, beside the drawing of its step and the preview,
with nothing that adds, removes, renames or reorders.

The **Parameters** card lists the values the scan is made from. A scan that
is its template's output shows the template's own form, inline, each
committed field making the scan again from the template (the panel's
``sigTemplateEdited``, which the controller answers); the card is given the
form class it hosts, so this module imports nothing from the panel. Any other
scan -- built by hand, edited in the Expert view, an Advanced file -- shows
its own values (:class:`ScanValuesForm`): every value the Expert parts edit,
as label and field, written to the panel's tables exactly as the parts write
them, in sections in the order the scan happens.

What the card shows changes under no one's hands: a new scan replaces it
whole, the field being typed in too; a change it makes by itself waits until
the focus leaves it. Whatever it removes is disconnected first, hidden, and
deleted later, never on the stack of a signal of its own.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from qtpy import QtWidgets

from ._expert_scan_parts import (
    EVENT_WORDS, HOLD_WORDS, KIND_NAMES, ORIGIN_WORDS, PLACEMENT_WORDS, RESTORE_WORDS,
    SHAPE_WORDS, TIMEOUT_WORDS, TRANSITION_WORDS, TRAVERSAL_WORDS, Card, FlowLayout, _combo,
    _field, _group, _muted, _on_commit, _quiet_set, _set_combo, role_color,
)

#: The most a spin box holds: a count has no other limit.
_MOST = 2 ** 31 - 1

#: The tables the listing is made of.
_TABLES = ('loops', 'drives', 'timeline', 'waits', 'detectors')

#: What the card shows.
_TEMPLATE, _VALUES = 'template', 'values'


def _usual(panel, row) -> bool:
    """Whether a detector's binding has the window and assembly its kind
    takes (the only ones the model accepts for it)."""
    expected = ('step', 'image') if panel.isPointDetector(row['detector']) else ('event', 'stream')
    return (row['window'], row['assembly']) == expected


def values_signature(panel) -> tuple:
    """What the listing's rows are made of: the dimensions, the positioners,
    the events, the pauses and the bindings, by what names them and not by
    any value a field edits. A listing is built for one signature and kept
    for as long as the tables hold it; another signature is another listing.
    No field of the listing changes its own signature: it writes values."""
    return (
        tuple((row['id'], row['kind']) for row in panel.tableRows('loops')),
        tuple((row['device'], bool(row['table_loop'])) for row in panel.tableRows('drives')),
        tuple((row['device'], row['kind'], row['anchor'], row['phases'], row['id'])
              for row in panel.tableRows('timeline')),
        tuple((row['at'], row['trigger']) for row in panel.tableRows('waits')),
        tuple((row['detector'], panel.isPointDetector(row['detector']), _usual(panel, row),
               row['events'], row['phases']) for row in panel.tableRows('detectors')),
    )


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _flow(*widgets) -> QtWidgets.QWidget:
    """Widgets side by side that wrap onto the next line when the width runs
    out: a row of fields never asks the panel for a sideways scroll."""
    host = QtWidgets.QWidget()
    layout = FlowLayout(host)
    for widget in widgets:
        layout.addWidget(widget)
    return host


def _style(widget, sheet: str):
    if widget.styleSheet() != sheet:
        widget.setStyleSheet(sheet)


class ScanValuesForm(QtWidgets.QWidget):
    """The scan's own values (plan D7): every number and choice the Expert
    parts edit, as label and field, in the panel's words and in sections in
    the order the scan happens -- *Scan order*, *Within one step*,
    *Positions*, *Pauses and triggers*, *Recording* -- a section with nothing
    in it left out.

    A field writes the panel's tables exactly as the Expert part does, through
    ``setTableValue`` (and the dwell, and ``setLoopOrder``): one state, and a
    value that does not read is reported where it is read. Whatever makes the
    structure is a label, never a field, and nothing here adds, removes,
    renames or reorders: no button of any kind. A *moves per* field is a value
    like any other -- blank or a number, its row always there; blank means the
    dimension does not move the positioner.

    The form is built for one ``signature()`` and ``refresh()`` writes values
    only, quietly, never into the field the user is in."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel
        self._live = True
        self._signature = values_signature(panel)
        #: The section forms by title, their headings, and the widget that
        #: edits each value by (table, row, key); the dwell is ('step', 0, 'step_s').
        self.sections: Dict[str, QtWidgets.QFormLayout] = {}
        self.headings: Dict[str, QtWidgets.QLabel] = {}
        self.fields: Dict[tuple, QtWidgets.QWidget] = {}
        self.notes: Dict[int, QtWidgets.QLabel] = {}       # where each event plays
        self.ranges: Dict[int, QtWidgets.QLabel] = {}      # where each positioner goes
        self._refreshers: List[Callable[[Dict], None]] = []
        self._column = QtWidgets.QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        rows = {name: panel.tableRows(name) for name in _TABLES}
        self._buildOrder(rows['loops'])
        self._buildStep(rows['timeline'])
        self._buildPositions(rows['drives'])
        self._buildPauses(rows['waits'], rows['loops'])
        self._buildRecording(rows['detectors'])
        self.refresh()

    # -- the form as a whole --------------------------------------------------------

    def signature(self) -> tuple:
        """What the form was built for (:func:`values_signature`)."""
        return self._signature

    def refresh(self):
        """Write the tables' values into the fields, quietly: never the field
        the user is in, and never a signal that would write them back."""
        rows = {name: self._panel.tableRows(name) for name in _TABLES}
        for refresher in self._refreshers:
            refresher(rows)

    def detach(self):
        """Take the form out of use: no field writes any more, so a field that
        commits as it is hidden (focus out) writes nowhere."""
        self._live = False

    def _writes(self) -> bool:
        """Whether a field may write. Not once the form is taken out of use,
        and not once the tables are no longer the ones it was built from: the
        card keeps a form while the user is in it, and its rows would be other
        rows."""
        return self._live and self._signature == values_signature(self._panel)

    def _write(self, table, row, key, text):
        if self._writes():
            self._panel.setTableValue(table, row, key, text)

    # -- building blocks --------------------------------------------------------------

    def _section(self, title: str) -> QtWidgets.QFormLayout:
        heading = QtWidgets.QLabel(title)
        heading.setObjectName('expertCardTitle')
        form = QtWidgets.QFormLayout()
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self._column.addWidget(heading)
        self._column.addLayout(form)
        self.headings[title], self.sections[title] = heading, form
        return form

    @staticmethod
    def _row(form: QtWidgets.QFormLayout, text: str, field: QtWidgets.QWidget, tip=''):
        label = QtWidgets.QLabel(text)
        if tip:
            label.setToolTip(tip)
        if isinstance(field, QtWidgets.QComboBox):
            field = _flow(field)          # a choice keeps the width of its words
        form.addRow(label, field)

    def _register(self, table, row, key, widget, refresher):
        self.fields[(table, row, key)] = widget
        self._refreshers.append(refresher)

    def _line(self, table, row, key, width=None, tip='', placeholder='') -> QtWidgets.QLineEdit:
        """A text field of a table's cell: written when the user leaves a
        value they changed."""
        line = _field(width, tip) if width else QtWidgets.QLineEdit()
        if tip and not width:
            line.setToolTip(tip)
        if placeholder:
            line.setPlaceholderText(placeholder)
        _on_commit(line, lambda text: self._write(table, row, key, text))
        self._register(table, row, key, line, lambda rows: _quiet_set(
            line, line.setText, rows[table][row].get(key, '')))
        return line

    def _choice(self, table, row, key, words, tip='', choices=None, fallback='',
                write=None) -> QtWidgets.QComboBox:
        """A choice of a table's cell, in words. ``write(value)`` instead of
        writing the cell (the order of a dimension); ``fallback`` for a cell
        that holds nothing."""
        combo = _combo(words if choices is None else choices, words, tip)
        save = write or (lambda value: self._write(table, row, key, value))
        combo.currentIndexChanged.connect(lambda _i: save(combo.currentData()))
        self._register(table, row, key, combo, lambda rows: _set_combo(
            combo, rows[table][row][key] or fallback))
        return combo

    def _count(self, table, row, key, tip) -> QtWidgets.QSpinBox:
        """A whole number of at least 1: a dimension's count, frames per
        step. What the table holds and is not that is shown in red, with what
        it holds."""
        spin = QtWidgets.QSpinBox()
        spin.setRange(1, _MOST)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda value: self._write(table, row, key, str(value)))

        def refresh(rows):
            text = rows[table][row][key]
            try:
                value = int(text)
                if not spin.minimum() <= value <= spin.maximum():
                    raise ValueError(text)
            except ValueError:
                _style(spin, f'color: {role_color("bad")};')
                spin.setToolTip(f'The table holds {text!r}, not a count.')
                return
            _quiet_set(spin, spin.setValue, value)
            _style(spin, '')
            spin.setToolTip(tip)

        self._register(table, row, key, spin, refresh)
        return spin

    # -- Scan order -----------------------------------------------------------------------

    def _buildOrder(self, loops):
        if not loops:
            return
        form = self._section('Scan order')
        kinds = [row['kind'] for row in loops]
        for index, loop in enumerate(loops):
            many = KIND_NAMES.get(loop['kind'], (loop['kind'],) * 2)[1]
            count = self._count('loops', index, 'count', 'How many iterations.')
            # the slowest dimension has none outside it to alternate with
            orders = [value for value in TRAVERSAL_WORDS if index or value != 'serpentine']
            order = self._choice(
                'loops', index, 'traversal', TRAVERSAL_WORDS,
                'The order its iterations run in. Serpentine: every other pass of the '
                'dimension outside it runs backwards.', choices=orders, fallback='forward',
                write=lambda value, i=index: self._writeOrder(i, value))
            self._refreshers.append(lambda rows, i=index, box=order: self._orderTip(
                box, rows['loops'][i]))
            self._row(form, _capital(many) + (f' ({loop["id"]})' if kinds.count(loop['kind']) > 1
                                             else ''),
                      _flow(count, order),
                      f'Dimension {loop["id"]!r} ({loop["kind"]}). The positioners\' formulas '
                      'and the events\' anchors name it by its id.')

    def _writeOrder(self, index, traversal):
        """A serpentine alternates with the dimension right outside it."""
        if self._writes():
            self._panel.setLoopOrder(index, traversal, 1)

    @staticmethod
    def _orderTip(combo, loop):
        traversal = loop['traversal'] or 'forward'
        alternates = f', alternating with {loop["parity_loops"]}' if loop['parity_loops'] else ''
        combo.setToolTip(f'Order: {TRAVERSAL_WORDS.get(traversal, traversal)}{alternates}.')

    # -- Within one step -----------------------------------------------------------------

    def _buildStep(self, timeline):
        form = self._section('Within one step')
        dwell = _field(6, 'The dwell: the length of every step, in ms.')
        dwell.setPlaceholderText('set')
        _on_commit(dwell, self._writeDwell)
        self._register('step', 0, 'step_s', dwell, self._refreshDwell)
        self._row(form, 'Dwell (ms)', dwell)
        same: Dict[tuple, List[int]] = {}
        for index, event in enumerate(timeline):
            same.setdefault((event['device'], event['kind']), []).append(index)
        for index, event in enumerate(timeline):
            label = f'{event["device"]} {EVENT_WORDS.get(event["kind"], event["kind"])}'.strip()
            alike = same[(event['device'], event['kind'])]
            if len(alike) > 1:
                label += f' ({event["id"]})' if event['id'] else f' {alike.index(index) + 1}'
            start = self._line('timeline', index, 'start_s', 6,
                               'When it starts, in ms from the step\'s start (from its end for '
                               'the last step of a pass: then ≤ 0).')
            end = self._line('timeline', index, 'end_s', 6, 'When it ends, in ms.')
            fields = [_group('from', start), _group('to', end, 'ms')]
            if event['kind'] in ('level', 'offset') or event['value']:
                value = self._line('timeline', index, 'value', 5,
                                   'The power level, or how far it moves (µm).')
                shape = self._choice('timeline', index, 'shape', SHAPE_WORDS,
                                     'Held through the event, or ramped to the value.',
                                     fallback='hold')
                fields.append(_group('value', value, shape))
            note = _muted('')
            note.setVisible(False)
            self.notes[index] = note
            host = QtWidgets.QWidget()
            column = QtWidgets.QVBoxLayout(host)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            column.addWidget(_flow(*fields))
            column.addWidget(note)
            self._row(form, label, host)
        self._refreshers.append(self._refreshNotes)

    def _writeDwell(self, text):
        if self._writes():
            self._panel.stepPar.setText(text)

    def _refreshDwell(self, _rows):
        panel = self._panel
        dwell = self.fields[('step', 0, 'step_s')]
        _quiet_set(dwell, dwell.setText, panel.stepPar.text())
        _style(dwell, '' if panel.stepSeconds() is not None
               else f'border: 1px solid {role_color("bad")};')

    def _refreshNotes(self, _rows):
        """Where each event plays (the pass it is anchored to, its phases), as
        the drawing words it."""
        if not self.notes:
            return
        where = {event['row']: event.get('where', '') for event in self._panel.stepContent()[0]}
        for index, note in self.notes.items():
            note.setText(where.get(index, ''))
            note.setVisible(bool(where.get(index, '')))

    # -- Positions ----------------------------------------------------------------------------

    def _buildPositions(self, drives):
        if not drives:
            return
        form = self._section('Positions')
        names = self._panel.loopNames()
        for index, drive in enumerate(drives):
            heading = QtWidgets.QWidget()
            line = QtWidgets.QHBoxLayout(heading)
            device = QtWidgets.QLabel(drive['device'])
            bold = device.font()
            bold.setBold(True)
            device.setFont(bold)
            # a positioner's rows are told from the previous one's by a gap
            line.setContentsMargins(0, device.fontMetrics().height() // 2 if index else 0, 0, 0)
            span = _muted('')
            self.ranges[index] = span
            line.addWidget(device)
            line.addWidget(span, 1)
            form.addRow(heading)
            self._row(form, 'Starts at (µm)', self._line(
                'drives', index, 'offset_um', 6,
                'Where it is at the first iteration of every dimension, in µm.'))
            if drive['table_loop']:
                loop = drive['table_loop']
                self._row(form, f'Positions over {names.get(loop, loop)} (µm)', self._line(
                    'drives', index, 'table_um', None,
                    'A position per count of the dimension, in µm, separated by commas.'))
            else:
                for loop, name in names.items():
                    self._row(form, f'Moves per {name} (µm)', self._line(
                        'drives', index, f'w:{loop}', 5,
                        f'How far it moves for each {name} ({loop}), in µm. Blank: this '
                        'dimension does not move it.', placeholder='0'))
            self._row(form, 'Moves', _flow(
                self._choice('drives', index, 'transition', TRANSITION_WORDS,
                             'How it goes from one position to the next.'),
                self._choice('drives', index, 'placement', PLACEMENT_WORDS,
                             'At the end of each step (inside its dwell), or in a pause '
                             'between steps.')))
            self._row(form, 'Move time (ms)', self._line(
                'drives', index, 'move_s', 4, 'Time the move takes, in ms.'))
            self._row(form, 'Settle time (ms)', self._line(
                'drives', index, 'settle_s', 4, 'Time it then needs to stand still, in ms.'))
            self._row(form, 'Line return (ms)', self._line(
                'drives', index, 'return_s', 4,
                'Time to return to the start of a line, after every pass of the fastest '
                'dimension, in ms.'))
            self._row(form, 'Positions are', self._choice(
                'drives', index, 'origin', ORIGIN_WORDS,
                'Positions as written, or added to where it is when the scan starts.'))
            self._row(form, 'After the scan', self._choice(
                'drives', index, 'restore', RESTORE_WORDS, 'Where it goes after the scan.'))
        self._refreshers.append(self._refreshRanges)

    def _refreshRanges(self, _rows):
        for index, span in self.ranges.items():
            text = self._panel.driveRange(index)
            span.setText(text)
            span.setVisible(bool(text))

    # -- Pauses and triggers ------------------------------------------------------------------

    @staticmethod
    def _place(at, loops) -> str:
        """Where a pause is, in the words of the pause's own editor."""
        if at == 'start':
            return 'the scan starts'
        if at == 'step':
            return 'every step'
        kinds = [loop['kind'] for loop in loops if loop['id']]
        for loop in loops:
            if loop['id'] == at:
                one = KIND_NAMES.get(loop['kind'], (at,))[0]
                return f'each {one} ({at})' if kinds.count(loop['kind']) > 1 else f'each {one}'
        return at

    def _buildPauses(self, waits, loops):
        if not waits:
            return
        form = self._section('Pauses and triggers')
        for index, wait in enumerate(waits):
            place = f'Before {self._place(wait["at"], loops)}'
            if wait['trigger'] == 'time':
                self._row(form, f'{place}: pause (ms)', self._line(
                    'waits', index, 'interval_s', 5, 'How long a pause lasts, in ms.',
                    placeholder='set'))
            else:
                form.addRow(QtWidgets.QLabel(f'{place}: wait for a trigger'))
                self._row(form, 'Shortest time between triggers (ms)', self._line(
                    'waits', index, 'interval_s', 5,
                    'The shortest time between two releases, in ms.', placeholder='set'))
                self._row(form, 'Give up after (ms)', _flow(
                    self._line('waits', index, 'timeout_s', 5,
                               'How long to wait for the trigger, in ms. Blank: for ever.',
                               placeholder='never'),
                    self._choice('waits', index, 'on_timeout', TIMEOUT_WORDS,
                                 'What happens when the trigger does not come.')))
            self._row(form, 'Meanwhile the lasers are', self._choice(
                'waits', index, 'hold', HOLD_WORDS,
                'The lasers and camera windows while it waits.'))

    # -- Recording ---------------------------------------------------------------------------------

    def _buildRecording(self, bindings):
        if not bindings:
            return
        form = self._section('Recording')
        for index, binding in enumerate(bindings):
            point = self._panel.isPointDetector(binding['detector'])
            phases = f', in phases {binding["phases"]}' if binding['phases'].strip() else ''
            if not _usual(self._panel, binding):
                field = QtWidgets.QLabel(
                    f'window {binding["window"]!r}, assembly {binding["assembly"]!r} (set in '
                    'Tables, in the Expert view): the check says whether the model takes it')
                field.setObjectName('expertProblems')
                field.setWordWrap(True)
            elif point:
                field = QtWidgets.QLabel(
                    f'integrates each whole step into one pixel of an image{phases}')
                field.setWordWrap(True)
            else:
                frames = self._count('detectors', index, 'per_step',
                                     'How many frames each step records.')
                text = QtWidgets.QLabel(f'frames per step, triggered by {binding["events"]}'
                                        f'{phases}')
                text.setWordWrap(True)
                field = QtWidgets.QWidget()
                line = QtWidgets.QHBoxLayout(field)
                line.setContentsMargins(0, 0, 0, 0)
                line.addWidget(frames)
                line.addWidget(text, 1)
            self._row(form, binding['detector'], field)


class ParametersCard(Card):
    """The scan's values: the template's form when the scan is its template's
    output, else its own values. Needs from the panel: ``template()``,
    ``listsValues()``, ``editedTemplateTitle()``, ``devices``,
    ``editPending()``, ``refreshParts()`` and the signals ``sigTemplateEdited``
    and ``sigPresetRequested``; the listing needs the panel's tables and what
    reads them (see :class:`ScanValuesForm`)."""

    #: Above the scan's own values, when the scan was made from a template and
    #: edited since.
    EDITED = ('Edited since it was made from {title}: its values are listed. Change template '
              'values… makes it again from the template, without the edits.')

    def __init__(self, panel, formClass, parent=None):
        super().__init__('Parameters', parent)
        self._panel, self._formClass = panel, formClass
        self.valuesLabel = _muted('')
        self.valuesLabel.setVisible(False)
        self.header.addWidget(self.valuesLabel)
        self.editedLabel = _muted('')
        self.editedLabel.setVisible(False)
        self.body.addWidget(self.editedLabel)
        self.holder = QtWidgets.QWidget()
        self._holderLayout = QtWidgets.QVBoxLayout(self.holder)
        self._holderLayout.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.holder)
        self._form = None           # the template's form
        self._values = None         # the scan's own values
        self._invalid = False       # the next refresh replaces the content, whatever has the focus
        self._pending = False       # a change of content was put off
        self._waiting = False       # ... and the card listens for the focus to leave

    # -- what it shows ------------------------------------------------------------

    def form(self):
        """The template's form, or None."""
        return self._form

    def values(self):
        """The scan's own values, or None."""
        return self._values

    def showsTemplate(self) -> bool:
        return self._form is not None

    def showsValues(self) -> bool:
        return self._values is not None

    def shows(self) -> bool:
        """Whether it has anything to show."""
        return self._form is not None or self._values is not None

    def invalidate(self):
        """The next refresh replaces the content, whatever has the focus
        (entering the view)."""
        self._invalid = True

    def programShown(self):
        """A new scan is shown: its content replaces this one, the field being
        typed in too -- what that field showed was for the scan it replaced.
        Unless the template's form is committing: its own commit is what made
        the scan. This only marks; the refresh after the tables are written
        builds the new content."""
        if self._form is not None and self._form.isCommitting():
            return
        self.invalidate()

    def _wanted(self):
        """What the card should show now: ``(_TEMPLATE, the template's id)``,
        ``(_VALUES, the signature of the tables)`` or None."""
        preset = (self._panel.template() or (None,))[0]
        if preset is not None:
            return (_TEMPLATE, preset.id)
        if self._panel.listsValues():
            return (_VALUES, values_signature(self._panel))
        return None

    def _shown(self):
        """What the card shows, in the form of ``_wanted``."""
        if self._form is not None:
            return (_TEMPLATE, self._form.preset.id)
        if self._values is not None:
            return (_VALUES, self._values.signature())
        return None

    def refresh(self):
        preset, params, problem = self._panel.template() or (None, {}, '')
        wanted, shown = self._wanted(), self._shown()
        if wanted != shown or (wanted is not None and self._invalid):
            if self._invalid or self._mayChange():
                self._replace(wanted, preset, params, problem)
            else:
                self._putOff()
        else:
            self._pending = False
            self._stopWaiting()
            if self._form is not None:
                self._form.setProblem(problem)
                if not problem:
                    self._form.setValues(params)
            elif self._values is not None:
                self._values.refresh()
        self._invalid = False
        self._showHeader()

    def _showHeader(self):
        shown = self._shown()
        self.valuesLabel.setText('' if shown is None else
                                 'the template\'s values' if shown[0] == _TEMPLATE else
                                 'the scan\'s values')
        self.valuesLabel.setVisible(shown is not None)
        title = self._panel.editedTemplateTitle() if self._values is not None else ''
        self.editedLabel.setText(self.EDITED.format(title=title) if title else '')
        self.editedLabel.setVisible(bool(title))

    # -- replacing the content ----------------------------------------------------

    def _replace(self, wanted, preset, params, problem):
        self._pending = False
        self._stopWaiting()
        self._remove()
        if wanted is None:
            return
        if wanted[0] == _TEMPLATE:
            form = self._formClass(preset, self._panel.devices, params)
            form.sigCommitted.connect(
                lambda pid=preset.id: self._panel.sigTemplateEdited.emit(pid))
            form.sigEditRequested.connect(
                lambda pid=preset.id: self._panel.sigPresetRequested.emit(pid))
            form.setProblem(problem)
            self._holderLayout.addWidget(form)
            self._form = form
        else:
            values = ScanValuesForm(self._panel)
            self._holderLayout.addWidget(values)
            self._values = values

    def _remove(self):
        """Take the content away. Its signals go first: a field that commits as
        it is hidden (focus out) writes nowhere. It is deleted later, not
        here: this may run inside a signal of one of its own editors."""
        form, self._form = self._form, None
        values, self._values = self._values, None
        if form is not None:
            for signal in (form.sigCommitted, form.sigEditRequested):
                try:
                    signal.disconnect()
                except TypeError:
                    pass              # nothing was connected
        if values is not None:
            values.detach()
        for gone in (form, values):
            if gone is not None:
                gone.hide()
                gone.setParent(None)
                gone.deleteLater()

    # -- no change under the user's hands -----------------------------------------

    def _holdsFocus(self) -> bool:
        focus = QtWidgets.QApplication.focusWidget()
        return focus is not None and self.isAncestorOf(focus)

    def _mayChange(self) -> bool:
        """Whether a change the card makes by itself can be made now: not
        while a field of the card has the focus, and not while a change put
        off waits for the report of an edit -- leaving a field commits it
        before the focus signal comes, and its read-back arrives only with
        the report, which decides."""
        return not self._holdsFocus() and not (self._pending and self._panel.editPending())

    def _putOff(self):
        """Keep the content as it is, live, until the focus leaves the card
        (and the edit it brought is reported). The scan's own values stay
        current for as long as the tables still have the structure they were
        built for."""
        self._pending = True
        if self._holdsFocus() and not self._waiting:
            self._waiting = True
            QtWidgets.QApplication.instance().focusChanged.connect(self._focusChanged)
        if self._values is not None and self._values.signature() == values_signature(self._panel):
            self._values.refresh()

    def _stopWaiting(self):
        if self._waiting:
            self._waiting = False
            QtWidgets.QApplication.instance().focusChanged.disconnect(self._focusChanged)

    def _focusChanged(self, _old, now):
        if now is not None and self.isAncestorOf(now):
            return                    # moved inside the card: still in use
        self._stopWaiting()
        # Leaving a field commits it before this signal comes: if that edit is
        # not reported yet, the refresh its report brings decides.
        if not self._panel.editPending():
            self._panel.refreshParts()


__all__ = ['ParametersCard', 'ScanValuesForm', 'values_signature']


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
