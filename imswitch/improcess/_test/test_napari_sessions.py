"""Endpoint sessions own exactly what they created, and clean up once."""

import pytest

from imswitch.improcess.model.napari_endpoints import NapariEndpoint
from imswitch.improcess.model.napari_sessions import SessionError, SessionRegistry


class _Result:
    def __init__(self, name="recon", uid="r-1"):
        self.name = name
        self.result_uid = uid


def _endpoint(lane="dock"):
    if lane == "dock":
        return NapariEndpoint(id="p:w", label="P", lane="dock", plugin_name="p", widget_name="W")
    return NapariEndpoint(id="p:r", label="P reader", lane=lane, plugin_name="p",
                          reader_plugin="p", export_format="hdf5", kinds=("image",), verified=True)


def test_a_session_lives_from_pending_to_open_to_closed(tmp_path):
    registry = SessionRegistry(temp_root=tmp_path)
    session = registry.open(_endpoint(), _Result())
    assert session.state == "pending" and session.result_uid == "r-1"
    layer_a, layer_b = object(), object()
    registry.mark_open(session, layers=[layer_a, layer_b], dock="dock")
    assert session.is_open and registry.sessions() == [session]
    registry.close(session)
    assert session.state == "closed" and session.layers == [] and session.dock is None
    assert registry.sessions() == []


def test_ownership_is_by_object_identity_not_uid():
    registry = SessionRegistry()
    first = registry.open(_endpoint(), _Result(uid="same"))
    second = registry.open(_endpoint(), _Result(uid="same"))
    layer_first, layer_second = object(), object()
    registry.mark_open(first, layers=[layer_first])
    registry.mark_open(second, layers=[layer_second])
    assert registry.find_by_layer(layer_first) is first
    assert registry.find_by_layer(layer_second) is second
    assert registry.find_by_layer(object()) is None


def test_removing_the_last_owned_layer_names_the_session_to_close():
    registry = SessionRegistry()
    session = registry.open(_endpoint(), _Result())
    a, b = object(), object()
    registry.mark_open(session, layers=[a, b])
    assert registry.layer_removed(a) is None            # one left
    assert registry.layer_removed(b) is session         # last one gone
    assert registry.layer_removed(object()) is None     # not ours


def test_temp_files_live_until_close_and_are_gone_after(tmp_path):
    registry = SessionRegistry(temp_root=tmp_path)
    session = registry.open(_endpoint("reader"), _Result())
    directory = registry.temp_dir_for(session)
    exported = directory / "x.h5"
    exported.write_bytes(b"data")
    registry.mark_exporting(session)
    registry.mark_open(session, files=[exported], layers=[object()])
    assert exported.exists()
    registry.close(session)
    assert not directory.exists()
    assert session.files == []


def test_a_failed_export_cleans_up_immediately(tmp_path):
    registry = SessionRegistry(temp_root=tmp_path)
    session = registry.open(_endpoint("reader"), _Result())
    directory = registry.temp_dir_for(session)
    registry.mark_exporting(session)
    registry.mark_failed(session, "boom")
    assert session.state == "failed" and session.error == "boom"
    assert not directory.exists()
    assert registry.sessions() == []


def test_state_transitions_are_checked():
    registry = SessionRegistry()
    session = registry.open(_endpoint(), _Result())
    registry.mark_open(session)
    with pytest.raises(SessionError):
        registry.mark_exporting(session)
    registry.close(session)
    with pytest.raises(SessionError):
        registry.mark_open(session)


def test_close_all_and_forget(tmp_path):
    registry = SessionRegistry(temp_root=tmp_path)
    for _ in range(3):
        registry.mark_open(registry.open(_endpoint(), _Result()), layers=[object()])
    assert len(registry.close_all()) == 3
    registry.forget_closed()
    assert registry.all_sessions() == []
