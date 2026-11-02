"""The workflow editor and the workflow schema live in imcommon so that
ImControl can open the editor on its own graphs: importing them must not pull
in ImProcess or ImControl, nor napari (ImProcess's viewer stack). Qt and
h5py are not forbidden: every ImSwitch module has them, and keeping them out
would mean making ``imcommon``'s package imports lazy, which is not this
boundary's business."""
import json
import subprocess
import sys

import pytest

_FORBIDDEN = ("imswitch.improcess", "imswitch.imcontrol", "napari")

_PROBE = """
import json, sys
import {module}
loaded = sorted(name for name in sys.modules
                if any(name == f or name.startswith(f + ".") for f in {forbidden!r}))
print(json.dumps(loaded))
"""


def _imports_of(module, forbidden):
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE.format(module=module, forbidden=forbidden)],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", [
    "imswitch.imcommon.model.kinds",
    "imswitch.imcommon.model.param_spec",
    "imswitch.imcommon.model.workflow.steps",
    "imswitch.imcommon.model.workflow.sources",
    "imswitch.imcommon.model.workfloweditor",
    "imswitch.imcommon.model.workfloweditor.session",
])
def test_the_model_side_imports_no_improcess(module):
    assert _imports_of(module, _FORBIDDEN) == []


@pytest.mark.parametrize("module", [
    "imswitch.imcommon.view.workfloweditor",
    "imswitch.imcommon.view.workfloweditor.canvas",
])
def test_the_view_imports_no_improcess(module):
    assert _imports_of(module, _FORBIDDEN) == []


def test_the_old_improcess_paths_still_resolve_to_the_moved_code():
    from imswitch.imcommon.model.workfloweditor import document as new_document
    from imswitch.imcommon.model.workflow import steps as new_steps
    from imswitch.improcess.model.workfloweditor import document as old_document
    from imswitch.improcess.workflows import steps as old_steps

    assert old_document.WorkflowDocument is new_document.WorkflowDocument
    assert old_steps.Workflow is new_steps.Workflow and old_steps.Process is new_steps.Process
