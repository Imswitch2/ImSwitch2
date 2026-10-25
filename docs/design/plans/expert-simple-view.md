# The Expert panel's simple view

Branch `feat/scan-engines` (one effort, one branch). Draft 3, 2026-10-25 (reviews taken: see *Review round 1*).

## The request (Lenny, 2026-10-25, from the rig tests)

> In a similar manner to the cloaks of simple scan I would like to be able to
> activate a cloak for the expert scan module as well. However there it would
> be much simpler. A button which changes the widget appearance. It should
> minimalize it to a simple listing of all the input parameters and the views
> of pulses if there are any and the option to show the preview of the
> signaling. The advantage would be that all the functionality to add or
> remove would be gone and it's much simpler to understand and closer to e.g.
> the original MS-RESOLFT widget.

The original widget (`TriggerScopePLSRWidget`, `TriggerScopePLSRMulticolorWidget`)
is a form: one labelled field per parameter (timepoints, delays, on/off/RO
pulse times, start, step size, steps), Run, Save, Load.

## How it looks

A **Simple | Expert** switch, the first item of the panel's toolbar. **Simple**
shows the same scan, read the way the original widget read.

**A scan made from a template** lists the template's own fields -- the same
labels, units and groups as *New from template → MS-RESOLFT* -- with the
scan's values. Change one and the scan is made again from the template:

```
┌────────────────────────────────────────────────────────────────────────────┐
│ MS-RESOLFT (cycles x planes)  (Ready to run on nidaq)   [ Run ]  [ Stop ]  │
│ 2 timepoints › 2 cycles › 3 planes                                         │
│ (12 steps) (96 ms) (12 frames, Camera) (exact timing, 10 µs clock)         │
│ [Simple|Expert] [New from template ▾] [Open…] [Save…] [ ] Repeat           │
└────────────────────────────────────────────────────────────────────────────┘
┌ Parameters ──────── the template's values ┐  ┌ Preview [One step|One pass|All]
│ Positions                                 │  │ [ ] Auto-update [Show preview]
│   Readout positioner          [Z      ▾]  │  │ 405 ▁▁██▁▁▁▁▁▁▁▁▁▁▁▁           │
│   Cycles                      [2      ]   │  │ 488 ▁▁▁▁▁████▁▁██▁▁            │
│   Planes per cycle            [3      ]   │  │ Cam ▁▁▁▁▁▁▁▁▁▁▁██▁▁            │
│   Position spacing (µm)       [0.25   ]   │  │ Z   ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁╱‾          │
│   First position (µm)         [1      ]   │  └──────────────────────────────┘
│ Pulses                                    │
│   Pulse sequence  Laser  On (ms) Gap after (ms)
│                   405    1       1        │
│                   488    2       1        │
│                   488    1       0        │
│ Camera                                    │
│   Camera                      [Camera ▾]  │
│   Camera exposes during pulse [3      ]   │
│ Time lapse                                │
│   Timepoints                  [2      ]   │
│   Pause before each timepoint (ms) [0 ]   │
│ Motion                                    │
│   Move time (ms)              [0.5    ]   │
│   Settle time (ms)            [0.5    ]   │
│   Move shape                  [jump   ▾]  │
└───────────────────────────────────────────┘
┌ Within one step ─────────── dwell 7 ms ───┐
│ ms 0    1    2    3    4    5    6    7   │
│ 405 ██████                                │
│ 488           ████████████    ██████      │
│ Cam                           ██████      │
│                                 //Z moves │
└───────────────────────────────────────────┘
```

*Can it run?* is not there while the scan runs as written: the header's chip
says so. It shows when the scan cannot run, or plays something differently.

**A scan built or edited by hand** lists its own values: every number and
choice the Expert parts edit, as label and field, and none of the controls
that add, remove, rename or reorder:

```
┌ Parameters ──────────────────────── the scan's values ┐
│ Edited since it was made from MS-RESOLFT (cycles x planes): its values are
│ listed. Change template values… makes it again from the template, without
│ the edits.                                            │
│ Scan order                                            │
│   Timepoints            [2 ⇕]  [forward          ▾]   │
│   Cycles                [2 ⇕]  [forward          ▾]   │
│   Planes                [3 ⇕]  [forward          ▾]   │
│ Within one step                                       │
│   Dwell (ms)            [7      ]                     │
│   405 pulse             from [0    ] to [1    ] ms    │
│   488 pulse (pulse2)    from [2    ] to [4    ] ms    │
│   488 pulse (pulse3)    from [5    ] to [6    ] ms    │
│   Camera exposure       from [5    ] to [6    ] ms    │
│ Z           6 positions from 1 to 2.25 µm, 0.25 µm apart.
│   Starts at (µm)        [1      ]                     │
│   Moves per timepoint (µm) [     ]                    │
│   Moves per cycle (µm)  [0.25   ]                     │
│   Moves per plane (µm)  [0.5    ]                     │
│   Moves                 [jumps ▾] [at the end of each step ▾]
│   Move time (ms)        [0.5    ]                     │
│   Settle time (ms)      [0.5    ]                     │
│   Line return (ms)      [1      ]                     │
│   Positions are         [absolute ▾]                  │
│   After the scan        [back to its first position ▾]│
│ Pauses and triggers                                   │
│   Before each timepoint: pause (ms)   [100  ]         │
│   Meanwhile the lasers are            [off ▾]         │
│ Recording                                             │
│   Camera                [1 ⇕] frames per step, triggered by exposure
└───────────────────────────────────────────────────────┘
```

An empty scan shows *Start from a template* (the templates and the
trajectories), without *+ Dimension*.

## Decisions

**D1. A view, not a cloak.** The Simple-scan cloaks translate a plan of their
own to a backend's dicts (`ScanCloak.to_backend` / `from_backend`, a mirror
timer, a confirm-discard when the Advanced page holds what the simple page
cannot show). The Expert panel's state is the scan itself, so its simple view
is a second way to show and edit the same program: no second state, no
translation, nothing to discard on the way back. `SCAN_CLOAKS`, `scan.cloak`
and `resolve_scan_frontend` are unchanged.

**D2. The switch.** Two checkable buttons, **Simple** and **Expert**, the
first item of the header's toolbar, as the cloaks' *Simple | Advanced* pair:
first, so it stays under the cursor when the view hides or shows the buttons
after it (the toolbar is a flow layout). Always there: no setup key. The view
is kept in the panel's state and restored at startup
(`ComponentStateApplyMode.STARTUP_RESTORE`, which an imported states bundle
also uses); opening a scan file or a setup-mode switch (`SETUP_MODE_APPLY`)
never changes it, so a file saved by someone else does not change the panel
under the user. A panel without a saved state opens in **Expert** (today's
look: nobody's panel changes until they switch). Switching is allowed during
a run: it is the same scan; Run, Stop and the progress are in the header of
both.

**D3. What each view shows.**

| Part | Expert view | Simple view |
|---|---|---|
| Header: title, status chip, Run, Stop, dimensions, notice, facts, engine line, progress | as today | the same |
| *Change template values…* | as today | hidden while Parameters is the template's form (it is that form); otherwise as today (the way back to the template for an edited scan) |
| *New from template ▾*, *Open…*, *Save…*, *Repeat* | as today | the same: a template is how a simple user picks a scan |
| *Undo*, *Redo*, *Tables* | as today | hidden, and their shortcuts (Ctrl+Z, Ctrl+Shift+Z) off: no hidden behaviour; Tables closes |
| *Start from a template* (empty scan) | templates, trajectories, *+ Dimension* | templates and trajectories |
| **Parameters** (new) | -- | for a scan (D4) |
| *Scan order*, *Positions*, *Pauses and triggers*, *Recording* | as today | hidden (their values are in Parameters) |
| *Within one step* | edited: drag, resize, nudge, Delete; *+ Pulse*, *+ Exposure*, *+ Power level*, *+ Move*; the inspector | the drawing only, when the scan has events: no add buttons, no inspector, no hint, no drag, no keys, no selection; the dwell as text; notes as text, without their action button; the phase box stays (a view, like the preview's zoom) |
| *Path* (a trajectory) | as today | hidden while Parameters is its template's form; otherwise as today |
| *Can it run?* | as today | shown while a scan (not an empty one) cannot run for a reason other than the form's refused value (that one is under the form and in the header already), or plays something differently (the header's amber chip); otherwise hidden -- the status chip is the verdict, and clicking it (or the amber chip) shows the card until the view is switched |
| *Preview* | as today | the same: *Show preview*, *Auto-update*, the zoom |

**D4. Which parameters.** The ones the scan is made from.

- **The template's** when the scan is its template's output: a template and
  values -- the ones its `requested_as` names, else the ones the scan was
  last made from (D4b) -- make it, compared without `requested_as`
  (`dataclasses.replace(made, requested_as=None) ==
  dataclasses.replace(program, requested_as=None)`). Parameters is then the
  template's form, inline, holding the values `make` recorded
  (`made.requested_as['params']`: a field the template gained after the scan
  was saved, such as the raster's *Move shape*, is there with what it meant
  before, `Field.absent`). The comparison is cheap and pure (`make` builds
  and validates; it compiles nothing and never calls a trajectory's path, so
  no script runs). It also catches a file whose parameters no longer make its
  scan -- edited by hand, or written by an older template -- which is then
  listed by its own values, never silently made again.
- **D4b. Back to the template's.** The controller keeps the template and
  values the scan was last made from (beside `_titleBase`, set where it is
  set). When the tables are read back after an edit and they make that
  template's output -- an edit undone, a value typed back -- the program is
  that output again, `requested_as` and all: the title is the template's, not
  *…, edited*, and Parameters is its form again.
- **The scan's own** otherwise: built by hand, edited in the Expert view (an
  edit drops `requested_as`, as today), an Advanced file. A scan edited since
  it was made from a template says so above its values (*Edited since it was
  made from MS-RESOLFT (cycles x planes): its values are listed. Change
  template values… makes it again from the template, without the edits.*),
  and *Change template values…* is in the header.
- A trajectory that is not its template's (edited in Tables) has no own-values
  listing; its *Path* card shows, as in the Expert view.

**D5. Editing the template's form.** Each committed field -- Return or leaving
a field that was typed in, a choice picked, a box ticked, a pulse cell
edited -- makes the scan again from the template with the form's values, as
the template dialog's OK does: the title stays the template's, the values are
remembered for the template (`templateValues`), Undo starts again.

- **Only what was changed changes.** The form's values are the values it was
  given, except where the user changed a field: a field whose text is still
  what the form showed gives back its value exactly, never read again from
  its text. And every number shows exactly (D8), so what is shown is what
  runs.
- **A refused value** (not a number, *Cycles* 0, an exposure pulse past the
  sequence) leaves the scan as it was and makes it **not runnable**: the
  reason shows under the form and in the header (*Cannot run*), and Run, the
  preview and the check refuse with it; *Save…* refuses too, with the reason,
  rather than write the old scan silently. Run never runs the old values
  behind a form that shows new ones.
- The form keeps what was typed until a value is accepted, or the view is
  switched to Expert, which drops the refused value (back in Simple, the form
  shows the scan's values). **A scan from anywhere else** -- *New from
  template*, *Change template values…*, *Open…*, a restore, a script --
  replaces the form whole, the field being typed in too: what it showed was
  for the scan it replaced.

**D5a. A value typed but not yet left is committed before Run.** Under the
macOS style a push button or check box takes no focus when clicked
(`TabFocus`), and `click()` moves none, so the field being typed in never
sees `editingFinished` and Run would run the scan without it -- in the Expert
parts too, today. *Run*, *Show preview* and *Save…* first commit the field
being typed in: on their `pressed`, which comes before `clicked`, the pressed
button takes the focus, which ends the field's editing as leaving it would.
(Clearing the field's focus instead is wrong: where buttons take the focus on
a press -- Fusion, Windows, the rigs -- the focus is the button's by then,
and losing it releases the button, so the click never comes; probed.) Both
views.

**D6. The form's fields, inline.** Every field kind of the template dialog,
with its labels, units, groups and tooltips:

- `positioner`, `laser`, `detector`, `optional-laser`, `choice`: a choice box;
  `flag`: a check box; `count`, `number`, `seconds`, `micrometres`, `pair`: a
  field (seconds shown in ms);
- `pulses`: the pulse table, every cell editable (laser, on, gap after), with
  **no Add, Remove, Up or Down**: how many pulses there are is the scan's
  structure. To change it: *New from template* → the same template opens its
  full form with the scan's values, Add and Remove (as today,
  `makeFromPreset`), and makes a new scan -- a form for a new scan, not the
  listing, so outside "nothing adds";
- `points` and `code` (trajectories): a one-line summary (*12 points*, *a
  Python path, 14 lines*) and **Edit…**, which opens the template's full form:
  a multi-line text has no natural commit, and code runs whenever the scan is
  checked, so it is approved in the form when OK is pressed, as today. Their
  values are the form's given values, always.

Nothing is filled in from the rig inline -- the values are the scan's; the
dialog keeps doing that for a new scan.

**D7. The scan's own values.** Every value the Expert parts edit, as label and
field, in the panel's words (§7.12 of the Expert plan), in sections in the
order the scan happens; a section with nothing in it is left out:

- **Scan order**: each dimension (slowest first) by its kind's plural, its id
  added when two share a kind: its count, its order (*forward*, *backward*,
  *serpentine* -- not for the slowest; a serpentine alternates with the
  dimension right outside it, as the Expert view sets it).
- **Within one step**: the dwell; each event by its device and kind
  (*405 pulse*, *Camera exposure*, *AOM power*, *Z move*) -- when two share
  both, its id added, or its number among them in timeline order when they
  have no ids (events added by hand have none) -- and where it plays as a note
  (*last of each cycle*, *phase 1*): from, to; and value and shape for a
  power level or a move.
- **Positions**, one block per positioner under its name, with its range as a
  note: starts at; **moves per** each dimension -- every dimension, the field
  blank where it does not move it (the Expert sentence's weight: blank = does
  not move it); or its table of positions; how it moves (*jumps*, *moves
  linearly*, *moves smoothly*; *at the end of each step*, *between steps*);
  move time; settle time; line return; *positions are* (absolute, relative to
  where it starts); *after the scan*.
- **Pauses and triggers**, by where they are (*Before each timepoint*): a
  pause's length; for a trigger, the shortest time between triggers, *give up
  after* and what then; what the lasers do meanwhile.
- **Recording**: a camera's frames per step, with what triggers it and its
  phases as text; a point detector as text (*integrates each whole step into
  one pixel*).

**Labels, not fields**: whatever makes the structure -- a dimension's kind and
id, a positioner's device, an event's device, kind, anchor, phases, id and
whether it waits for the stage, a pause's place and kind, a detector, what
triggers it and its phases. **Nothing adds, removes, renames or reorders.**
Every field writes the panel's tables exactly as the Expert parts do (one
state; a value that does not read is reported as it is there). A *moves per*
field is a value like any other: blank or a number, its row always there.

**D7a. The range note says when positions repeat.** `driveRange` counts the
steps that move a positioner (the product of their counts) and calls them
positions. After a count is changed by hand -- *Cycles* 2 → 3 with *moves per
plane* still 0.5 µm in an MS-RESOLFT scan -- 9 steps visit 7 positions, two
of them twice, and the note said *9 positions*. When positions repeat it
says so, counting the visits (not the header's steps, which also count the
dimensions that do not move it): *7 positions from 1 to 2.5 µm, 0.25 µm
apart; 2 of them visited twice.*, *…; 1 of them visited 3 times.* (both views:
the Expert Positions card reads the same note). Positions are compared
exactly: each the decimal sum of the offset and the weights as written
(`Decimal(repr(...))`, as `exact_sum`), so 0.3 and 0.1 + 0.2 are one position
-- as floats they were two, and the old spacing test needed a rounding to 9
digits (`round(b - a, 9)`), which goes.

**D8. No new numbers.** Everything shown is the scan's or its template's;
nothing is defaulted, rounded or guessed. The template's fields show their
values exactly -- the shortest decimal that reads back as the same float
(`repr`), seconds shown in ms as that decimal × 1000, which `stored()` reads
back to the same float -- in the inline form and in the dialog, the pulse
table's on and gap columns included, replacing the 6- and 12-digit displays
(`:g`, `.12g`), which rounded a value with more digits on its way through the
form (an edited pulse cell re-reads its whole table). Widths are the parts'
own (in characters, `_field`).

## Implementation

### Files

| File | What |
|---|---|
| `imswitch/imcontrol/view/widgets/ScanWidgetExpert.py` | `exact_text()` beside `stored()`; `_RowTable` quiet rows and `setRowButtonsVisible()`; `_preset_editor()` and `_preset_params()` extracted from `PresetDialog._editor` / `params` (the dialog's layout and attributes unchanged; its numbers exact, its messages naming the field); `TemplateForm`; the switch, `view()`, `setView()`, `sigViewChanged`; `commitEditing()`; visibility in `refreshParts` and `_refreshHeader`; `setTemplate()`, `template()`, `templateParams()`, `sigTemplateEdited`; public `mode()`; `setLoopOrder()` (P2) |
| `imswitch/imcontrol/view/widgets/_expert_scan_parts.py` | `StepCard.setReadOnly()`, `StepCanvas.setEditable()`, `StartCard.setBuildVisible()`, `AdaptiveColumns.rearrange()`; `_LoopChip` calls `panel.setLoopOrder` (P2) |
| `imswitch/imcontrol/view/widgets/_expert_simple_view.py` (new) | `ParametersCard`; `ScanValuesForm` (P2) |
| `imswitch/imcontrol/controller/controllers/ScanControllerExpert.py` | `_madeFrom`, `_templateOf()`, `_reattach()`, `_templateProblem`, `_onTemplateEdited()`, `_onViewChanged()`, `saveScan()`; `setTemplate` in `setProgram` and `updateReport`; the refusal in `_compile`; `view` in the state |
| tests, docs | below |

### The widget

- `exact_text(value, scale=1.0)`: a stored value as shown -- `repr(float)`'s
  decimal times `scale`, exactly (`Decimal`), without an exponent or trailing
  zeros: the inverse of `stored()` (`stored(exact_text(v, s), s) == v` for
  every finite float, at the panel's scales, 1 and `MS`).
- `_RowTable.addRow` restores the quiet state it found instead of clearing
  it: `setRows` and `setColumns` emit no `sigEdited` per row (they did: addRow
  set `_quiet` back to False). A lone `addRow` (the Add button) still emits.
- `_Column(..., exact=False)`: an exact column shows its values with
  `exact_text` (the pulse table's on and gap); every other column as today.
- `VIEW_SIMPLE = 'simple'`, `VIEW_EXPERT = 'expert'`, `VIEWS`. `simpleViewButton`,
  `expertViewButton`: checkable `QToolButton`s, exclusive, object names
  `expertViewSimple`, `expertViewExpert`, together (one `_group`) as the first
  item of the toolbar flow.
- `view()`; `setView(view)` (unknown → `ValueError`): applies D3; on the way
  into Simple closes *Tables* and invalidates the Parameters card; turns the
  undo/redo shortcuts off in Simple, on in Expert; clears a check shown on
  request; refreshes the parts; `self.columns.rearrange()`; then emits
  `sigViewChanged(view)` when the view changed -- after the parts it hides
  were hidden, so a field that commits as it is hidden has committed.
- `commitEditing(button)`: on `pressed` of `scanButton`, `plotButton`,
  `saveScanBtn`, the button takes the focus (`setFocus(Qt.OtherFocusReason)`),
  which commits the field that had it as leaving it does; a button that has
  the focus already (a press where buttons take it, Space) is left as it is.
  Never `clearFocus()`: that releases the pressed button and its click is
  lost.
- `setTemplate(preset, params=None, problem='')`: the template the scan is
  made from (`preset` `None`: none), its values, and the refusal of the form's
  last value (`''`: none); `template()` → `(preset, params, problem)` or
  `None`. `sigTemplateEdited(preset_id)` when the inline form commits;
  `templateParams()` → the form's values (`ValueError` naming the field).
- `programShown()`: a new scan is shown (the controller's `setProgram` calls
  it, before it writes the tables): the Parameters card replaces its content
  (below). Not from `setProgramDict`, which also runs when shared attributes
  are written back (the base's `attrChanged` → `setParameters`): that is no
  new scan.
- `refreshParts()` and `_refreshHeader()` apply D3 for the current view; the
  Expert view is unchanged.
- `_showCheck()` in Simple shows the check card until the view is switched.

### The template's form (`TemplateForm`)

- `TemplateForm(preset, devices, values, parent=None)`: a grouped
  `QFormLayout` (group titles `expertCardTitle`, as the dialog), editors by
  `_preset_editor(field, value, devices, inline=True)` (D6), wrapping its rows
  when the dock is narrow (`WrapLongRows`: no sideways scroll, §7.12).
- **Given values.** Per field the form keeps a pair: the given value and its
  exact display (`exact_text`; the pulse table's rows; the choice's text; the
  box's state) -- set together, never from what the editor happens to show.
  `params()`: for a field whose editor still shows that display (always for
  points and code), the given value; otherwise the editor read
  (`_preset_params`).
- `sigCommitted` from every editor (a typed field on `_on_commit`, a choice
  `currentIndexChanged`, a box `toggled`, the pulse table `sigEdited`), emitted
  with `self._committing` held; `isCommitting()`. `sigEditRequested` from a
  points or code field's *Edit…*.
- `setValues(values)` writes nothing while committing (the controller's
  re-make runs inside the commit: the field just committed holds the value);
  otherwise, per field: the field that has focus keeps its pair and its text;
  every other gets the new pair and, where it shows something else, the
  display, written with its signals blocked (the pulse table's too: a
  `QSignalBlocker`).
- `setProblem(text)`: the red line under the form (`expertProblems`); `''`
  hides it. Written while committing too.

### The Parameters card (`_expert_simple_view.ParametersCard`)

- A `Card` titled *Parameters*, its header saying *the template's values* or
  *the scan's values*; in the left column of `AdaptiveColumns`, after the
  start and path cards. Built with the form class it hosts
  (`ParametersCard(panel, TemplateForm)`), so the new module imports nothing
  from `ScanWidgetExpert`.
- `refresh()`: the template's form when `panel.template()` names one -- built
  again when there is none, its template differs, or it was invalidated;
  otherwise `setProblem(problem)`, and `setValues(params)` unless the form
  holds a refused value -- else, for a scan (`panel.mode() == 'program'`), the
  scan's values (P2); else nothing.
- **What it shows changes under no one's hands.** When what the card should
  show changes by itself -- the scan's values becoming its template's form
  after a value typed back (D4b), or the other way -- while a field of the
  card has the focus, the change waits until the focus leaves the card
  (`QApplication.focusChanged`) or the view is entered again; until then the
  content shown stays live. Leaving a field commits it before
  `focusChanged` comes, and that edit is read back only by the next report:
  so when an edit is waiting for its report (the panel's `_edited()` since
  the last `setCheck`), the change is decided by the refresh that report
  brings, not at `focusChanged` (which would show the template's form for a
  report period and flip back).
- `invalidate()` (entering Simple). `programShown()` (a new scan): marks the
  content to be replaced, the field being typed in too (D5), unless it is
  the template's form committing -- its own commit is what made the scan.
  It only marks: the refresh inside `setProgramDict`, after the tables are
  written, builds the new content.
- Whatever the card removes is **disconnected first** (so a field that
  commits as it goes -- focus-out on hiding -- writes nowhere), then hidden
  and `deleteLater`; never deleted synchronously.
- **A commit never rebuilds what committed it.** The form's commit runs the
  controller's re-make synchronously inside the editor's own signal
  (`editingFinished`, `currentIndexChanged`, the pulse table's `itemChanged`
  and the laser box's `currentIndexChanged`), which ends in `setProgramDict` →
  `refreshParts` → this card: so the committing form is not rebuilt, and
  writes nothing.

### The parts

- `StepCard.setReadOnly(on)` (D3) and `StepCanvas.setEditable(on)`: presses,
  moves and keys edit nothing and select nothing, no resize or hand cursor, no
  selection drawn. `StepCard.refresh()` -- which runs after every report --
  honours it for all five: the hint stays hidden, the notes are drawn without
  their buttons, nothing is selected, the inspector stays hidden, the dwell
  is text (its field hidden).
- `StartCard.setBuildVisible(on)`: the *Or build it yourself* row.
- `AdaptiveColumns.rearrange()`: what `resizeEvent` does, at the current width
  (hidden cards change the threshold).

### The controller

- `_madeFrom`: `(template id, params)` of the scan last made from a
  template, set in `setProgram` beside `_titleBase` (`None` for a scan not
  made from one).
- `_templateOf(program)` → `(preset, params, made)` or `None` (D4): the
  candidates are the program's own `requested_as`, then `_madeFrom`; a
  candidate that raises while making is skipped.
- `_reattach(program)`: `made` when `_templateOf` matches, else `program`;
  `getParameters()` applies it to a program read back from the tables (D4b).
- `setProgram()`: `self._edited = False` (it rewrites the tables: a pending
  edit of the old ones is moot -- without this, a table edit younger than
  the report's settle time made the new scan *edited*), `_templateProblem =
  ''`, `_madeFrom`, then `self._widget.setTemplate(...)` and
  `self._widget.programShown()` **before** `setParameters()`, so the refresh
  inside `setProgramDict` already sees the new template and content.
- `updateReport()`, right after `getParameters()` and before anything can
  return: `self._widget.setTemplate(...)` with the scan's template, its values
  and `_templateProblem`.
- `_compile()`: with `_templateProblem` set, `ScanDesignRefusedError` with it,
  right after `getParameters()` -- before `_capturedPositions` (which updates
  `_unknownPositions`) and the compile cache. The check, Run
  (`_buildScanSignals`, Repeat's continuation through `runScanAdvanced`) and
  the preview (`plotProgram`) all refuse with the form's reason.
- `_onTemplateEdited(preset_id)`: `templateParams()` and `presets.make`; a
  refusal (`PresetError`, `ProgramError`, `ValueError`, `TypeError`, as
  `makeFromPreset`'s validate) sets `_templateProblem` and `updateReport()`;
  otherwise `_templateProblem = ''`, the values remembered in
  `_templateValues[preset_id]`, `setProgram(program)`.
- `_onViewChanged(view)`: into Simple, `flushRefresh()` and `updateReport()`
  (a pending Expert edit is read first, so the template is current); out of
  Simple with a problem: cleared, and reported.
- `saveScan()`: with `_templateProblem` set, *Not saved: <the reason>* under
  the header (`showNotice`) and nothing saved; otherwise as the base.
- `makeFromPreset()` (*Change template values…*, *New from template*) opens
  the form with the values the scan was made from: its own `requested_as`,
  else `_madeFrom`'s when that is the template asked for, else the last
  values used for the template (today it skipped `_madeFrom`, so after
  *Open…* an edited scan's button offered another scan's values, against its
  tooltip).
- `_title()`: a template's title only for the template's output
  (`_templateOf`); a scan whose `requested_as` names a template that no
  longer makes it is *<title>, edited*, as an edited one.
- The empty scan's reason in Simple has no *add a dimension*, which Simple
  cannot do: *The scan has no dimensions yet: start from a template.*
  (the Expert view's text is unchanged); the view switch updates it.
- `getComponentState()` adds `'view': self._widget.view()`.
  `applyComponentState()`, for `STARTUP_RESTORE` (and `None`, the persistence
  layer's default), right after the running and dictionary checks, before
  the program is checked: a `view` in `VIEWS` is applied (a scan that does not
  apply still opens in the view it was left in); a missing one keeps the
  current view; any other value is a warning (`RestoreWarning`, not critical)
  and changes nothing -- never an exception, which would drop the whole
  state -- carried into every list the method returns after it (its later
  refusals return lists of their own). `SETUP_MODE_APPLY` leaves the view
  alone.

## Tests

New `imswitch/imcontrol/_test/unit/test_scan_widget_expert_simple_view.py`, on
`ExpertRig` (`test_scan_controller_expert.py`). Stub `askPresetParams` and
`askApproveScript` (and use `set_approvals(store)`, as
`test_scan_script_approval.py`) before anything that can open a modal: Run and
Show preview on a trajectory script call `_approveScript`.

P1:

1. The switch: Expert by default; Simple hides the parts D3 hides and shows
   Parameters, the drawing and *Preview*; *Tables* is closed and hidden; the
   shortcuts are off; the switch is at the same place in both views (shown
   widget); back in Expert every part is as it was, editable again.
2. A template's scan lists the template's fields, labels and groups as the
   dialog, with the scan's values shown exactly (seconds in ms); *Change
   template values…* is hidden.
3. Typing *Cycles* 3 and committing makes the scan again: `program ==
   presets.make('ms-resolft', ...)` with cycles 3, the title still the
   template's, the subtitle *2 timepoints › 3 cycles › 3 planes*,
   `_templateValues` updated, still the template's form.
4. A refused value (*Cycles* `x`, then `0`): the reason under the form and in
   the header, *Cannot run*, `runScanAdvanced` refused with it (no run), the
   preview refuses, *Save…* refuses (stub the file dialog to fail the test if
   it is reached), the program unchanged, the typed value still shown; a valid
   value clears it; switching to Expert drops it and the form shows the
   scan's values again; a commit made by the switch itself (a modified field
   hidden) leaves no problem in Expert.
5. The pulse table has no Add, Remove, Up or Down; editing an on time makes
   the scan again with it; a laser picked in the table makes it again with
   that laser, the table not reset under its own signal.
6. A trajectory's code or points field is a summary with *Edit…*, which asks
   for the template's form (stubbed); its other fields are inline; its points
   come back exactly.
7. The template's form only for the template's output: an MS-RESOLFT scan
   edited in the Expert view is not (P1: Parameters shows nothing); an edit
   undone makes it the template's again (title, form, `requested_as`); a file
   whose `requested_as` does not make its scan is not, and is titled
   *…, edited*; *Change template values…* on an edited scan opened from a
   file offers the values it was made from. A template's scan
   through the JSON round trip (`getComponentState` → `applyComponentState`)
   is still its template's, for every built-in template; a raster saved
   before *Move shape* existed (no `transition` in its parameters) is its
   template's, the form showing *Move shape* with what it meant.
8. **Exactness.** Values with more digits than the old displays kept (a pair
   `[1234.5678, 987.654321]`, a pause of 1/30 s, a point `1.2345678`, a pulse
   on for 1/3000 s beside the one edited): every
   untouched field gives back its value exactly when another one is committed
   (`program == make(..., only the committed field changed)`); `exact_text`
   reads back through `stored` exactly (a sweep of floats, seconds and µm).
9. **Pending edits and outside scans.** An Expert edit younger than the
   report's settle time, then a template commit (or `setProgram(make(...))`):
   `requested_as` kept, the title not *edited*. A same-template scan arriving
   from outside (`makeFromPreset` with the dialog stubbed, a file load) while
   a focused field holds a refused value: the form shows the new scan's values
   and no problem remains.
10. The drawing in Simple, after a report (`updateReport()`): a drag on a bar,
    Delete and the arrow keys change nothing; nothing is selected; no hint, no
    inspector, no note button; the dwell is text.
11. **The view in the state**: saved; restored at startup; not changed by a
    scan file (`SETUP_MODE_APPLY`); a missing `view` keeps the current one; an
    unknown one is a warning and the program still applies; a state with
    `view` opens in the Advanced panel as before when it is an unchanged
    Advanced scan.
12. **Run and the typed field**: text typed into a template field without
    Return, then Run -- a real press on the shown panel (`QTest.mouseClick`),
    `click()`, and Space on the focused button, under the Fusion style and with
    the button's focus policy `TabFocus` (macOS): the field is committed, the
    click comes, the run uses the typed value (or is refused with its reason);
    the same for *Show preview* and *Save…*. From the Simple view on the mock rig:
    Run ends, Stop stops, *Show preview* draws; switching views during a run
    changes nothing about the run.
13. The wheel over a field of the form scrolls the panel, not the field.
14. *Can it run?* in Simple: hidden for a scan that runs as written, shown for
    one that cannot run (a reason other than the form's) or plays something
    differently, shown on a click of the status chip until the view changes;
    `_RowTable.setRows` emits no `sigEdited` per row, a lone `addRow` one.

P2:

15. The scan's own values: every row D7 lists for the edited MS-RESOLFT scan
    and for a raster (two positioners, a serpentine X), with their values; the
    *Edited since…* line; a count, a weight, an event time, a pause and a
    frames-per-step typed there land in the tables (the same values the
    Expert parts then show); serpentine on the second dimension alternates
    with the first; the listing holds no button.
16. A cleared *moves per* field keeps its row, blank, and means "does not
    move it"; typed again, it moves it again.
17. The range note: *7 positions from 1 to 2.5 µm, 0.25 µm apart; 2 of them
    visited twice.* after *Cycles* 2 → 3 by hand; the unedited scan's note as
    before.

The existing Expert tests pass unchanged. Lanes: unit, core, `python -m
sphinx -b html -W docs OUT`.

## Docs

- `docs/expert-scanning.rst`: a *Simple view* section after *The panel*: the
  switch, what it lists (the template's form or the scan's own values), what
  is not there, a refused value, values typed and Run, the remembered view.
- `docs/changelog.rst`: an entry under *Unreleased → New Features*.
- `docs/gui.rst`: the switch, in the line on the Expert panel.
- `docs/design/plans/scan-engines-rig-test-plan.md`: rows V1-V4 (below).
- This plan: an *As built* section.

## Phases (one branch, a commit each)

- **P1**: D1-D6, D5a, D8 -- `exact_text`, the quiet `_RowTable`, the switch
  and the state, the views (D3) with the read-only drawing and start card and
  the check's rule, the template's form inline and the controller path (D4,
  D4b, D5, D6), tests 1-14 (the scan's own values not yet: the Parameters
  card shows nothing for a scan that is not its template's).
- **P2**: D7, D7a -- `ScanValuesForm`, `setLoopOrder`, the *Edited since…*
  line, the range note, tests 15-17; docs, changelog, rig rows.

Each phase: implemented by an agent from a brief, reviewed and run by me;
after P2 an independent review of the whole.

## Rig rows (V, for `scan-engines-rig-test-plan.md`)

- **V1** MS-RESOLFT from the Simple view: change cycles, planes, a pulse's on
  time and the pause; Run. The scope shows the new pulse times; the recording
  opens with the new cycles and planes in SNOUTY.
- **V2** Restart ImSwitch: the panel opens in the view it was left in;
  opening a scan file saved in the other view keeps it.
- **V3** The preview of the Simple view is the Expert view's (same scan, same
  drawing), and a refused value refuses Run with the reason.
- **V4** Someone who has not used the Expert panel runs an MS-RESOLFT scan
  from the Simple view without help (the request came from users): what they
  had to ask is the result.

## As built (2026-10-25)

P1 is `faa187b3a`, P2 the commit after it. What differs from this draft, or
was not foreseen, found while building:

- **`_reattach` runs in `updateReport()`**, after `getParameters()`, not inside
  it (D4b): an existing test pins that `getParameters()` returns the tables as
  they read. A value typed back in the listing therefore makes the scan its
  template's output again at the next report, and the card turns into the
  template's form under the waiting rule, not at the keystroke.
- **The two cards are not symmetric in Expert.** The template's form is built
  and kept current while hidden in Expert (the card refreshes in both views);
  the scan's own values are built in Simple only (`listsValues()`), so editing
  in Expert pays nothing for a listing nobody sees, and entering Simple builds it.
- **The note of an event** (*last of each cycle*, *phase 1*) is the drawing's
  `tag` without the value it opens with for a power level or a move: the value
  is a field of the listing. `stepContent()` gives each event a `where`, the
  tag's other half; the drawing's tags are as they were.
- **A listing the card keeps while the user is in it writes nowhere once the
  tables have another structure** (its rows would be other rows: a count
  could land on another dimension). The card keeps a different structure off
  until the focus leaves, as D4b says, so this is a guard, not a feature:
  every field writes through one place that checks it, which is also how a
  listing taken away is cut off (`detach()`) instead of disconnecting every
  field one by one.
- **A count that reads but is not a count** (0) is red with the table's text,
  as one that does not read is; the Expert block's box clamps it to 1 without
  a word.
- **Wording the mockup did not settle.** A trigger is a line (*Before each
  timepoint: wait for a trigger*) over its three rows; a camera says *frames
  per step* as the mockup does, whatever the count; a point detector's phases
  are text when set; the red text of an unusual window or assembly says *set in
  Tables, in the Expert view*, Tables not being in Simple.
- **The range note, exactly** (D7a): sums are exact for any float (made in
  whole units of the finest decimal place written, since the final review:
  integers, as quick as the float sums were), a
  position that is the only one says
  *Stays at* (a table of equal entries said *3 positions from 1 to 1 µm*), and
  values that do not read, or no positions, say nothing.
- **The inline pulse table's blank strip** (left from P1) is Qt 5.15's: a scroll
  area's size hint adds the room of a horizontal scroll bar it has not shown,
  19 px here. The table is capped at its header, its rows and its frame; the
  cap follows `setRows`.
- **Two P1 assertions changed**, the ones that said the card shows nothing for a
  scan that is not its template's output (the edited-scan test, the card-waits
  test): P2 makes the card list its own values there.

Found while building, fixed after it (`101ee79c7`): `scan_program.program.validate`
raised `TypeError` (`len(None)`) for a positioner that follows a table whose
values are blank, on the line after the one that adds *a table needs both
table_loop and table_um*. The Tables view could always do this; the listing's
*Positions over …* field made it one keystroke away, and the report raised in
the Qt event loop instead of refusing the scan. The length is now checked only
when both are there.

## Final review (2026-10-25)

An independent reviewer of the whole (P1, P2, the fix; probes in
`/private/tmp/claude-501/expert-simple-final-review/`): fix first. Taken:

- **An Expert edit lost on the way into Simple** (blocking). A field typed in
  but not left, a Tables cell open, a count typed, or a value committed less
  than a report period before **Simple** was clicked: the card was drawn from
  the template as it was before the edit, the field, hidden while focused,
  handed the focus into it, and the card held on to that stale form as if the
  user were working in it. The next commit there made the scan again from the
  template, the edit gone without a word. Now the view's own button takes the
  focus first (`commitEditing`, as Run does), so the field commits before
  anything is hidden and the focus stays on the switch; and the report on
  entering Simple decides what the card shows whatever has the focus
  (`decideNow()`, which rebuilds nothing that is right: forcing a rebuild
  there built the listing twice and broke its tab order).
- **The inline pulse table squeezed in a narrow dock**: beside the widest label
  it got less than its columns, scrolled and hid part of its last pulse. It
  now takes the form's whole width, on its own row under its label. And the
  page of tables, never shown in Simple, set the panel's minimum width (a
  stacked widget's minimum is its widest page's): only the page shown asks for
  room now, so the Simple view fits a dock down to its own parts' width.
- A refused value left a drawn preview unmarked → *out of date*.
- The range note took six times as long as the float code on a positioner moved
  by two dimensions of hundreds of counts → integer sums, exact and as quick.
- A negative or not-a-number pause made a scan without a pause while the form
  showed it (and a not-a-number pulse time passed the pulse check) → refused
  by the templates.
- Coming back to Expert selected the first event → the event the inspector was
  on.
- A script's save (`saveScanParamsToFile`) wrote the old scan behind a refused
  value → refused, logged, still exported to the API.

Not taken: naming the listing's field widths (6, 5, 4 characters) once for
both the listing and the Expert parts -- they are the parts' own widths,
mirrored (D8), and naming them would touch every Expert part for no change.

The reviewer then checked the fixes (`71400621a`): **ship**, with two small
points, both taken: the switch takes the focus only from a field of the panel
(a restore's switch took it from a field elsewhere in the window, committing
it), and the inline pulse table is at least as wide as its columns, so below
about 365 px the panel scrolls sideways with every pulse in view instead of
the table hiding part of its last row.

## Review round 1 (2026-10-25)

Two reviewers, one on the request and the user, one on the code (probes in
`/private/tmp/claude-501/expert-simple-review/`). Both: changes needed. Taken,
all of it:

- A cleared *moves per* field removed a dimension from a positioner, a row
  the listing could not bring back → every dimension listed, blank = does not
  move it (D7).
- Every commit re-made the scan from every field's text, and the shared
  editors showed 6 or 12 digits: committing one field rounded the others
  (probed: a Lissajous frequency, a 1/30 s pause, a point) → given values for
  untouched fields, exact display (D5, D8).
- An edit undone left the scan *edited* for good → back to the template's
  (D4b). A raster saved before *Move shape* failed the equality → compared
  without `requested_as`, the form holding what `make` recorded (D4).
- A pending table edit made the next template scan *edited* → `setProgram`
  drops it; entering Simple reads it first.
- The first refresh after a commit ran with the old template, writing old
  values into the field just committed by Tab and into the pulse table inside
  its own signal; `_RowTable.setRows` was not quiet → `setTemplate` before
  `setParameters`, a committing form writes nothing, quiet rows, blocked
  signals.
- A same-template scan from outside kept a refused value shown with no
  problem left → a scan from outside replaces the form.
- macOS buttons take no focus: a value typed without Return was not run, in
  the Expert parts too → commit on `pressed` (D5a).
- A bad `view` in a saved state would have dropped the whole state → only a
  known view applies; a warning otherwise.
- The read-only drawing came back to life on every report → its refresh
  honours read-only.
- *Can it run?* repeated the header in expert words when all was well, and
  the mockup's *Runs exactly as written* is not what the NI-DAQ reports →
  shown only when there is something to know.
- The switch moved under the cursor (flow layout) → first in the toolbar.
  Hidden Undo with live Ctrl+Z → shortcuts off. Save during a refused value →
  refused. The range note counted repeated positions → D7a. Columns did not
  re-arrange on a view change → `rearrange()`. Wording: *Gap after (ms)*,
  ordinals for events without ids, detector phases as text, the phase box
  kept, the way to add a pulse said. A rig row for a first-time user (V4).

Not taken: nothing.

Round 2 (draft 2): the product reviewer GREEN, with five small points, all
taken: the range note counts visits, not the header's steps (D7a); a refused
Save says *Not saved: …*; an empty scan in Simple shows no check card and
its reason offers no *+ Dimension*; *Change template values…* opens with the
values the scan was made from (`_madeFrom`); a file whose template no longer
makes it is titled *…, edited*.

Round 2, the code reviewer: changes needed, all taken (draft 3). D5a's
contract cleared the field's focus on `pressed`, which on Fusion and Windows
-- where the button has the focus by then -- released the button: the click,
and so the run, never came (probed, six cases) → the pressed button takes the
focus. The pulse table re-read its 12-digit cells → exact columns. A content
change under a focused field of the card (P2: the listing becoming the form
after a value typed back) → waits for the focus to leave; everything removed
is disconnected first. Given values kept as pairs. `programShown()` from
`setProgram`, not `setProgramDict` (shared attributes call that too). The view
warning survives later refusals. Two notes under *Found, not changed*.

Round 3, the code reviewer: GREEN, with three points, all taken: the range
note compares positions exactly (decimal sums; float sums split 0.3 from
0.1 + 0.2) and words any number of visits (D7a); a change of the card's
content waits for the report of an edit made by leaving a field, not for
`focusChanged`; `programShown()` only marks, the refresh after the tables are
written builds. **Both reviewers: green -- implementation starts.**

## Found, not changed

- The Expert tables show floats with 12 significant digits (`_RowTable._show`,
  `ms_text`) and every edit reads the whole scan back from them, so a value
  with more digits than that is rounded by an edit elsewhere. It is
  deliberate where it is: a dragged event's time is a snapped float whose
  noise the 12 digits drop, which strict timing would otherwise refuse as off
  its grid. The template's form does not depend on it (given values, D5);
  the scan's own values (D7) write the tables and share it. So does D4b: an
  edit undone reads the tables back, and a value with more than 12 digits (a
  1/30 s pause) comes back rounded, so that scan stays *edited* (probed; the
  rig's MS-RESOLFT, a serpentine raster, a pulse sequence and a circle come
  back exact).
- The template dialog fills an empty first position from the positioner's
  tracked position with 6 digits (`_followPosition`, `.6g`). It is a
  suggestion shown in the field before OK, not a value re-read behind the
  user's back; not touched here. The inline form fills nothing from the rig.

## Not built

- A setup key to start in, or lock, a view (a facility mode). The view is
  remembered per computer; a key is a small addition if a facility wants one.
- Adding or removing anything in the Simple view -- by design.
- A simple view for the other scan panels: those have the cloaks.
