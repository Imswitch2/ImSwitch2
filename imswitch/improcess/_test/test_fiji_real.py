"""The host and session against a real Fiji (marked ``fiji``).

These start a real Fiji GUI on the screen of whoever runs them, so they are
skipped unless both ``IMSWITCH_FIJI_TEST_DIR`` (a Fiji Latest folder) and
``IMSWITCH_FIJI_TEST_PYTHON`` (an interpreter with pyimagej, numpy and, on
macOS, PyObjC; it needs no ImSwitch install, the child gets ``PYTHONPATH``) are
set. One session is shared by the module so Fiji starts once.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.fiji.convert import PROP_RESULT_UID, NotSendable, import_fiji_image, plan_send
from imswitch.improcess.fiji.protocol import HostError, resolve_launch_config
from imswitch.improcess.fiji.session import FijiDied, FijiSession
from imswitch.improcess.fiji.tracker import GRID_POLICY_VERSION, grid_preserved, history_for
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.provenance import derive_history, graph_of, output_node

FIJI_DIR = os.environ.get("IMSWITCH_FIJI_TEST_DIR")
FIJI_PYTHON = os.environ.get("IMSWITCH_FIJI_TEST_PYTHON")

pytestmark = [
    pytest.mark.fiji,
    pytest.mark.skipif(not (FIJI_DIR and FIJI_PYTHON), reason="set IMSWITCH_FIJI_TEST_DIR and IMSWITCH_FIJI_TEST_PYTHON"),
]

REPO_ROOT = Path(__file__).resolve().parents[3]
JARS_DIR = REPO_ROOT / "imswitch" / "improcess" / "fiji" / "jars"
#: Beyond int16's range, so a signed reinterpretation would show.
UINT16_HIGH = 40000


def _config():
    ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return resolve_launch_config(FIJI_DIR, ram_bytes=ram, bridge_jars=sorted(JARS_DIR.glob("*.jar")))


def _new_session() -> FijiSession:
    # No PYTHONPATH here: the session puts this ImSwitch on the host's path itself.
    return FijiSession(_config(), python=FIJI_PYTHON)


@pytest.fixture(scope="module")
def session():
    with _new_session() as started:
        yield started


def _data(shape, dtype):
    n = int(np.prod(shape))
    if np.dtype(dtype) == np.float32:
        data = (np.arange(n, dtype=np.float32) * 0.37 - 5.0).reshape(shape)
        data.flat[3] = np.nan
        return data
    top = UINT16_HIGH + 1000 if np.dtype(dtype) == np.uint16 else 250
    return ((np.arange(n) * 7) % top).astype(dtype).reshape(shape)


def _result(shape, labels, dtype, *, scales=None, channel_names=None, name="src"):
    return ArrayProcessingResult(
        name, _data(shape, dtype), list(labels), axis_scales=scales, scale_unit="um",
        metadata={"channel_names": channel_names} if channel_names else None,
    )


def _round_trip(session, source):
    plan = plan_send(source)
    image_id = session.send(plan)
    data, desc = session.fetch(image_id)
    imported = import_fiji_image(desc, data, source, sent=session.sent[image_id])
    return image_id, desc, imported.result


CASES = {
    "2d-uint8": ((12, 17), "YX", np.uint8, [0.2, 0.1], None),
    "2d-uint16-high": ((12, 17), "YX", np.uint16, [0.2, 0.1], None),
    "2d-float32-nan": ((12, 17), "YX", np.float32, [0.2, 0.1], None),
    "zyx-uint16": ((4, 12, 17), "ZYX", np.uint16, [0.5, 0.2, 0.1], None),
    "cyx-names": ((3, 12, 17), "CYX", np.uint8, [1.0, 0.2, 0.1], ["DAPI", "GFP", "RFP"]),
    "tyx-float32": ((3, 12, 17), "TYX", np.float32, [2.5, 0.2, 0.1], None),
    "tzcyx-uint8": ((2, 3, 2, 12, 17), "TZCYX", np.uint8, [4.0, 0.5, 1.0, 0.2, 0.1], ["a", "b"]),
    "xzcy-order": ((17, 3, 2, 12), "XZCY", np.uint16, [0.1, 0.5, 1.0, 0.2], ["a", "b"]),
}


@pytest.mark.parametrize("case", list(CASES))
def test_round_trip_is_exact(session, case):
    shape, labels, dtype, scales, names = CASES[case]
    source = _result(shape, labels, dtype, scales=scales, channel_names=names)
    image_id, desc, result = _round_trip(session, source)

    assert result.data.dtype == source.data.dtype
    assert np.array_equal(result.data, source.data, equal_nan=np.dtype(dtype) == np.float32)
    assert list(result.axis_labels) == [a.upper() for a in labels]
    assert np.allclose(result.axis_scales, scales)
    assert result.scale_unit == "um" and desc.x_unit == "µm"       # ImageJ's own spelling
    if names:
        assert result.metadata["channel_names"] == names
    if "T" in labels:
        assert desc.time_unit.lower().startswith("s") and desc.frame_interval == pytest.approx(scales[labels.index("T")])
    assert desc.properties[PROP_RESULT_UID] == source.result_uid


def test_a_monalisa_serialization_view_round_trips_with_channel_names_as_slice_labels(session):
    from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult

    coeffs = np.arange(1 * 2 * 4 * 2 * 5, dtype=np.float32).reshape(1, 2, 4, 2, 5)
    scan_params = {
        'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
        'directions': ['pos', 'pos', 'pos'], 'steps': ['2', '2', '1', '1'],
        'step_sizes': ['40', '80', '120', '1'], 'unidirectional': True,
    }
    labels = {'r_l_text': 'Right-Left', 'u_d_text': 'Up-Down', 'b_f_text': 'Back-Front',
              'timepoints_text': 'Timepoints', 'p_text': 'pos', 'n_text': 'neg'}
    result = MonalisaProcessingResult.from_coeffs("monalisa", coeffs, scan_params, labels)
    view = result.serialization_view()
    plan = plan_send(result)
    image_id = session.send(plan)
    data, desc = session.fetch(image_id)
    np.testing.assert_array_equal(data, np.asarray(plan.data))
    np.testing.assert_array_equal(data, np.asarray(view.data))
    assert list(desc.channel_labels) == list(view.channel_names)
    session.close_image(image_id)


def test_refusals_happen_before_any_ipc(session):
    before = [d.image_id for d in session.list_images()]
    with pytest.raises(NotSendable):
        plan_send(ArrayProcessingResult("f64", np.zeros((12, 17), np.float64), ["Y", "X"]))
    with pytest.raises(NotSendable):
        plan_send(ArrayProcessingResult("q", np.zeros((2, 12, 17), np.uint8), ["Q", "Y", "X"]))
    assert [d.image_id for d in session.list_images()] == before


def test_duplicate_is_seen_with_its_lineage_hint_and_events(session):
    source = _result((4, 12, 17), "ZYX", np.uint16, scales=[0.5, 0.2, 0.1])
    image_id = session.send(plan_send(source))
    before = {d.image_id for d in session.list_images()}
    session.events(clear=True)

    session.run_command(image_id, "Duplicate...", "title=dup duplicate")
    new = [d for d in session.list_images() if d.image_id not in before]
    assert len(new) == 1 and new[0].properties[PROP_RESULT_UID] == source.result_uid
    events = session.events()["events"]
    assert any(e["kind"] == "command" and e["command"] == "Duplicate..." and e["current_id"] == image_id for e in events)
    assert any(e["kind"] == "opened" and e["id"] == new[0].image_id for e in events)


def test_crop_imports_on_a_fresh_grid(session):
    source = _result((40, 50), "YX", np.uint16, scales=[0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.run_command(image_id, "Specify...", "width=20 height=10 x=5 y=7 slice=1")
    session.run_command(image_id, "Crop")
    data, desc = session.fetch(image_id)
    imported = import_fiji_image(desc, data, source, sent=session.sent[image_id], preserves_grid=True)
    assert imported.result.data.shape == (10, 20)
    assert np.array_equal(imported.result.data, source.data[7:17, 5:25])
    assert imported.grid == "fresh"


def test_ping_reports_a_gui_fiji_with_the_configured_heap(session):
    info = session.ping_info
    assert info.headless is False and info.java.startswith("21")
    assert info.max_heap_mb == session.config.max_heap_mb


def test_fetching_a_closed_image_is_a_host_error(session):
    image_id = session.send(plan_send(_result((12, 17), "YX", np.uint8, scales=[0.2, 0.1])))
    session.close_image(image_id)
    assert image_id not in [d.image_id for d in session.list_images()]
    with pytest.raises(HostError, match=str(image_id)):
        session.fetch(image_id)
    assert session.state == "ready"


def test_a_killed_host_is_reported_dead():
    second = _new_session()
    try:
        second.start()
        second.process.kill()
        with pytest.raises(FijiDied):
            second.list_images()
        assert second.state == "dead"
    finally:
        second.stop()


#: Generous allowance for ImageJ's own File > Quit to take the host down:
#: Fiji's disposal took ~0.2 s in P0, so this is the stop bound.
QUIT_ALLOWANCE_S = 10.0


def test_closing_fiji_itself_is_noticed_without_a_request():
    """File > Quit (what closing Fiji's window does) ends the host; the session must notice."""
    import time
    from imswitch.improcess.fiji.session import STOP_TIMEOUT_S

    codes = []
    config = _config()
    second = FijiSession(config, python=FIJI_PYTHON, on_exit=codes.append)
    try:
        second.start()                               # no images are open, so Quit asks nothing
        try:
            second.run_command(None, "Quit")
            outcome = "replied"
        except FijiDied:
            outcome = "connection broke"
        print(f"ImageJ Quit: {outcome}")
        deadline = time.monotonic() + max(QUIT_ALLOWANCE_S, STOP_TIMEOUT_S)
        while (second.state != "dead" or not codes) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert second.state == "dead" and len(codes) == 1
        assert codes[0] == second.process.returncode
        assert second.process.poll() is not None
    finally:
        second.stop()


def test_a_run_command_event_carries_options_end_plugin_and_recorder_offset(session):
    source = _result((40, 50), "YX", np.uint16, scales=[0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.events(clear=True)
    session.run_command(image_id, "Gaussian Blur...", "sigma=2")

    reply = session.events()
    command = next(e for e in reply["events"] if e["kind"] == "command" and e["command"] == "Gaussian Blur...")
    assert command["options"].strip() == "sigma=2" and command["current_id"] == image_id
    assert command["end"] is not None and command["end"] >= command["time"]
    assert command["plugin_class"] and command["plugin_jar"] and command["plugin_jar"].endswith(".jar")
    assert isinstance(command["recorder_offset"], int) and command["recorder_offset"] <= len(reply["recorder"])
    assert any(e["kind"] == "updated" and e["id"] == image_id and e["changes"] is True for e in reply["events"])
    # The scripted call is not in the recorder (P0): the offset only marks where it would start.
    assert 'run("Gaussian Blur...", "sigma=2")' not in reply["recorder"]
    seqs = [e["seq"] for e in reply["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_display_only_actions_fire_updated_with_the_change_flag_unset(session):
    """The tracker's "possible unrecorded change" rule rests on this observation.

    ImageJ1 fires ``imageUpdated`` for a slice change and for Enhance Contrast
    (which only changes display ranges), but ``imp.changes`` stays False. So an
    ``updated`` alone is weak evidence; ``changes`` turning True without a
    command is the strong one.
    """
    source = _result((4, 40, 50), "ZYX", np.uint16, scales=[0.5, 0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.events(clear=True)
    for command, options in (("Next Slice [>]", ""), ("Enhance Contrast", "saturated=0.35")):
        session.run_command(image_id, command, options)
        events = session.events(clear=True)["events"]
        updated = [e for e in events if e["kind"] == "updated" and e["id"] == image_id]
        assert updated and all(e["changes"] is False for e in updated), (command, events)
    # Contrast is display-only: the pixels are untouched.
    data, _ = session.fetch(image_id)
    assert np.array_equal(data[0, :, 0], source.data)


def _import_with_history(session, image_id, source):
    """What the controller's import job does, without the controller."""
    data, desc = session.fetch(image_id)
    log = session.events()
    history = history_for(
        image_id, log["events"], log["recorder"], sent_ids=set(session.sent),
        fetched_fingerprint=session.fetch_fingerprints.get(image_id),
    )
    preserves, reason = grid_preserved(history)
    imported = import_fiji_image(
        desc, data, source, sent=session.sent.get(image_id) or next(iter(session.sent.values())),
        preserves_grid=preserves,
        details={"fiji_history": history.to_details(), "grid_policy": GRID_POLICY_VERSION, "grid_policy_reason": reason},
    )
    return history, reason, imported


def test_a_blur_in_place_comes_back_on_the_inherited_grid_with_its_history(session):
    source = _result((4, 20, 30), "ZYX", np.uint16, scales=[0.5, 0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.run_command(image_id, "Gaussian Blur...", "sigma=2 stack")
    history, reason, imported = _import_with_history(session, image_id, source)

    assert history.origin == "sent" and history.origin_id == image_id and reason == ""
    blur = next(s for s in history.steps if s.command == "Gaussian Blur...")
    assert blur.options_source == "macro" and blur.options.strip() == "sigma=2 stack" and blur.in_place
    assert blur.plugin_jar and blur.completeness == "recorded"
    assert history.unrecorded == "none observed" and history.completeness == "recorded"
    assert imported.grid == "inherit"
    assert "history" not in imported.result.metadata["fiji"]
    labels = [step["label"] for step in derive_history(graph_of(imported.result))]
    assert labels[-2].startswith("Fiji: Gaussian Blur... (") and "sigma=2" in labels[-2]
    assert labels[-1] == "Imported from Fiji"


def test_a_duplicate_blurred_in_fiji_comes_back_on_the_inherited_grid_in_two_steps(session):
    source = _result((4, 20, 30), "ZYX", np.uint16, scales=[0.5, 0.2, 0.1])
    image_id = session.send(plan_send(source))
    before = {d.image_id for d in session.list_images()}
    session.run_command(image_id, "Duplicate...", "title=dup duplicate")
    (dup,) = [d.image_id for d in session.list_images() if d.image_id not in before]
    session.run_command(dup, "Gaussian Blur...", "sigma=1 stack")
    history, reason, imported = _import_with_history(session, dup, source)

    assert [(s.command, s.in_place) for s in history.steps] == [("Duplicate...", False), ("Gaussian Blur...", True)]
    assert history.steps[0].input_id == image_id and history.steps[0].output_id == dup
    assert history.origin_id == image_id and reason == "" and imported.grid == "inherit"


def test_a_crop_in_fiji_is_fresh_and_the_reason_names_crop(session):
    source = _result((40, 50), "YX", np.uint16, scales=[0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.run_command(image_id, "Specify...", "width=20 height=10 x=5 y=7 slice=1")
    session.run_command(image_id, "Crop")
    history, reason, imported = _import_with_history(session, image_id, source)

    assert [s.command for s in history.steps][-1] == "Crop"
    assert "Specify" in reason or "Crop" in reason
    assert imported.grid == "fresh"
    assert output_node(imported.result)["details"]["fiji_history"]["origin"] == "sent"


def test_scrolling_after_a_filter_does_not_degrade_the_history(session):
    source = _result((4, 20, 30), "ZYX", np.uint16, scales=[0.5, 0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.run_command(image_id, "Gaussian Blur...", "sigma=2 stack")
    session.run_command(image_id, "Next Slice [>]")
    history, reason, imported = _import_with_history(session, image_id, source)

    assert session.fetch_fingerprints[image_id]
    assert history.unrecorded == "none observed" and history.completeness == "recorded", history.summary()
    assert reason == "" and imported.grid == "inherit"
    assert [s.command for s in history.steps][-1] == "Next Slice [>]"


def test_a_request_with_empty_options_is_a_recorded_step(session):
    source = _result((20, 30), "YX", np.uint16, scales=[0.2, 0.1])
    image_id = session.send(plan_send(source))
    session.run_command(image_id, "8-bit")
    history, reason, imported = _import_with_history(session, image_id, source)
    (step,) = history.steps
    assert step.command == "8-bit" and step.options == "" and step.options_source == "macro"
    assert step.completeness == "recorded" and history.completeness == "recorded"
