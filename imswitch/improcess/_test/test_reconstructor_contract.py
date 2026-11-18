"""Every reconstructor states how it takes its input, and is treated the same way.

The contract (``Reconstructor.core_dims_in`` / ``reads_recording_file``) is
what decides, for *every* reconstructor alike, whether it is auto-indexed
over a time-lapse, whether it can take a result of another step, and what a
refusal says. These tests run over the whole registry, so a reconstructor
added later is held to the same rules without anyone writing a test for it:

* it must declare ``core_dims_in`` (a tuple of axes, or ``WHOLE_INPUT``);
  ``None`` is only tolerated for third-party plugins written before this;
* one that reads its recording file takes only a source, and is refused the
  same way by ``check_input``, workflow validation and the editor;
* one with core dims is looped over a declared leading axis and handed an
  input of its own rank unchanged; ``WHOLE_INPUT`` is never looped;
* guessed axis labels (a raw file that names none) never start a loop;
* every reconstruction goes through ``run_reconstruction`` (one provenance
  node), so nothing in production calls ``process()`` around it.
"""

import os
import re
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model import iteration  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.core_dims import ANY_AXIS, WHOLE_INPUT, declared  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.model.result_source import data_obj_from_result  # noqa: E402
from imswitch.improcess.reconstructors.base import Reconstructor, recording_file_refusal  # noqa: E402
from imswitch.improcess.reconstructors.run import run_reconstruction  # noqa: E402
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry, validate  # noqa: E402

IMPROCESS = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _all(registry):
    return sorted(registry.reconstructors(), key=lambda plugin: plugin.id)


def _ids(registry):
    return [plugin.id for plugin in _all(registry)]


REGISTRY = bootstrap_registry(user_plugins=False)
ALL_IDS = [plugin.id for plugin in sorted(REGISTRY.reconstructors(), key=lambda plugin: plugin.id)]
LOOPING = [pid for pid in ALL_IDS if REGISTRY.get_reconstructor(pid).core_dims_in]
WHOLE = [pid for pid in ALL_IDS if REGISTRY.get_reconstructor(pid).core_dims_in == WHOLE_INPUT]
FILE_READING = [pid for pid in ALL_IDS if REGISTRY.get_reconstructor(pid).reads_recording_file]


def _label_for(entry: str) -> str:
    """An axis label that satisfies one core-dims entry."""
    first = str(entry).split("|")[0].strip()
    return "Frame" if first == ANY_AXIS else first


def _result(labels, shape):
    data = np.random.default_rng(0).random(shape).astype(np.float32) + 1.0
    return ArrayProcessingResult("input", data, list(labels))


# -- the declaration ------------------------------------------------------------------

def test_the_registry_holds_every_builtin(registry):
    # A new built-in shows up here; the rules below then apply to it.
    assert len(ALL_IDS) >= 12 and "snouty" in ALL_IDS and "monalisa" in ALL_IDS


@pytest.mark.parametrize("plugin_id", ALL_IDS)
def test_every_reconstructor_declares_its_input(plugin_id):
    plugin = REGISTRY.get_reconstructor(plugin_id)
    assert declared(plugin.core_dims_in), (
        f"{plugin_id}: set core_dims_in -- a tuple of the axes one process() call consumes, "
        "or WHOLE_INPUT -- see Reconstructor.core_dims_in"
    )
    assert isinstance(plugin.core_dims_in, tuple)
    assert all(isinstance(entry, str) and entry.strip() for entry in plugin.core_dims_in)
    assert isinstance(plugin.reads_recording_file, bool)


@pytest.mark.parametrize("plugin_id", FILE_READING)
def test_a_file_reading_reconstructor_is_never_looped(plugin_id):
    # It reads a file, not an array: there is nothing to slice.
    assert REGISTRY.get_reconstructor(plugin_id).core_dims_in == WHOLE_INPUT


# -- a result into a file-reading reconstructor: one refusal, everywhere -----------------

@pytest.mark.parametrize("plugin_id", FILE_READING)
def test_a_file_reading_reconstructor_refuses_a_result_the_same_way_everywhere(plugin_id, tmp_path):
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog
    from imswitch.improcess.model.workfloweditor.document import DocumentError
    from imswitch.improcess.model.workfloweditor.nodes import can_feed, candidates, kinds_of

    plugin = REGISTRY.get_reconstructor(plugin_id)
    message = recording_file_refusal(plugin)
    bridged = data_obj_from_result(_result(["Z", "Y", "X"], (2, 8, 8)))
    # 1. before a run
    assert plugin.check_input(bridged, {}) == [message]
    with pytest.raises(Exception, match="reads its recording file"):
        run_reconstruction(plugin, bridged, type(plugin).default_params())
    # 2. workflow validation (a source is still fine)
    workflow = Workflow("w", [
        Source("raw", path=str(tmp_path / "x.h5")),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "out = data\n"}, inputs=["view"]),
        Reconstruct("rec", plugin_id, inputs=["py"]),
        Reconstruct("direct", plugin_id, inputs=["raw"]),
    ])
    issues = validate(workflow, REGISTRY)
    assert [i.message for i in issues if i.step == "rec" and "recording file" in i.message] == [message]
    assert not [i for i in issues if i.step == "direct" and "recording file" in i.message]
    # 3. the editor: no cable, no quick-add offer
    document = WorkflowDocument(workflow, catalog=build_catalog(REGISTRY))
    assert not can_feed(document, "py", "rec") and can_feed(document, "raw", "direct")
    with pytest.raises(DocumentError, match="reads its recording file"):
        document.connect("py", "rec")
    kind = kinds_of(document)["py"]
    offered = [c.plugin_id for c in candidates(document.catalog, kind, producer=document.step("py"))
               if c.step_kind == "reconstruct"]
    assert plugin_id not in offered


# -- looping: the same rules for every reconstructor with core dims ----------------------

@pytest.mark.parametrize("plugin_id", LOOPING)
def test_a_declared_leading_axis_is_looped_and_own_rank_is_not(plugin_id):
    plugin = REGISTRY.get_reconstructor(plugin_id)
    core = [_label_for(entry) for entry in plugin.core_dims_in]
    shape = tuple(6 if i == 0 else 8 for i in range(len(core)))
    own = data_obj_from_result(_result(core, shape))
    assert not iteration.plan(plugin, own.axis_labels, own.data_handle.shape, {}).iterates
    lapse = data_obj_from_result(_result(["T", *core], (3, *shape)))
    match = iteration.plan(plugin, lapse.axis_labels, lapse.data_handle.shape, {})
    assert match.loop_labels == ("T",) and match.loop_shape == (3,)
    assert iteration.iterated_over(match) == {"T": 3}
    # Asked not to, it is handed the whole stack.
    assert not iteration.plan(plugin, lapse.axis_labels, lapse.data_handle.shape, {"_iterate": False}).iterates
    # The axes it needs at the end must be there.
    shuffled = data_obj_from_result(_result(["T", *reversed(core)], (3, *shape)))
    assert any("works on the last" in p for p in plugin.check_input(shuffled, {}))


@pytest.mark.parametrize("plugin_id", LOOPING)
def test_guessed_labels_never_start_a_loop(plugin_id):
    # A raw file that names no axes gets labels from its rank alone; those
    # must not decide which axis is time. It is handed over whole, as before.
    plugin = REGISTRY.get_reconstructor(plugin_id)
    rank = len(plugin.core_dims_in) + 1
    labels = ["T", "Z", "C", "Y", "X"][-rank:]
    assert not iteration.plan(plugin, labels, (2,) * rank, {}, labels_declared=False).iterates


@pytest.mark.parametrize("plugin_id", WHOLE)
def test_whole_input_is_never_looped(plugin_id):
    plugin = REGISTRY.get_reconstructor(plugin_id)
    assert not iteration.plan(plugin, ["T", "C", "Z", "Y", "X"], (2, 2, 2, 8, 8), {}).iterates


def test_an_undeclared_plugin_keeps_working_as_whole_input():
    # A third-party plugin written before the contract: one call, no loop,
    # one provenance node, exactly as before.
    class Old(Reconstructor):
        name = "Old"
        id = "test-old"
        calls = []

        def make_param_widget(self, parent):
            return None

        def make_metadata_dialog(self, parent):
            return None

        def process(self, data_obj, params, context=None):
            type(self).calls.append(np.shape(data_obj.data))
            return ArrayProcessingResult("old", np.asarray(data_obj.data), list(data_obj.axis_labels))

    plugin = Old()
    assert plugin.core_dims_in is None and not declared(plugin.core_dims_in)
    made = run_reconstruction(plugin, _result(["T", "Z", "Y", "X"], (3, 2, 8, 8)), {})
    assert Old.calls == [(3, 2, 8, 8)]
    node = output_node(made.result)
    assert node["op"] == "reconstruct" and "iterated_over" not in node


# -- one entry point ---------------------------------------------------------------------

_ALLOWED = {
    "reconstructors/run.py",           # the entry point
    "model/iteration.py",              # its per-index loop
    "reconstructors/monalisa/reconstructor.py",   # delegates to its own engines
}


def test_nothing_in_production_calls_process_around_run_reconstruction():
    pattern = re.compile(r"\b(?:reconstructor|plugin|recon|_reconstructor\(\)|Reconstructor\(\))\s*\.process\(")
    offenders = []
    for path in IMPROCESS.rglob("*.py"):
        relative = path.relative_to(IMPROCESS).as_posix()
        if relative.startswith("_test/") or relative in _ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == [], "call run_reconstruction (it records provenance and auto-indexes):\n" + "\n".join(offenders)


@pytest.mark.parametrize("plugin_id", ["snouty", "snouty-projections"])
def test_the_snouty_family_loops_over_time_identically(plugin_id):
    """Auto-indexed over T == each timepoint alone, stacked; one node."""
    from imswitch.improcess._test.test_auto_indexing import SNOUTY

    plugin = REGISTRY.get_reconstructor(plugin_id)
    rng = np.random.default_rng(3)
    volumes = [rng.random((8, 16, 16)).astype(np.float32) * 100 + 101 for _ in range(2)]
    lapse = ArrayProcessingResult("lapse", np.stack(volumes), ["T", "Z", "Y", "X"])
    made = run_reconstruction(plugin, lapse, dict(SNOUTY))
    assert np.shape(made.result.data)[0] == 2
    for t, volume in enumerate(volumes):
        alone = plugin.process(data_obj_from_result(ArrayProcessingResult("t", volume, ["Z", "Y", "X"])),
                               dict(SNOUTY))
        np.testing.assert_allclose(np.asarray(made.result.data)[t], np.asarray(alone.data))
    nodes = [n for n in graph_of(made.result)["nodes"].values() if n["op"] == "reconstruct"]
    assert len(nodes) == 1 and nodes[0]["iterated_over"] == {"T": 2} and nodes[0]["plugin_id"] == plugin_id
