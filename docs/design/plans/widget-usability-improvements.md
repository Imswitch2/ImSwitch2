# Widget Usability Improvements

## Goal

Make ImControl widgets usable on small screens and reduce fragile widget-level
UI behavior. The first baseline is that docked widgets must remain reachable by
scrolling rather than being clipped by minimum-size constraints.

## Landed Baseline

- Dock insertion remains direct through `Dock.addWidget(widget)` so default and
  JSON-defined dock placement keeps the original pyqtgraph DockArea sizing
  behavior. A global dock-level scroll wrapper was avoided because it perturbed
  dock proportions even when setup positions were still read correctly.
- Scan widgets no longer force their scroll area and parent widget to the
  full content minimum width.
- Laser widgets no longer force their scroll area and parent widget to the
  full content minimum width.
- No docked widget pins its own minimum *height* either. Docks stack
  vertically and a splitter's minimum is the sum of its children's, so a panel
  insisting on 320 px added 320 px to the smallest size the window could take;
  a laser column, a scan panel and a TriggerScope panel in one column pushed
  that past the screen, at which point Qt holds the window at its minimum and
  the bottom is cut off -- the maximize / restore / maximize cycle worked
  around exactly that. The panels already scrolled their own contents, so the
  minimum was doing nothing for them and everything to the window. Removed
  from Laser (320), the six TriggerScope/light-sheet panels, ScanWidgetBase
  and Console (200 / 180 each), which took the example STED profile's window
  minimum from 999x649 to 1105x383. A source-level contract test keeps them
  out.
- **Every docked panel scrolls its own contents, applied once in
  `WidgetFactory.createWidget()`.** Removing the explicit minimum heights (the
  bullet above) was not enough: a panel's minimum is whatever its *layout*
  needs, so a form of thirty rows still added thirty rows' worth of height to
  the window minimum. `Widget.makeScrollable()` moves a panel's layout into an
  internal `QScrollArea`; `Widget.scrollablePanel = False` opts out (Console,
  which scrolls itself and wants all the room it is given). This is the
  per-panel form of the dock-level wrapper that was rejected above, and it
  only works paired with the next bullet.

  The wrapper has to be transparent to `sizeHint()`, not only to minimums.
  Qt's hint for a resizable `QScrollArea` does not follow its contents
  (measured: 42 px while the content asked for 150), and three panels -- Flip
  Mirrors, Stand and Setup Modes -- cap themselves at their own preferred
  height with `QSizePolicy.Maximum`, so they opened a few dozen pixels tall
  with their contents scrolling inside that however tall their dock was.
  `_PanelScrollArea` reports the content's hint, *and* invalidates it on the
  content's `LayoutRequest`: panels are wrapped while still empty and filled
  in by their controllers afterwards, so the layout above otherwise keeps the
  hint the panel had with nothing in it. Both halves are needed -- each was
  checked by removing the other.
- **Dock sizes come from panel content, not from uniform stretch factors.**
  Every dock was created with `size=(1, 1)`, so pyqtgraph gave a one-row panel
  exactly as much height as a thirty-row one and recomputed that share from
  scratch on every dock drag -- which is why moving one panel rearranged all
  the others. What hid it was panels refusing to shrink, so the layout was
  really being decided by minimums. `ImConMainView.applyContentAwareDockSizing()`
  sets each dock's stretch from `Widget.panelContentSizeHint()`, bounded at
  both ends, and the viewer's width share is expressed against the widest
  panel column instead of a bare number. It runs three times: while the view
  is built, once the controllers have populated the panels, and once more when
  the window is first shown (panels such as the detector parameter trees only
  report their real size after that). A restored saved layout wins over all
  three.
- **No window may insist on more than 80% of the screen.** `ImConMainView` and
  `ImProcessMainView` are pages of MultiModuleWindow's tab widget, so either
  one's minimum becomes the application window's; a window that cannot be made
  as small as the screen opens with its bottom edge below it, which is what
  the maximize / restore cycle worked around. Both clamp
  `minimumSizeHint()` against `availableGeometry()`. `ImConMainView` also
  clears the explicit minimum Qt writes onto it while it is briefly a
  top-level window during construction -- that minimum outlives the
  reparenting into the tab widget and is never recomputed there.
- **MultiModuleWindow sizes its frame, not its client area, to the screen.**
  `move()` places the frame while `resize()` sizes the client area, so the
  title bar hung below the available area; the frame margins are measured
  after `show()` and subtracted.
- **Rearranging one column no longer resizes the others.** A column's stretch
  is the sum of its docks', so *any* dock move changes it -- and pyqtgraph
  answers a descendant's stretch change by running `updateStretch()` on every
  container up to the top, each of which re-divides its own splitter from the
  stretch factors alone. Moving a dock in the right-hand column therefore
  threw away every width the user had dragged, including the left column's.
  `_LayoutPreservingDockArea` (in `ImConMainView`) hands out container
  subclasses whose `childStretchChanged` still carries the new stretch value
  upward but suppresses the resize; containers whose own children changed
  reach `updateStretch()` through `insert()` / `childEvent_()` instead and
  still lay themselves out, because room has to be made for a dock that just
  arrived. Measured against stock pyqtgraph, columns went from
  `[357, 337, 198]` to `[297, 298, 297]` on a move in the right column; they
  now stay put.
- **...and the same when rearranging *restructures* the containers.**
  Suppressing the stretch-change resize does not cover every path: tabbing a
  two-dock column's docks together leaves that column holding a single tab
  container, so pyqtgraph dissolves the column and puts the tab container in
  its place -- a genuine child change for the splitter that holds the
  columns, which re-divided the window width again. `addDock()` (which
  `moveDock()` and every drag-and-drop go through) now snapshots every
  splitter's sizes and puts them back afterwards for any container that ended
  up with the same number of children it started with: no pane was gained or
  lost there, whatever happened inside it. A container that really did gain or
  lose one -- a column split off, or emptied -- is laid out by pyqtgraph as
  before, because the space has to come from somewhere. The area a dock
  *leaves* gets the same protection, because whoever gains the dock is the one
  running: floating a dock out runs `addDock` on the new window's area, not on
  the one losing it. `addTempArea()` is overridden too, since pyqtgraph
  hard-codes a stock `DockArea` for floating windows.
- **Tools > Reset panel layout** puts the docks back where the setup file puts
  them, at content-derived sizes -- the way back from a layout that a drag
  rearranged.
- Recording keeps Snap/REC controls visible while its settings grid scrolls
  internally.
- Positioner keeps its existing controls and signals but places the per-axis
  grid inside an internal scroll area.
- Advanced scan center spin boxes allow negative center positions.
- Advanced Scan no longer exposes partial BeadRec center/axial controls; it
  keeps only the scan-geometry interface consumed by BeadRec reconstruction.
- Source-level contract tests cover direct dock insertion, scan/laser scroll
  behavior, Recording and Positioner scrolling, negative Advanced scan centers,
  and absence of orphaned Advanced Scan BeadRec controls.

## Follow-Up Ideas

- **Numeric field consistency**
  - Replace free-form numeric `QLineEdit` controls with `QDoubleSpinBox`,
    `QSpinBox`, or validated line edits where free-form lists are not needed.
  - Audit all coordinate, offset, delay, power, size, and timing fields for
    explicit ranges and units.
  - Add setup-aware ranges where hardware metadata provides travel limits,
    voltage limits, exposure limits, or power limits.

- **Responsive layout pass**
  - Replace very wide grid rows with grouped vertical sections in dense widgets
    such as Scan, Advanced Scan, Recording, SLMs, EtSTED, and EtMonalisa.
  - Avoid fixed graph heights where they crowd controls on small displays;
    use splitter or collapsible advanced sections where appropriate.
  - Prefer horizontal scrolling only for table-like controls; prefer wrapping
    or sectioning for normal forms.
  - Current hard-size audit still flags several legitimate but review-worthy
    fixed/minimum sizes in dialogs, small numeric inputs, SLM controls, graph
    heights, and the BeadRec list panel. These should be reviewed case by case
    rather than removed globally.
  - Napari/Image dock minimum: `ImageWidget.minimumSizeHint()` now *caps* the
    embedded viewer's minimum at `minimumViewerSize` (240x180) rather than
    dropping it to zero. Reporting no minimum at all was tried before and
    collapsed the dock layout during startup; a cap keeps the viewer weighing
    something while still letting the window fit any screen. Note that
    `nw.setMinimumSize(0, 0)` alone never did this -- it only clears an
    *explicit* minimum, and Qt falls back to `minimumSizeHint()`.

- **Advanced Scan**
  - Make phase delay and D3 step delay validated numeric controls with explicit
    units and allowed negative/positive semantics documented in tooltips.
  - Clarify the difference between line repeats, advanced line program, and
    per-line-step power controls.
  - Move advanced intra-pixel pulse editing into a collapsible section.
  - If Advanced Scan should support BeadRec auto-axial workflows later, add the
    full MoNaLISA-equivalent workflow explicitly instead of reintroducing
    partial center controls.

- **Recording**
  - Split capture target, output path, format, and acquisition mode into clearer
    sections.
  - Add inline validation for unwritable folders and incompatible file/mode
    combinations.

- **EtSTED / EtMonalisa**
  - Add visible status and validation messages consistently across both widgets.
  - Move pipeline/transform/scanning prerequisites into a compact preflight
    section.
  - Hide or collapse calibration-only controls during normal acquisition.

- **Laser / Positioner / Rotator**
  - Add setup-derived min/max ranges where available.
  - Avoid fixed-width rows for setups with many devices; use per-device
    collapsible rows or a table with scrollbars.
  - Positioner scrollability is only a containment fix. A later pass should
    still make large multi-axis setups denser and easier to scan visually.

- **Testing**
  - Added `test_widget_sizing_audit()` in `test_widget_responsiveness_contract.py`:
    scans all widget source files for problematic hard-sizing patterns
    (`scrollArea.setMinimumWidth`, `ScrollBarAlwaysOff`). Uses an allowlist
    approach for known exceptions. The existing SLMsWidget horizontal
    scroll-disable pattern is allowlisted with a review note.
  - Add a lightweight Qt smoke test for creating representative no-hardware
    widget sets inside a constrained viewport.
  - Add targeted tests for fields that must allow negative coordinates or
    offsets.

## Suggested Work Order

1. Finish the scrollability baseline widget by widget, preserving dock placement
   semantics and adding internal scroll areas only where the widget owns its
   layout.
2. Audit and normalize numeric ranges for Scan, Advanced Scan, Positioner,
   Rotator, and Laser widgets.
3. Refactor the densest widgets into clear sections or collapsible panels.
4. Add constrained-viewport smoke tests once full UI collection no longer pulls
   in incompatible GUI dependencies.
