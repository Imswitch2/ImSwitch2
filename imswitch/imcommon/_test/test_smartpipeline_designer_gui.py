"""The smart-pipeline designer (the shared workflow editor on the pipeline
step kinds), offscreen: a gate's or an action's parameter is exposed from
its row's menu, shows in the Controls panel, is renamed, relabelled and
un-exposed there, and the saved file carries it."""
import pytest

pytest.importorskip("pytestqt")

from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.model.smartpipeline import (  # noqa: E402
    Action,
    ActionCatalog,
    ActionSpec,
    Gate,
    Live,
    SmartPipelineDocument,
    Transform,
)
from imswitch.imcommon.model.smartpipeline.descriptors import pipeline_descriptors  # noqa: E402
from imswitch.imcommon.model.workflow.steps import Workflow  # noqa: E402
from imswitch.imcommon.model.workfloweditor.catalog import Catalog  # noqa: E402
from imswitch.imcommon.model.workfloweditor.document import WorkflowDocument  # noqa: E402
from imswitch.imcommon.model.workfloweditor.preferences import EditorPreferences  # noqa: E402

ACTIONS = ActionCatalog([
    ActionSpec("move-to", accepts=("points",), params={"positioner": "", "frame": "", "deadline_s": 10.0},
               frame_param="frame"),
])


def _catalog():
    steps = pipeline_descriptors(detectors=("Camera",), transforms=("identity",), actions=ACTIONS)
    return Catalog(registry=None, processors=(), reconstructors=(), save_formats=(), path_placeholders=(),
                   steps=steps)


@pytest.fixture
def editor(qtbot, tmp_path):
    from imswitch.imcommon.view.workfloweditor.editor import MainWindow

    window = MainWindow(_catalog(), start_folder=tmp_path, preferences=EditorPreferences(tmp_path / "o.json"),
                        title="Smart pipeline designer")
    window.resize(1400, 900)
    window.show()
    workflow = Workflow("p", [
        Live("cam", detector="Camera"),
        Gate("rest", "min-interval", params={"seconds": 5.0}, inputs=["cam"]),
        Transform("tf", "identity", inputs=["rest"]),
        Action("mv", "move-to", {"positioner": "XY", "frame": "stage:XY"}, inputs=["tf"]),
    ])
    window.load_document(WorkflowDocument(workflow, catalog=_catalog()), confirm=False)
    window.refresh()
    yield window
    window.document.dirty = False
    window.close()


def test_a_gate_parameter_is_exposed_renamed_relabelled_and_unexposed(editor, qtbot, tmp_path):
    editor.select_step("rest")
    form = editor.inspector.param_form
    assert form.context_actions("seconds") == {"expose": "Expose as control…"}
    assert editor.inspector.expose_param("seconds", "cooldown")
    assert editor.document.exposed("rest") == {"seconds": "cooldown"}
    assert editor.inspector.param_form.context_actions("seconds") == {"unexpose": "Stop exposing (control 'cooldown')"}
    editor.select_step("mv")
    assert editor.inspector.expose_param("deadline_s", "deadline")
    assert editor.document.step("mv").params["deadline_s"] == 10.0
    # the Controls panel (nothing selected): the value, rename, stop exposing
    editor.scene.clearSelection()
    QtWidgets.QApplication.processEvents()
    assert set(editor.inspector.control_forms) == {"cooldown", "deadline"}
    editor.inspector.control_forms["cooldown"].set_value("cooldown", 2.0)
    assert editor.document.step("rest").params["seconds"] == 2.0
    assert editor.inspector.rename_control("cooldown", "cooldown_s")
    editor.document.set_control_label("cooldown_s", "Min. time between scans")
    editor.inspector.control_buttons["deadline"]["stop"].click()
    QtWidgets.QApplication.processEvents()
    assert [c.name for c in editor.document.controls] == ["cooldown_s"]
    # saved: a schema-2 pipeline file the panel reads back
    path = tmp_path / "p.json"
    SmartPipelineDocument(editor.document.workflow).save(path)
    loaded = SmartPipelineDocument.load(path)
    assert [(c.name, c.targets, c.label) for c in loaded.workflow.controls] == [
        ("cooldown_s", ["rest.seconds"], "Min. time between scans")]
