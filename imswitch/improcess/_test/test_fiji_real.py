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
from imswitch.improcess.model.array_result import ArrayProcessingResult

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
