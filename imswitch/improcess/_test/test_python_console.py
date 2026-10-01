"""The console's namespace over the live results list, and what a result made
in it records. Qt-free: the widget has its own tests."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, output_node, validate_graph  # noqa: E402
from imswitch.improcess.processors.python_step.console import REBOUND_NAMES, ConsoleSession  # noqa: E402
from imswitch.improcess.processors.python_step.context import ScriptError  # noqa: E402


def _result(name="rec", shape=(6, 4, 4), scales=None, unit="px"):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult(name, data, ["Z", "Y", "X"][-len(shape):], axis_scales=scales, scale_unit=unit)


def _bound_to(value, result) -> bool:
    """``value`` shows ``result``'s pixels, read-only: the namespace never hands
    out a writable input, so identity with ``result.data`` is not the test."""
    return (
        isinstance(value, np.ndarray)
        and np.shares_memory(value, result.data)
        and not value.flags.writeable
    )


class _List:
    """A stand-in for the results list the controller reads."""

    def __init__(self, selected=(), current=None):
        self.selected = list(selected)
        self.current = current
        self.published = []

    def session(self):
        return ConsoleSession(
            selected=lambda: list(self.selected),
            current=lambda: self.current,
            publish=lambda result, name: self.published.append((result, name)),
        )


def test_the_namespace_is_the_steps_less_outputs_plus_the_live_list():
    rec = _result()
    session = _List([rec], rec).session()
    assert set(session.namespace) == {
        "np", "data", "inputs", "axes", "scales", "unit", "axis", "results",
        "make_result", "make_labels", "current", "selected", "publish",
    }
    assert "outputs" not in session.namespace and "out" not in session.namespace
    assert session.namespace["np"] is np
    assert _bound_to(session.namespace["data"], rec)
    assert session.namespace["axes"] == ["Z", "Y", "X"]
    assert session.namespace["results"] == [rec]


def test_current_and_selected_answer_from_the_list():
    a, b = _result("a"), _result("b")
    live = _List([a, b], b)
    session = live.session()
    assert session.current() is b and session.selected() == [a, b]
    assert session.namespace["current"]() is b
    live.selected, live.current = [], None
    assert session.current() is None and session.selected() == []


def test_the_console_hands_out_read_only_inputs_and_publishes_independent_results():
    rec = _result()
    session = _List([rec], rec).session()
    with pytest.raises(ValueError, match="read-only"):
        exec("data[0] = 1", session.namespace)
    published = session.publish(session.namespace["data"])
    assert published.data.flags.writeable and not np.shares_memory(published.data, rec.data)
    published = session.publish(rec.data[1:])
    assert not np.shares_memory(published.data, rec.data)
    assert rec.data[0, 0, 0] == 0


def test_data_follows_the_selection_and_falls_back_to_the_current_result():
    a, b, c = _result("a"), _result("b", shape=(3, 4, 4)), _result("c")
    live = _List([a, b], c)
    session = live.session()
    assert session.namespace["results"] == [a, b] and session.bound() == [a, b]
    assert [x.shape for x in session.namespace["inputs"]] == [(6, 4, 4), (3, 4, 4)]

    live.selected = []
    assert session.refresh() == [c]                     # nothing selected: the current one
    assert _bound_to(session.namespace["data"], c)

    live.current = None
    assert session.refresh() == []
    space = session.namespace
    assert space["data"] is None and space["inputs"] == [] and space["results"] == []
    assert space["axes"] == [] and space["unit"] == "px"
    with pytest.raises(ValueError, match="no result is selected"):
        space["axis"]("Z")


def test_refresh_rebinds_only_the_reserved_names():
    live = _List([_result("a")])
    session = live.session()
    session.namespace["mine"] = 42
    session.namespace["outputs"] = "the user's own variable"
    session.namespace["data"] = "overwritten on purpose"
    live.selected = [_result("b", scales=[2.0, 0.5, 0.5], unit="um")]
    session.refresh()
    assert session.namespace["mine"] == 42
    assert session.namespace["outputs"] == "the user's own variable"
    assert isinstance(session.namespace["data"], np.ndarray)
    assert session.namespace["scales"] == [2.0, 0.5, 0.5] and session.namespace["unit"] == "um"
    assert session.namespace["axis"]("X") == 2
    assert set(REBOUND_NAMES) <= set(session.namespace)


def test_a_result_that_is_not_in_memory_is_not_read_by_following_the_selection():
    class Lazy:
        shape = (3, 2, 2)
        ndim = 3
        reads = 0

        def __array__(self, dtype=None, copy=None):
            Lazy.reads += 1
            return np.ones(self.shape, np.float32)

        def __getitem__(self, key):
            return np.ones(self.shape, np.float32)[key]

    lazy = ArrayProcessingResult("lazy", Lazy(), ["Z", "Y", "X"])
    session = _List([lazy]).session()
    session.refresh()
    session.refresh()
    assert Lazy.reads == 0 and isinstance(session.namespace["data"], Lazy)
    assert np.asarray(session.namespace["data"]).shape == (3, 2, 2)      # the user reads it when they choose


# -- publish ------------------------------------------------------------------

def test_publish_adds_a_result_with_the_selections_axes_and_scales():
    rec = _result(scales=[2.0, 0.5, 0.5], unit="um")
    live = _List([rec], rec)
    session = live.session()
    made = session.namespace["publish"](session.namespace["data"] * 2)
    assert live.published == [(made, "rec (console)")]
    assert np.array_equal(made.data, rec.data * 2)
    assert made.axis_labels == ["Z", "Y", "X"] and made.axis_scales == [2.0, 0.5, 0.5]
    assert made.scale_unit == "um" and made.metadata["operation"] == "console"

    named = session.publish(rec.data[:3], name="first three")
    assert named.name == "first three" and live.published[-1][1] == "first three"


def test_publish_of_another_dimensionality_needs_axes_and_takes_the_shared_calibration():
    rec = _result(scales=[2.0, 0.5, 0.25], unit="um")
    session = _List([rec], rec).session()
    with pytest.raises(ScriptError, match="the published array has 2 dimensions but the input has 3"):
        session.publish(rec.data.max(axis=0))
    made = session.publish(rec.data.max(axis=0), axes=["Y", "X"], name="mip")
    assert made.axis_labels == ["Y", "X"] and made.axis_scales == [0.5, 0.25]
    explicit = session.publish(rec.data.max(axis=0), axes=["Y", "X"], scales=[1.0, 1.0])
    assert explicit.axis_scales == [1.0, 1.0]


def test_publish_with_nothing_selected_needs_axes_and_like_chooses_the_reference():
    rec = _result(scales=[2.0, 0.5, 0.5], unit="um")
    live = _List([], None)
    session = live.session()
    with pytest.raises(ScriptError, match="no input to take its axes from"):
        session.publish(np.zeros((4, 4)))
    assert session.publish(np.zeros((4, 4)), axes=["Y", "X"]).axis_labels == ["Y", "X"]
    via_like = session.publish(rec.data, like=rec)
    assert via_like.axis_scales == [2.0, 0.5, 0.5] and via_like.scale_unit == "um"
    assert via_like.name == "rec (console)"


def test_publish_takes_make_labels_and_a_ready_made_result():
    rec = _result()
    live = _List([rec], rec)
    session = live.session()
    labels = session.publish(session.namespace["make_labels"]((rec.data > 10).astype(np.uint8), name="mask"))
    assert labels.kind == "labels" and labels.name == "mask"

    ready = _result("ready")
    assert session.publish(ready, name="renamed") is ready and ready.name == "renamed"
    assert [name for _r, name in live.published] == ["mask", "renamed"]


def test_publish_refuses_what_is_not_a_numeric_array_and_publishes_nothing():
    live = _List([_result()])
    session = live.session()
    with pytest.raises(ScriptError, match="the published array must be a numeric array"):
        session.publish("text")
    assert live.published == []


def test_code_typed_into_the_console_can_publish():
    rec = _result()
    live = _List([rec], rec)
    session = live.session()
    exec("x = data[::2]\npublish(x, name='every other')", session.namespace)
    assert live.published[0][1] == "every other" and live.published[0][0].data.shape == (3, 4, 4)


# -- what a console-made result records -----------------------------------------

def test_a_console_result_records_an_opaque_node_without_inputs():
    rec = _result()
    made = _List([rec], rec).session().publish(rec.data * 2)
    graph = validate_graph(graph_of(made))
    node = output_node(made)
    assert node["op"] == "opaque" and node["label"] == "Made in the console" and node["console"] is True
    assert node["inputs"] == []                                  # the console cannot know the lineage
    assert node["replayable"] is False and "console" in node["reasons"][0]
    assert any("rec" in label for label in node["selected_when_published"])
    assert len(graph["nodes"]) == 1


def test_a_result_that_already_has_a_history_keeps_it_when_published_again():
    rec = _result()
    session = _List([rec], rec).session()
    first = session.publish(rec.data * 2)
    before = graph_of(first)
    assert session.publish(first) is first
    assert graph_of(first) == before


def test_a_step_run_on_a_console_result_chains_on_its_node_and_is_not_replayable(tmp_path):
    from imswitch.improcess.processors.run import run_processor
    from imswitch.improcess.workflows import ReplayError, bootstrap_registry, workflow_from_provenance
    from imswitch.improcess.model.save_protocol import ProvenanceDocument

    rec = _result()
    made = _List([rec], rec).session().publish(rec.data * 2)
    registry = bootstrap_registry(user_plugins=False)
    processor = registry.get_processor("filter")
    results, failures = run_processor(processor, [made], type(processor).default_params(), None)
    assert failures == []
    node = output_node(results[0])
    assert node["plugin_id"] == "filter"
    nodes = graph_of(results[0])["nodes"]
    assert node["inputs"][0]["node"] in nodes and nodes[node["inputs"][0]["node"]]["op"] == "opaque"
    with pytest.raises(ReplayError) as caught:
        workflow_from_provenance(ProvenanceDocument(graph=graph_of(results[0])), registry=registry)
    assert "console" in str(caught.value)


# -- following the selection without undoing the user's own names -------------

def test_refresh_if_changed_rebinds_only_when_the_selection_moved():
    a, b = _result("a"), _result("b")
    live = _List([a], a)
    session = live.session()
    session.namespace["data"] = "the user's own data"
    assert session.refresh_if_changed() is False            # same selection: hands off
    assert session.namespace["data"] == "the user's own data"
    live.selected = [b]
    assert session.refresh_if_changed() is True
    assert _bound_to(session.namespace["data"], b)
    assert session.refresh_if_changed() is False
    live.selected, live.current = [], None
    assert session.refresh_if_changed() is True and session.namespace["data"] is None


def test_a_selection_that_changes_while_publishing_is_picked_up_at_the_next_command_not_mid_script():
    rec = _result("rec")
    live = _List([rec], rec)
    new_ones = []

    def publish(result, name):
        new_ones.append(result)
        live.selected = [result]                              # the list selects what it was given
        session.refresh_if_changed()                          # ... and its signal reaches the controller

    session = ConsoleSession(selected=lambda: list(live.selected), current=lambda: live.current, publish=publish)
    exec("first = publish(data * 2)\nsecond = publish(data * 3)", session.namespace)
    assert [np.array_equal(r.data, rec.data * k) for r, k in zip(new_ones, (2, 3))] == [True, True]
    assert _bound_to(session.namespace["data"], rec)          # still the original, through both publishes
    assert new_ones[1].name == "rec (console)"                # not "rec (console) (console)"
    assert session.refresh_if_changed() is True                # the next command sees the selection
    assert _bound_to(session.namespace["data"], new_ones[1])
