"""Interactive runs in the workflow editor, through the real WorkflowController
and its worker: boxes show state, timing and output; a change outdates exactly
what follows and *Run to here* recomputes only that; Stop works inside a slow
processing step; a failing Python step shows its line; results leave the editor
only on request (a preview slot, or Pin); and every file a cached result needs
stays open exactly as long as it is needed."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.improcess.model.workfloweditor import WorkflowDocument  # noqa: E402
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry  # noqa: E402
from imswitch.improcess.workflows.steps import Save  # noqa: E402

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
sys.path.insert(0, str(_EXAMPLES))

from _synthetic import write_synthetic_recording  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


@pytest.fixture
def rig(qapp, monkeypatch, tmp_path):
    """A controller with a stand-in channel, its editor, and a recording."""
    from imswitch.improcess.controller.WorkflowController import WorkflowController
    from imswitch.improcess.model import folder_preferences
    from imswitch.improcess.model.workfloweditor import folders

    monkeypatch.setattr(folders, "workflows_directory", lambda create=True: str(tmp_path / "workflows"))
    monkeypatch.setattr(folder_preferences, "options_file_path", lambda: str(tmp_path / "options.json"))
    loaded = []
    comm = SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append((n, r))),
        sigWorkflowPreview=_Signal(),
        getAllResults=lambda: [r for _n, r in loaded],
        getSelectedResults=lambda: list(loaded),
    )
    controller = WorkflowController(
        comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=0: None), SimpleNamespace(getActiveResult=lambda: None),
        registry_factory=lambda: bootstrap_registry(user_plugins=False),
    )
    editor = controller.openEditor()
    editor.resize(1500, 900)
    recording = str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))
    yield SimpleNamespace(controller=controller, editor=editor, comm=comm, loaded=loaded, recording=recording,
                          tmp=tmp_path, app=qapp)
    controller.cancelRun(5000)
    if controller._editor is not None:
        editor.document.dirty = False
        editor.close()
    qapp.processEvents()


def _wait(rig, timeout=60.0):
    deadline = time.time() + timeout
    while rig.controller._thread is not None and time.time() < deadline:
        rig.app.processEvents()
        time.sleep(0.005)
    for _ in range(5):
        rig.app.processEvents()
    assert rig.controller._thread is None, "the run did not end"


def _wait_for(rig, condition, timeout=20.0):
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        rig.app.processEvents()
        time.sleep(0.005)
    assert condition()


def _diamond(rig):
    editor = rig.editor
    assert editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    editor.bind_source("raw", rig.recording)
    return editor


def _status(editor, step_id):
    node = editor.scene.node(step_id)
    return node.run.status if node.run is not None else ""


def test_a_run_fills_every_box_and_a_change_reruns_only_what_follows(rig):
    editor = _diamond(rig)
    assert editor.run()
    _wait(rig)
    assert {s: _status(editor, s) for s in editor.scene.nodes} == {
        "raw": "done", "rec": "done", "split": "done", "smooth": "done", "sharp": "done", "merge": "done",
        "out": "would-write"}
    for step_id in ("rec", "split", "smooth", "merge"):
        node = editor.scene.node(step_id)
        assert node.run.thumbnail is not None and " ms" in node.run.footer, step_id
    assert editor.scene.node("out").run.summary == ("would write", "cells_diamond.h5")
    assert "Ran 6 steps" in editor.statusBar().currentMessage()

    editor.select_step("smooth")
    editor.inspector.param_form.set_value("radius", 4.0)
    assert [s for s in editor.scene.nodes if _status(editor, s) == "outdated"] == ["smooth", "merge"]
    assert editor.chip.text() == "↻  2 steps outdated"

    editor.select_step("merge")
    assert editor.run_to_selected()
    _wait(rig)
    assert _status(editor, "smooth") == _status(editor, "merge") == "done"
    message = editor.statusBar().currentMessage()
    assert "Ran 2 steps" in message and "3 from the cache" in message
    assert editor.chip.text().startswith("✓")
    assert editor.run_to_selected() is False                    # nothing left to do for 'merge'
    assert "up to date" in editor.statusBar().currentMessage()


def test_the_inspector_shows_the_selected_output_and_the_actions_work(rig):
    editor = _diamond(rig)
    editor.run()
    _wait(rig)
    editor.select_step("split")
    panel = editor.inspector.output
    assert panel.status.text().startswith("Done in")
    assert [panel.port.itemData(i) for i in range(panel.port.count())] == ["C0", "C1"]
    _wait_for(rig, lambda: panel.image.pixmap() is not None and not panel.image.pixmap().isNull())
    assert panel.view_button.isEnabled() and panel.pin_button.isEnabled()

    panel.sigShowInViewer.emit("C1")
    shown, name = rig.comm.sigWorkflowPreview.calls[-1]
    assert shown is editor.session.result(editor.document, "split", "C1") and "preview" in name
    assert rig.loaded == []                                   # a preview is not a result in the list

    panel.sigPin.emit("C0")
    assert [name for name, _r in rig.loaded] == ["split-process-merge:split.C0"]


def test_closing_hands_the_pinned_results_files_to_the_controller(rig):
    editor = _diamond(rig)
    editor.run()
    _wait(rig)
    assert editor.pin("rec")
    handles = editor.session.open_handles()
    assert len(handles) == 1
    editor.document.dirty = False
    editor.close()
    rig.app.processEvents()
    controller = rig.controller
    assert controller._editor is None
    assert [len(h) for _uids, h in controller._retained] == [1]
    assert controller.releaseUnusedSources() == 0               # the pinned result is still loaded
    rig.loaded.clear()
    assert controller.releaseUnusedSources() == 1               # gone: its file is closed


def test_stop_ends_a_slow_processing_step(rig):
    editor = rig.editor
    editor.load_document(WorkflowDocument(Workflow("slow", [
        Source("raw", path=rig.recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "import time\nfor _ in range(3000):\n    time.sleep(0.01)\nout = data\n"},
                inputs=["rec"]),
    ]), catalog=editor.catalog), confirm=False)
    assert editor.run()
    _wait_for(rig, lambda: _status(editor, "py") == "running")
    started = time.time()
    editor.cancel_run()
    _wait(rig, timeout=15)
    assert time.time() - started < 10
    assert _status(editor, "py") != "done" and _status(editor, "rec") == "done"
    assert editor.statusBar().currentMessage().startswith("Stopped")


def test_a_failing_python_step_shows_its_line_and_what_it_printed(rig):
    editor = rig.editor
    editor.load_document(WorkflowDocument(Workflow("broken", [
        Source("raw", path=rig.recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "print('the shape is', data.shape)\ny = 1\nraise ValueError('the matrix is for 2 channels')\n"},
                inputs=["rec"]),
        Save("out", input="py"),
    ]), catalog=editor.catalog), confirm=False)
    editor.run()
    _wait(rig)
    node = editor.scene.node("py")
    assert node.run.status == "failed" and node.run.error.startswith("line 3:")
    assert _status(editor, "rec") == "done" and _status(editor, "out") == ""
    assert editor.chip.text() == "✕  Stopped at 'py'"
    assert editor.selected_step_id() == "py"
    panel = editor.inspector.output
    assert "line 3" in panel.error.text() and "the shape is" in panel.printed.toPlainText()
    editor.inspector.python_editor.setText("out = data.copy()\n")
    editor.inspector._commit_code()
    editor.run()
    _wait(rig)
    assert _status(editor, "py") == "done" and "1 from the cache" in editor.statusBar().currentMessage()


def test_a_run_asked_for_during_a_run_waits_and_picks_up_the_edits(rig):
    editor = rig.editor
    editor.load_document(WorkflowDocument(Workflow("two", [
        Source("raw", path=rig.recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "import time\ntime.sleep(0.4)\nout = data.copy()\n"}, inputs=["rec"]),
        Process("blur", "filter", inputs=["py"]),
    ]), catalog=editor.catalog), confirm=False)
    assert editor.run()
    _wait_for(rig, lambda: _status(editor, "py") == "running")
    editor.edit("radius", lambda: editor.document.set_param("blur", "radius", 3.0))
    assert editor.run() is True and editor._queued is not None
    # The first run files 'blur' under its old radius (outdated); only the
    # queued second run makes it done.
    _wait_for(rig, lambda: rig.controller._thread is None and editor._queued is None
              and _status(editor, "blur") == "done", timeout=30)
    _wait(rig)
    assert _status(editor, "blur") == "done"
    assert editor.session.states(editor.document)["blur"].status == "done"


def test_write_files_writes_into_the_chosen_folder(rig):
    editor = _diamond(rig)
    out = rig.tmp / "written"
    out.mkdir()
    assert editor.set_write_files(True, folder=str(out), overwrite=True)
    editor.run()
    _wait(rig)
    assert (out / "cells_diamond.h5").exists()
    assert editor.scene.node("out").run.summary == ("wrote", "cells_diamond.h5")
    assert editor.set_write_files(False)


def test_a_source_without_a_recording_says_what_to_do(rig):
    editor = rig.editor
    editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    assert editor.run() is False
    assert "has no recording" in editor.statusBar().currentMessage()
    assert editor.selected_step_id() == "raw"
    node = editor.scene.node("raw")
    editor._on_drop_files([rig.recording], node.mapToScene(QtCore.QPointF(100, 30)))
    assert editor.session.source_bindings == {"raw": rig.recording}
    assert editor.document.step("raw").source.path is None              # the file keeps its own setting
    assert editor.scene.node("raw").run.summary[0] == "runs on cells.h5"


def test_the_open_recording_can_be_used(rig):
    editor = rig.editor
    editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    rig.controller._onCurrentData(SimpleNamespace(dataPath=rig.recording, datasetName=None))
    editor.select_step("raw")
    buttons = editor.inspector.binding_area.findChildren(QtWidgets.QPushButton)
    use = next(b for b in buttons if b.text() == "Use the open recording")
    assert use.isEnabled()
    use.click()
    assert editor.session.source_bindings == {"raw": rig.recording}


def test_a_workflow_preview_is_shown_without_entering_the_list_or_taking_its_contrast():
    from imswitch.improcess._test.test_display_levels_memory import _controller, _dim, _View

    view = _View()
    controller = _controller(view)
    listed = _dim("listed", 1.0)
    view.add(listed)
    controller.listItemChanged()
    view.userDragsSlider(2.0, 9.0)
    controller.showWorkflowPreview(_dim("preview", 10.0), "preview")
    assert view.imgLayer.name == "preview"
    assert view.getAllItemDatas() == [("listed", listed)]
    view.userDragsSlider(50.0, 80.0)                     # a contrast set on the preview...
    controller.listItemChanged()                          # ...is not the listed result's
    assert view.imgLayer.name == "listed"
    assert tuple(view.imgLayer.contrast_limits) == (2.0, 9.0)
