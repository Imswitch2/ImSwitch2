"""Render the Workflow editor 2.0 mockups (the spike behind the plan's images).

    xvfb-run -a -s "-screen 0 1920x1080x24" python tools/workflow_editor_2_mockups/mock_screens.py OUTDIR [screen ...]

Screens: start, graph, outdated, quickadd, list, curves. Real catalogue, real
document model, real run results on the synthetic recording; the window
around them is the proposal in docs/design/plans/improcess-workflow-editor-2.md.
Throwaway code: Phase 1 reimplements the canvas properly; this stays only as
the generator of the plan's images until the real editor can render them.
"""

from __future__ import annotations

import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "examples", "improcess_workflows"))

from imswitch.imcommon import prepareApp  # noqa: E402

app = prepareApp()

import h5py  # noqa: E402
import numpy as np  # noqa: E402
from qtpy import QtCore, QtGui, QtWidgets  # noqa: E402

import canvas as cv  # noqa: E402
from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog  # noqa: E402
from imswitch.improcess.view.workfloweditor.paramform import ParamForm  # noqa: E402
from imswitch.improcess.workflows import bootstrap_registry, run  # noqa: E402

import tempfile  # noqa: E402

EXAMPLES = os.path.join(REPO, "examples", "improcess_workflows")
DATA = tempfile.mkdtemp(prefix="wfe2-mockups-")

CATALOG = build_catalog(user_plugins=False)
REGISTRY = bootstrap_registry(user_plugins=False)

STYLE = """
QToolBar#wfToolbar { background: #1a242e; border: none; border-bottom: 1px solid #2a3846; spacing: 6px; padding: 6px 10px; }
QToolBar#wfToolbar QToolButton { color: #dfe4ea; background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px 10px; font-size: 10pt; }
QToolBar#wfToolbar QToolButton:hover { background: #26323e; }
QToolBar#wfToolbar QToolButton#runButton { background: #2f7d55; border: 1px solid #3c9a6e; color: white; font-weight: bold; padding: 5px 14px; }
QToolBar#wfToolbar QToolButton#modeOn { background: #34407a; border: 1px solid #6666cc; color: white; }
QToolBar#wfToolbar QToolButton#modeOff { background: #1f2a35; border: 1px solid #2e3e4f; color: #93a0ad; }
QLabel#chipOk { background: #1d3a2c; color: #8fe0b2; border: 1px solid #2f6a4b; border-radius: 10px; padding: 3px 10px; }
QLabel#chipWarn { background: #3d2c17; color: #f5c48a; border: 1px solid #7a5420; border-radius: 10px; padding: 3px 10px; }
QWidget#side { background: #17212b; }
QLabel#sectionTitle { color: #93a0ad; font-size: 8.5pt; font-weight: bold; letter-spacing: 1px; padding: 10px 2px 4px 2px; }
QLineEdit#search { background: #0f161d; border: 1px solid #2e3e4f; border-radius: 8px; padding: 7px 10px; color: #e3e6ea; font-size: 10pt; }
QTreeWidget#palette { background: #17212b; border: none; color: #d7dde3; font-size: 9.5pt; }
QTreeWidget#palette::item { padding: 3px 2px; }
QFrame#inspectorHeader { border-radius: 8px; }
QLabel#inspTitle { color: white; font-size: 12.5pt; font-weight: bold; }
QLabel#inspSub { color: #e8ecf5; font-size: 8.8pt; }
QLabel#muted { color: #93a0ad; font-size: 8.8pt; }
QPushButton#primary { background: #2f7d55; border: 1px solid #3c9a6e; color: white; border-radius: 6px; padding: 6px 12px; font-weight: bold; }
QPushButton#secondary { background: #22303c; border: 1px solid #34465a; color: #dfe4ea; border-radius: 6px; padding: 6px 12px; }
QFrame#card { background: #1c2732; border: 1px solid #2a3846; border-radius: 8px; }
QStatusBar { background: #1a242e; color: #93a0ad; border-top: 1px solid #2a3846; }
"""


def pump(seconds=0.3):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def dot_icon(colour: str, size: int = 12) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pixmap)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(colour))
    p.drawRoundedRect(QtCore.QRectF(1, 1, size - 2, size - 2), 3, 3)
    p.end()
    return QtGui.QIcon(pixmap)


# -- data ----------------------------------------------------------------------

def recordings():
    from _synthetic import write_synthetic_recording

    os.makedirs(DATA, exist_ok=True)
    two = write_synthetic_recording(os.path.join(DATA, "cells.h5"), frames=2)
    with h5py.File(two, "a") as handle:
        handle["data"].attrs["axes"] = "CYX"
    twelve = write_synthetic_recording(os.path.join(DATA, "timelapse.h5"), frames=12, seed=3)
    return str(two), str(twelve)


def run_document(document, source):
    stamps = []
    workflow = document.workflow
    report = run(workflow, registry=REGISTRY, bindings={"raw": source}, out_dir=os.path.join(DATA, "out"),
                 overwrite=True, progress=lambda i, n, s: stamps.append((s, time.perf_counter())))
    durations = {stamps[i][0]: stamps[i + 1][1] - stamps[i][1] for i in range(len(stamps) - 1)}
    return report, durations


def load(name):
    document = WorkflowDocument.load(os.path.join(EXAMPLES, f"{name}.yaml"), CATALOG)
    return document


# -- the window ----------------------------------------------------------------------

class MockWindow(QtWidgets.QMainWindow):
    def __init__(self, title: str, *, mode="graph", chip=("ok", "✓  Ready to run · 7 steps"), write_files=False):
        self._write_files = write_files
        super().__init__()
        self.setStyleSheet(STYLE)
        self.setWindowTitle(title)
        self.resize(1920, 1060)
        self._build_toolbar(mode, chip)
        self.palette_panel = self._build_palette()
        self.scene = QtWidgets.QGraphicsScene()
        self.view = cv.CanvasView(self.scene)
        self.inspector = QtWidgets.QWidget()
        self.inspector.setObjectName("side")
        self.inspector.setMinimumWidth(380)
        self.inspector.setMaximumWidth(400)
        self.inspector_layout = QtWidgets.QVBoxLayout(self.inspector)
        self.inspector_layout.setContentsMargins(12, 12, 12, 12)
        self.inspector_layout.setSpacing(8)
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.palette_panel)
        splitter.addWidget(self.view)
        splitter.addWidget(self.inspector)
        splitter.setSizes([250, 1270, 400])
        splitter.setHandleWidth(1)
        self.setCentralWidget(splitter)

    # toolbar ------------------------------------------------------------------
    def _build_toolbar(self, mode, chip):
        bar = QtWidgets.QToolBar()
        bar.setObjectName("wfToolbar")
        bar.setMovable(False)
        bar.setIconSize(QtCore.QSize(16, 16))
        self.addToolBar(bar)

        def button(text, name=None, tip=""):
            b = QtWidgets.QToolButton()
            b.setText(text)
            if name:
                b.setObjectName(name)
            b.setToolTip(tip)
            bar.addWidget(b)
            return b

        title = QtWidgets.QLabel("  split-process-merge  ")
        title.setStyleSheet("color:#ffffff; font-size:11pt; font-weight:bold;")
        self.title_label = title
        bar.addWidget(title)
        for text in ("New", "Open…", "Save"):
            button(text)
        bar.addSeparator()
        button("▶  Run", "runButton")
        button("▶|  Run to selected")
        button("■  Stop")
        auto = button("⟳  Auto-run: off")
        auto.setToolTip("Re-run outdated steps a moment after each edit")
        self.auto_button = auto
        files = button("⤓  Write files: on" if self._write_files else "⤓  Write files: off")
        files.setToolTip("Off: runs keep results in memory and Save steps only show what they would write")
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        bar.addWidget(spacer)
        self.graph_button = button("◇  Graph", "modeOn" if mode == "graph" else "modeOff")
        self.list_button = button("☰  List", "modeOn" if mode == "list" else "modeOff")
        bar.addSeparator()
        self.chip = QtWidgets.QLabel(chip[1])
        self.chip.setObjectName("chipOk" if chip[0] == "ok" else "chipWarn")
        bar.addWidget(self.chip)

    # palette --------------------------------------------------------------------
    def _build_palette(self):
        side = QtWidgets.QWidget()
        side.setObjectName("side")
        side.setMinimumWidth(240)
        side.setMaximumWidth(270)
        layout = QtWidgets.QVBoxLayout(side)
        layout.setContentsMargins(12, 12, 8, 8)
        search = QtWidgets.QLineEdit()
        search.setObjectName("search")
        search.setPlaceholderText("🔍  Add a step…")
        layout.addWidget(search)
        hint = QtWidgets.QLabel("Drag onto the canvas, or drag a cable into empty space to pick what fits.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        tree = QtWidgets.QTreeWidget()
        tree.setObjectName("palette")
        tree.setHeaderHidden(True)
        tree.setIndentation(14)
        tree.setRootIsDecorated(False)

        def group(name, colour, children, expanded=False):
            top = QtWidgets.QTreeWidgetItem([name])
            top.setIcon(0, dot_icon(colour))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for child, tip in children:
                item = QtWidgets.QTreeWidgetItem([child])
                item.setToolTip(0, tip)
                item.setForeground(0, QtGui.QBrush(QtGui.QColor("#c9d1d9")))
                top.addChild(item)
            tree.addTopLevelItem(top)
            top.setExpanded(expanded)

        group("Recent", "#93a0ad", [("Filter", ""), ("Python step", ""), ("Projection", "")], expanded=True)
        group("Inputs & outputs", cv.STEP_COLOURS["source"], [("Source (file)", ""), ("Save", "")], expanded=True)
        group("Reconstruct", cv.STEP_COLOURS["reconstruct"],
              [(e.name, e.description) for e in CATALOG.runnable("reconstructor")])
        for category in CATALOG.categories():
            entries = [e for e in CATALOG.processors_in(category) if e.gui_only is None]
            if entries:
                group(category, cv.CATEGORY_COLOURS.get(category, "#7a8794"), [(e.name, e.description) for e in entries],
                      expanded=category == "Filters")
        layout.addWidget(tree, 1)
        self.palette_tree = tree
        self.search = search
        return side

    # inspector ---------------------------------------------------------------------
    def clear_inspector(self):
        while self.inspector_layout.count():
            item = self.inspector_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def inspector_header(self, model, description):
        frame = QtWidgets.QFrame()
        frame.setObjectName("inspectorHeader")
        c = QtGui.QColor(model.colour)
        frame.setStyleSheet(f"QFrame#inspectorHeader {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {c.name()}, stop:1 {c.darker(140).name()}); }}")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        title = QtWidgets.QLabel(model.title)
        title.setObjectName("inspTitle")
        layout.addWidget(title)
        row = QtWidgets.QHBoxLayout()
        idedit = QtWidgets.QLineEdit(model.id)
        idedit.setMaximumWidth(150)
        idedit.setStyleSheet("background: rgba(0,0,0,70); border: 1px solid rgba(255,255,255,60); border-radius: 5px; color: white; padding: 3px 6px; font-family: 'DejaVu Sans Mono';")
        row.addWidget(QtWidgets.QLabel("<span style='color:#eef'>id</span>"))
        row.addWidget(idedit)
        row.addStretch(1)
        layout.addLayout(row)
        if description:
            sub = QtWidgets.QLabel(description)
            sub.setObjectName("inspSub")
            sub.setWordWrap(True)
            layout.addWidget(sub)
        self.inspector_layout.addWidget(frame)

    def section(self, text):
        label = QtWidgets.QLabel(text.upper())
        label.setObjectName("sectionTitle")
        self.inspector_layout.addWidget(label)

    def buttons(self, specs):
        row = QtWidgets.QHBoxLayout()
        for text, kind in specs:
            b = QtWidgets.QPushButton(text)
            b.setObjectName(kind)
            row.addWidget(b)
        row.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        self.inspector_layout.addWidget(holder)

    def inputs_card(self, rows):
        card = QtWidgets.QFrame()
        card.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        for text, kind in rows:
            colour = cv.KIND_COLOURS.get(kind, "#a3adb8")
            label = QtWidgets.QLabel(f"<span style='color:{colour}; font-size:13pt'>●</span>&nbsp; {text}"
                                     f"&nbsp;&nbsp;<span style='color:#93a0ad'>{kind}</span>")
            label.setStyleSheet("color:#dfe4ea;")
            layout.addWidget(label)
        self.inspector_layout.addWidget(card)

    def param_form(self, entry, values, *, height=150):
        form = ParamForm()
        form.set_plugin(entry, values)
        form.setMinimumHeight(height)
        form.setMaximumHeight(height + 20)
        self.inspector_layout.addWidget(form)
        return form

    def preview(self, result, kind, caption):
        card = QtWidgets.QFrame()
        card.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(8, 8, 8, 8)
        pixmap = cv.thumbnail(result, kind, size=(352, 232))
        label = QtWidgets.QLabel()
        label.setPixmap(pixmap)
        label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(label)
        cap = QtWidgets.QLabel(caption)
        cap.setObjectName("muted")
        layout.addWidget(cap)
        self.inspector_layout.addWidget(card)
        return card

    def status(self, text):
        self.statusBar().showMessage(text)

    def fit(self, margin=60, max_zoom=1.0):
        rect = self.scene.itemsBoundingRect().adjusted(-margin, -margin, margin, margin)
        self.view.fitInView(rect, QtCore.Qt.KeepAspectRatio)
        if self.view.transform().m11() > max_zoom:
            self.view.resetTransform()
            self.view.scale(max_zoom, max_zoom)
            self.view.centerOn(rect.center())
        self.view.set_zoom_text()

    def shoot(self, path, *, with_popups=False):
        pump(0.6)
        if with_popups:
            geo = self.frameGeometry()
            pixmap = app.primaryScreen().grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
        else:
            pixmap = self.grab()
        pixmap.save(path)
        print("saved", path, flush=True)


# -- screens --------------------------------------------------------------------------

def screen_graph_after_run(outdir, cells, _lapse):
    """A: the diamond after an interactive run, 'smooth' selected."""
    document = load("split_process_merge_diamond")
    report, durations = run_document(document, cells)
    models = cv.build_models(document, report, durations)
    models["raw"].summary = ["cells.h5", models["raw"].summary[-1]]
    cv.would_write(models["out"])
    win = MockWindow("Workflow editor 2.0 — graph")
    win.move(0, 0)
    items = cv.populate(win.scene, document, models)
    items["smooth"].setSelected(True)
    step = document.step("smooth")
    entry = document.entry_for(step)
    win.inspector_header(models["smooth"], entry.description)
    win.section("Input")
    win.inputs_card([("split  ›  C0", "image")])
    win.section("Parameters")
    win.param_form(entry, document.effective_params("smooth"), height=120)
    win.section("Output")
    win.preview(report.result("smooth"), "image", "smooth.out  ·  96 × 96  YX  ·  image  ·  1 ms")
    win.buttons([("▶|  Run to here", "primary"), ("Show in viewer", "secondary"), ("Pin", "secondary")])
    win.inspector_layout.addStretch(1)
    win.status("Ran 6 steps in 0.2 s  ·  results kept in memory  ·  Write files is off: ‘out’ would write cells_diamond.h5")
    win.show()
    pump()
    win.fit(margin=50)
    win.shoot(os.path.join(outdir, "2_graph_after_run.png"))
    win.close()
    report.close()


def screen_outdated_and_run_to_here(outdir, cells, _lapse):
    """B: 'smooth' was edited; what depends on it is outdated; Run to here on 'merge'."""
    document = load("split_process_merge_diamond")
    report, durations = run_document(document, cells)
    document.set_param("smooth", "radius", 4.0)
    status = {"smooth": "stale", "merge": "stale"}
    models = cv.build_models(document, report, durations, status=status)
    models["raw"].summary = ["cells.h5", models["raw"].summary[-1]]
    cv.would_write(models["out"])
    models["out"].status = "idle"
    win = MockWindow("Workflow editor 2.0 — outdated", chip=("warn", "↻  2 steps outdated"))
    win.move(0, 0)
    items = cv.populate(win.scene, document, models)
    items["smooth"].setSelected(True)
    entry = document.entry_for(document.step("smooth"))
    win.inspector_header(models["smooth"], entry.description)
    banner = QtWidgets.QLabel("<b>Changed.</b> ‘smooth’ and ‘merge’ after it are outdated; their old "
                              "results stay visible, dimmed. <b>Run to here</b> (F6) re-runs only what is "
                              "outdated — ‘raw’, ‘rec’, ‘split’ and ‘sharp’ come from the cache.")
    banner.setWordWrap(True)
    banner.setStyleSheet("background:#3d2c17; color:#f5d3a8; border:1px solid #7a5420; border-radius:8px; padding:8px 10px;")
    win.inspector_layout.addWidget(banner)
    win.section("Parameters")
    form = win.param_form(entry, document.effective_params("smooth"), height=120)
    win.section("Output (last run, before the change)")
    win.preview(report.result("smooth"), "image", "smooth.out  ·  radius 2 → now 4  ·  outdated")
    win.buttons([("▶|  Run to here", "primary"), ("Show in viewer", "secondary"), ("Undo change", "secondary")])
    win.inspector_layout.addStretch(1)
    win.status("Edited ‘smooth’ radius 2 → 4  ·  2 steps outdated  ·  Ctrl+Z undoes")
    win.show()
    pump()
    win.fit(margin=50)
    pump()
    menu = QtWidgets.QMenu(win)
    menu.setStyleSheet("QMenu { background:#1c2732; color:#e3e6ea; border:1px solid #34465a; padding:6px; font-size:10pt; }"
                       "QMenu::item { padding:6px 26px 6px 14px; border-radius:5px; } QMenu::item:selected { background:#34407a; }"
                       "QMenu::separator { height:1px; background:#2e3e4f; margin:5px 8px; }")
    for text in ("▶|   Run to here                     F6", "⟲   Re-run from here", "◉   Show output in viewer",
                 "📌   Pin output to the results list", None, "⧉   Duplicate                         Ctrl+D",
                 "✕   Delete                              Del"):
        if text is None:
            menu.addSeparator()
        else:
            menu.addAction(text)
    merge = items["merge"]
    point = win.view.mapFromScene(merge.mapToScene(QtCore.QPointF(cv.NODE_W - 30, 40)))
    menu.popup(win.view.viewport().mapToGlobal(point))
    menu.setActiveAction(menu.actions()[0])
    win.shoot(os.path.join(outdir, "3_outdated_run_to_here.png"), with_popups=True)
    menu.close()
    win.close()
    report.close()


def screen_quick_add(outdir, cells, _lapse):
    """C: a cable dragged from split.C1 into empty space offers what fits there."""
    document = load("split_process_merge_diamond")
    report, durations = run_document(document, cells)
    for sid in ("out", "merge", "sharp", "smooth"):
        document.remove_step(sid)
    keep = {sid: d for sid, d in durations.items() if document.has(sid)}
    models = cv.build_models(document, report, keep)
    models["raw"].summary = ["cells.h5", models["raw"].summary[-1]]
    win = MockWindow("Workflow editor 2.0 — quick add", chip=("ok", "✓  Ready to run · 3 steps"))
    win.move(0, 0)
    items = cv.populate(win.scene, document, models)
    items["split"].setSelected(True)
    # inspector: the selected split
    entry = document.entry_for(document.step("split"))
    win.inspector_header(models["split"], entry.description)
    win.section("Parameters")
    win.param_form(entry, document.effective_params("split"), height=110)
    win.section("Outputs")
    win.inputs_card([("C0  ·  96 × 96  YX", "image"), ("C1  ·  96 × 96  YX", "image")])
    win.inspector_layout.addStretch(1)
    win.status("Drag from an output into empty space to add the next step  ·  Esc cancels")
    win.show()
    pump()
    win.fit(margin=40)
    rect = win.scene.itemsBoundingRect()
    win.view.resetTransform()
    win.view.scale(1.0, 1.0)
    win.view.centerOn(rect.center() + QtCore.QPointF(330, 40))
    win.view.set_zoom_text()
    pump()
    split = items["split"]
    start = split.port_pos("out", 1)
    end = start + QtCore.QPointF(210, 70)
    win.scene.addItem(cv.EdgeItem(start, end, "image", dashed=True))
    win.scene.addEllipse(QtCore.QRectF(end.x() - 5, end.y() - 5, 10, 10), QtGui.QPen(QtGui.QColor("#5aa9e6"), 2),
                         QtGui.QBrush(QtGui.QColor("#5aa9e6")))
    pump()
    popup = QtWidgets.QFrame(win, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
    popup.setStyleSheet("QFrame { background:#1c2732; border:1px solid #4a5d72; border-radius:10px; }"
                        "QLineEdit { background:#0f161d; border:1px solid #6666cc; border-radius:7px; padding:7px 9px; color:#e3e6ea; font-size:10.5pt; }"
                        "QLabel { border:none; background:transparent; }")
    layout = QtWidgets.QVBoxLayout(popup)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(4)
    head = QtWidgets.QLabel("<span style='color:#5aa9e6'>●</span> <b style='color:#e3e6ea'>split › C1</b>"
                            "<span style='color:#93a0ad'>  · image, 96 × 96 YX — steps that take it:</span>")
    layout.addWidget(head)
    search = QtWidgets.QLineEdit()
    search.setPlaceholderText("Type to narrow down…")
    layout.addWidget(search)
    picks = []
    for e in CATALOG.processors:
        if e.gui_only is None and "image" in e.accepted_kinds and e.min_inputs == 1:
            picks.append(e)
    order = ["filter", "subtract-background", "denoise", "projection", "math", "python", "convert-type", "resize"]
    picks.sort(key=lambda e: order.index(e.id) if e.id in order else 99)
    for i, e in enumerate(picks[:7]):
        row = QtWidgets.QLabel()
        colour = cv.CATEGORY_COLOURS.get(e.category, "#7a8794")
        bg = "#34407a" if i == 0 else "transparent"
        name = e.name
        row.setText(f"<table width='100%'><tr><td width='14'><span style='color:{colour}; font-size:12pt'>■</span></td>"
                    f"<td><b style='color:#e3e6ea'>{name}</b> <span style='color:#93a0ad'>· {e.category}</span><br>"
                    f"<span style='color:#9fb0c0; font-size:8.5pt'>{e.description[:70]}</span></td></tr></table>")
        row.setStyleSheet(f"background:{bg}; border-radius:6px; padding:4px 6px;")
        layout.addWidget(row)
    foot = QtWidgets.QLabel("<span style='color:#93a0ad; font-size:8.5pt'>↑↓ choose · Enter adds and connects · "
                            "only steps that accept an image are listed</span>")
    layout.addWidget(foot)
    popup.setFixedWidth(430)
    popup.adjustSize()
    vp = win.view.mapFromScene(end)
    popup.move(win.view.viewport().mapToGlobal(vp) + QtCore.QPoint(14, -40))
    popup.show()
    win.shoot(os.path.join(outdir, "4_quick_add.png"), with_popups=True)
    popup.close()
    win.close()
    report.close()


def screen_list_mode(outdir, _cells, lapse):
    """D: list mode: the same graph as a pipeline; a real error stops the run."""
    from imswitch.improcess.workflows import RunError

    document = load("python_step_crosstalk")
    stamps = []
    try:
        run(document.workflow, registry=REGISTRY, bindings={"raw": lapse}, out_dir=os.path.join(DATA, "out_err"),
            overwrite=True, progress=lambda i, n, s: stamps.append((s, time.perf_counter())))
        report, message = None, ""
    except RunError as exc:
        report, message = exc.report, str(exc)
    durations = {stamps[i][0]: stamps[i + 1][1] - stamps[i][1] for i in range(len(stamps) - 1) if stamps[i][0] in report.steps_run}
    error = message.split(": ", 1)[1] if ": " in message else message
    error = error.replace("'python' failed on 'timelapse.h5': ", "")
    status = {s.id: "queued" for s in document.steps if s.id not in durations}
    models = cv.build_models(document, report, durations, status=status, errors={"script": error})
    models["raw"].summary = ["timelapse.h5", models["raw"].summary[-1]]
    models["script"].title = "Python step · crosstalk"
    win = MockWindow("Workflow editor 2.0 — list", mode="list", chip=("warn", "✕  Stopped at ‘script’"))
    win.title_label.setText("  python-crosstalk  ")
    win.move(0, 0)
    items = cv.populate_list(win.scene, document, models)
    items["script"].setSelected(True)
    step = document.step("script")
    entry = document.entry_for(step)
    win.inspector_header(models["script"], "Run a few lines of Python over the inputs.")
    err = QtWidgets.QLabel(f"<b>line 11</b> — ValueError: the matrix is for 2 channels, the data has 12 along axis 0"
                           "<br><span style='color:#f0b8b4'>Input ‘rec’: 12 × 96 × 96 (Frame, Y, X), float32. "
                           "Line 11 is marked in the code below.</span>")
    err.setWordWrap(True)
    err.setStyleSheet("background:#3a1a1c; color:#ffd6d4; border:1px solid #8a3a37; border-radius:8px; padding:8px 10px;")
    win.inspector_layout.addWidget(err)
    win.section("Code")
    from imswitch.imcommon.view.guitools.CodeEditor import PythonCodeEditor
    editor = PythonCodeEditor()
    editor.setText(step.params.get("code", ""))
    editor.setMinimumHeight(330)
    win.inspector_layout.addWidget(editor)
    win.buttons([("▶  Run this step", "primary"), ("Open in console", "secondary"), ("Load snippet…", "secondary")])
    win.inspector_layout.addStretch(1)
    win.status("Stopped at ‘script’ after 2 steps  ·  ‘raw’ and ‘rec’ stay cached  ·  fix the code and press Run to continue from here")
    win.show()
    pump()
    rect = win.scene.itemsBoundingRect().adjusted(-40, -30, 40, 30)
    win.view.resetTransform()
    win.view.centerOn(rect.center() + QtCore.QPointF(0, 120))
    win.view.ensureVisible(rect)
    win.view.set_zoom_text()
    win.shoot(os.path.join(outdir, "5_list_mode_error.png"))
    win.close()


def screen_python_curves(outdir, _cells, lapse):
    """E: a recipe with curve outputs: typed green ports, sparkline previews, the plot in the inspector."""
    import pyqtgraph as pg

    document = load("python_step_signal_trace")
    report, durations = run_document(document, lapse)
    models = cv.build_models(document, report, durations)
    models["raw"].summary = ["timelapse.h5", models["raw"].summary[-1]]
    models["script"].title = "Python step · signal_trace"
    win = MockWindow("Workflow editor 2.0 — curves", chip=("ok", "✓  Ran 5 steps · 2 files written"), write_files=True)
    win.title_label.setText("  python-signal-trace  ")
    win.move(0, 0)
    items = cv.populate(win.scene, document, models)
    items["script"].setSelected(True)
    win.inspector_header(models["script"], "Follow the bright signal through a recording.")
    win.section("Output ports")
    win.inputs_card([("level  ·  12 points along Frame", "curve"), ("area  ·  12 points along Frame", "curve")])
    win.section("Preview · level")
    plot = pg.PlotWidget()
    plot.setBackground("#0c1218")
    y = np.asarray(report.result("script.level").data, dtype=float)
    plot.plot(np.arange(y.size), y, pen=pg.mkPen("#7bd389", width=2.5), symbol="o", symbolSize=6,
              symbolBrush="#7bd389", symbolPen=None)
    plot.getAxis("bottom").setLabel("Frame")
    plot.getAxis("left").setLabel("level (× first frame)")
    plot.showGrid(x=True, y=True, alpha=0.15)
    plot.setMinimumHeight(250)
    win.inspector_layout.addWidget(plot)
    cap = QtWidgets.QLabel("script.level  ·  curve  ·  9 ms   —   Push to Graph panel  ·  Push to table")
    cap.setObjectName("muted")
    win.inspector_layout.addWidget(cap)
    win.section("Code")
    from imswitch.imcommon.view.guitools.CodeEditor import PythonCodeEditor
    editor = PythonCodeEditor()
    editor.setText(document.step("script").params.get("code", ""))
    editor.setMinimumHeight(200)
    win.inspector_layout.addWidget(editor)
    win.buttons([("▶|  Run to here", "primary"), ("Edit in console", "secondary")])
    win.status("Ran 5 steps in 0.02 s  ·  wrote timelapse_signal_trace_level.csv, timelapse_signal_trace_area.csv")
    win.show()
    pump()
    win.fit(margin=50)
    win.shoot(os.path.join(outdir, "6_python_curves.png"))
    win.close()
    report.close()


def screen_start(outdir, _cells, _lapse):
    """G: what a new workflow starts from."""
    win = MockWindow("Workflow editor 2.0 — start", chip=("ok", "Nothing to run yet"))
    win.title_label.setText("  New workflow  ")
    win.move(0, 0)
    page = QtWidgets.QWidget()
    page.setStyleSheet("background:#111921;")
    outer = QtWidgets.QVBoxLayout(page)
    outer.setContentsMargins(70, 50, 70, 40)
    title = QtWidgets.QLabel("Start a workflow")
    title.setStyleSheet("color:#ffffff; font-size:20pt; font-weight:bold;")
    outer.addWidget(title)
    sub = QtWidgets.QLabel("A workflow is a chain of steps: a source, the processing, and where the results go. "
                           "Run it here step by step on one file, then over a whole folder.")
    sub.setStyleSheet("color:#93a0ad; font-size:10.5pt;")
    sub.setWordWrap(True)
    outer.addWidget(sub)
    outer.addSpacing(18)
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(18)

    def card(icon, head, text, action, accent):
        frame = QtWidgets.QFrame()
        frame.setStyleSheet(f"QFrame {{ background:#1c2732; border:1px solid #2e3e4f; border-radius:12px; }}"
                            f"QLabel {{ border:none; background:transparent; }}")
        lay = QtWidgets.QVBoxLayout(frame)
        lay.setContentsMargins(20, 18, 20, 18)
        ic = QtWidgets.QLabel(icon)
        ic.setStyleSheet(f"color:{accent}; font-size:24pt;")
        lay.addWidget(ic)
        h = QtWidgets.QLabel(head)
        h.setStyleSheet("color:#ffffff; font-size:12.5pt; font-weight:bold;")
        h.setWordWrap(True)
        lay.addWidget(h)
        t = QtWidgets.QLabel(text)
        t.setStyleSheet("color:#aeb9c4; font-size:9.6pt;")
        t.setWordWrap(True)
        lay.addWidget(t)
        lay.addStretch(1)
        b = QtWidgets.QPushButton(action)
        b.setStyleSheet(f"background:{accent}; color:white; border:none; border-radius:7px; padding:8px 12px; font-weight:bold;")
        lay.addWidget(b)
        frame.setMinimumHeight(250)
        return frame

    row.addWidget(card("◉", "From the recording that is open",
                       "Starts with a Source bound to <b>cells.h5</b> (2 × 96 × 96, C Y X) and its reconstruction, "
                       "so every step you add runs on real data right away.", "Start from cells.h5", "#3c9a6e"))
    row.addWidget(card("↺", "From what you just did",
                       "Turns the history of the selected result, <b>rec (max Z-projection)</b>, into a workflow: "
                       "3 steps, parameters as you set them.", "Use its history", "#3b8bb5"))
    row.addWidget(card("✦", "From a recipe",
                       "Nine ready-made jobs: normalise frames, temporal binning, best focus, follow a signal, "
                       "ratio with mask, despeckle, autocrop, crosstalk, tile mosaic.", "Browse recipes", "#6666cc"))
    row.addWidget(card("+", "Empty canvas",
                       "Add a Source and build from there. Drag steps from the left, or drag a cable into empty space.",
                       "Blank workflow", "#4a5a6a"))
    outer.addLayout(row)
    outer.addSpacing(26)
    recent = QtWidgets.QLabel("RECENT")
    recent.setStyleSheet("color:#93a0ad; font-size:8.5pt; font-weight:bold; letter-spacing:1px;")
    outer.addWidget(recent)
    for name, when, steps in (("split_process_merge_diamond.yaml", "today 14:02", "7 steps"),
                              ("python_step_signal_trace.yaml", "yesterday", "5 steps"),
                              ("batch_view_only_project_save.yaml", "3 days ago", "4 steps")):
        line = QtWidgets.QLabel(f"<span style='color:#e3e6ea'>{name}</span>"
                                f"<span style='color:#93a0ad'>   ·   {steps}   ·   {when}</span>")
        line.setStyleSheet("padding:6px 2px; font-size:10pt;")
        outer.addWidget(line)
    outer.addStretch(1)
    splitter = win.centralWidget()
    win.view.hide()
    splitter.insertWidget(1, page)
    splitter.setSizes([250, 1270, 0, 400])
    win.clear_inspector()
    tip = QtWidgets.QLabel("<b style='color:#e3e6ea'>New here?</b><br><br>"
                           "<span style='color:#aeb9c4'>• Each box is a step; cables carry results from left to right.<br><br>"
                           "• A dot's colour says what flows: <span style='color:#a3adb8'>recording</span>, "
                           "<span style='color:#5aa9e6'>image</span>, <span style='color:#f2a541'>labels</span>, "
                           "<span style='color:#7bd389'>curve</span>, <span style='color:#c792ea'>table</span>.<br><br>"
                           "• <b>Run</b> runs everything; <b>Run to here</b> runs up to the selected step and "
                           "shows its result inside the box.<br><br>"
                           "• Change a setting and the steps after it turn orange: outdated, re-run to refresh.<br><br>"
                           "• Nothing is saved to disk until a Save step runs.</span>")
    tip.setWordWrap(True)
    tip.setStyleSheet("background:#1c2732; border:1px solid #2e3e4f; border-radius:10px; padding:14px; font-size:9.6pt;")
    win.inspector_layout.addWidget(tip)
    win.inspector_layout.addStretch(1)
    win.status("No workflow open")
    win.show()
    win.shoot(os.path.join(outdir, "1_start.png"))
    win.close()


SCREENS = {"start": screen_start, "graph": screen_graph_after_run, "outdated": screen_outdated_and_run_to_here,
           "quickadd": screen_quick_add, "list": screen_list_mode, "curves": screen_python_curves}


def main():
    outdir = sys.argv[1]
    os.makedirs(outdir, exist_ok=True)
    cells, lapse = recordings()
    names = sys.argv[2:] or list(SCREENS)
    for name in names:
        SCREENS[name](outdir, cells, lapse)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
