"""Screenshots of the real workflow editor, for docs/improcess-workflows.rst.

    QT_QPA_PLATFORM=offscreen python tools/workflow_editor_screenshots.py docs/images/workflow-editor

(or under ``xvfb-run -a -s "-screen 0 1920x1080x24"`` with ``QT_QPA_PLATFORM=xcb``
for the fonts a desktop has). Runs the editor through the real
``WorkflowController`` on the synthetic recordings of
``examples/improcess_workflows/_synthetic.py``, in ImSwitch's own style sheet,
and writes one PNG per screen: start, graph, outdated, quick_add, list_error,
curves, and for workflow schema 2 bypass, controls and run_dialog (only those
with ``--schema-2``). Replaces the spike that rendered the plan's mockups.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "examples" / "improcess_workflows"))

from imswitch.imcommon import prepareApp  # noqa: E402

app = prepareApp()

from qtpy import QtCore, QtGui, QtWidgets  # noqa: E402

from _synthetic import write_synthetic_recording  # noqa: E402
from imswitch.improcess.controller.WorkflowController import WorkflowController  # noqa: E402
from imswitch.improcess.model import folder_preferences, snippets  # noqa: E402
from imswitch.improcess.model.workfloweditor import folders  # noqa: E402
from imswitch.improcess.workflows import bootstrap_registry, run  # noqa: E402
from imswitch.improcess.workflows.steps import Workflow  # noqa: E402

EXAMPLES = REPO / "examples" / "improcess_workflows"
SIZE = (1720, 980)


def pump(seconds: float = 0.25) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


class Rig:
    """The controller (with a stand-in channel), its editor, and data."""

    def __init__(self, scratch: Path):
        self.scratch = scratch
        folder_preferences.options_file_path = lambda: str(scratch / "options.json")
        folders.workflows_directory = lambda create=True: str(scratch / "workflows")
        snippets.snippets_directory = lambda create=True: str(REPO / "imswitch" / "_data" / "user_defaults"
                                                              / "improcess_snippets")
        (scratch / "workflows").mkdir(exist_ok=True)
        self.registry = bootstrap_registry(user_plugins=False)
        self.cells = str(write_synthetic_recording(scratch / "cells.h5", frames=2, axes="CYX"))
        self.timelapse = str(write_synthetic_recording(scratch / "timelapse.h5", frames=12, seed=3))
        self.loaded = []
        self.active = None
        comm = SimpleNamespace(
            sigResultProduced=SimpleNamespace(emit=lambda r, n: self.loaded.append((n, r))),
            getAllResults=lambda: [r for _n, r in self.loaded], getSelectedResults=lambda: list(self.loaded),
        )
        self.controller = WorkflowController(
            comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=0: None),
            SimpleNamespace(getActiveResult=lambda: self.active),
            registry_factory=lambda: self.registry,
        )
        self.editor = self.controller.openEditor()
        self.editor.resize(*SIZE)
        self.editor.show()
        pump()

    def wait(self) -> None:
        end = time.time() + 120
        while self.controller._thread is not None and time.time() < end:
            app.processEvents()
            time.sleep(0.01)
        pump(0.3)

    def open(self, name: str, recording: str):
        editor = self.editor
        editor.document.dirty = False
        editor.open_file(str(EXAMPLES / f"{name}.yaml"))
        editor.bind_source("raw", recording)
        return editor

    def shot(self, path: Path, *, popup=None) -> None:
        pump(0.4)
        pixmap = self.editor.grab()
        if popup is not None:
            painter = QtGui.QPainter(pixmap)
            corner = self.editor.mapFromGlobal(popup.mapToGlobal(QtCore.QPoint(0, 0)))
            painter.drawPixmap(corner, popup.grab())
            painter.end()
        pixmap.save(str(path))
        print("wrote", path)


def schema2_shots(rig: Rig, out: Path) -> None:
    """Controls and bypass: a bypassed filter, the Controls panel, the run dialog."""
    from imswitch.improcess.model.workfloweditor import WorkflowDocument
    from imswitch.improcess.view.WorkflowControlsDialog import WorkflowControlsDialog
    from imswitch.improcess.workflows.prepare import describe_controls

    editor = rig.editor
    editor.document.dirty = False
    document = WorkflowDocument(Workflow.load(EXAMPLES / "split_process_merge_diamond.yaml"), catalog=editor.catalog)
    document.add_control("radius", "smooth.radius", label="Filter radius",
                         help="Radius of both channel filters, in pixels")
    document.add_target("radius", "sharp.radius")
    document.add_control("method", "smooth.method", label="First channel's filter")
    editor.load_document(document, confirm=False)
    editor.set_mode("graph")
    editor.bind_source("raw", rig.cells)
    editor.run()
    rig.wait()
    editor.select_step("smooth")
    editor.toggle_bypass()
    editor.view.fit()
    rig.shot(out / "bypass.png")

    editor.scene.clearSelection()
    editor.refresh()
    editor.view.fit()
    rig.shot(out / "controls.png")

    described = describe_controls(document.workflow, rig.registry)
    dialog = WorkflowControlsDialog(document.name, described, parent=editor)
    dialog.resize(520, dialog.sizeHint().height())
    dialog.show()
    pump(0.4)
    dialog.grab().save(str(out / "run_dialog.png"))
    print("wrote", out / "run_dialog.png")
    dialog.close()
    editor.document.dirty = False


def main(argv) -> int:
    only_schema2 = "--schema-2" in argv
    argv = [arg for arg in argv if arg != "--schema-2"]
    out = Path(argv[1] if len(argv) > 1 else REPO / "docs" / "images" / "workflow-editor")
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="wfe-shots-"))
    rig = Rig(scratch)
    editor = rig.editor
    if only_schema2:
        schema2_shots(rig, out)
        editor.close()
        pump()
        return 0

    # A result with a history, and the recording "open in ImProcess", for the start page.
    with run(Workflow.load(EXAMPLES / "split_process_merge_diamond.yaml"), registry=rig.registry,
             bindings={"raw": rig.cells}, out_dir=scratch) as report:
        rig.active = report.result("merge")
        rig.controller._onCurrentData(SimpleNamespace(dataPath=rig.cells, datasetName=None))
        editor.show_start_page()
        rig.shot(out / "start.png")

    # The diamond after a run, with a frame around the two filters.
    rig.open("split_process_merge_diamond", rig.cells)
    editor.run()
    rig.wait()
    editor.scene.select_steps(["smooth", "sharp"])
    editor.frame_selection("one filter per channel")
    editor.select_step("smooth")
    editor.view.fit()
    rig.shot(out / "graph.png")

    # A parameter changed: the step and everything after it are outdated.
    editor.inspector.param_form.set_value("radius", 4.0)
    editor.select_step("merge")
    editor.view.fit()
    rig.shot(out / "outdated.png")

    # A cable dropped in empty space: the picker offers only what takes it.
    editor.run_to_selected()
    rig.wait()
    editor.view.fit()
    pump()
    port = editor.scene.node("split").output_port("split.C1").centre()
    empty = editor.scene.node("split").pos() + QtCore.QPointF(40, 190)
    editor._on_quick_add("split.C1", empty)
    popup = editor._quick_add
    popup.move(editor.view.mapToGlobal(editor.view.mapFromScene(empty)))
    cable = editor.scene.addPath(__import__(
        "imswitch.improcess.view.workfloweditor.canvas.items", fromlist=["cable_path"]).cable_path(port, empty),
        QtGui.QPen(QtGui.QColor("#5aa9e6"), 2.6, QtCore.Qt.DashLine))
    rig.shot(out / "quick_add.png", popup=popup)
    popup.close()
    editor.scene.removeItem(cable)

    # List mode, stopped at a real error in a Python step.
    rig.open("python_step_crosstalk", rig.timelapse)
    editor.set_mode("list")
    editor.run()
    rig.wait()
    rig.shot(out / "list_error.png")
    editor.set_mode("graph")

    # A Python step with curve outputs, files written.
    rig.open("python_step_signal_trace", rig.timelapse)
    written = scratch / "results"
    written.mkdir(exist_ok=True)
    editor.set_write_files(True, folder=str(written), overwrite=True)
    editor.run()
    rig.wait()
    editor.select_step("script")
    editor.view.fit()
    pump(0.5)
    rig.shot(out / "curves.png")

    editor.set_write_files(False)
    schema2_shots(rig, out)
    editor.document.dirty = False
    editor.close()
    pump()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    raise SystemExit(main(sys.argv))
