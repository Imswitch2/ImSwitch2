import numpy as np
import pytest

from slmcore import (
    PositionReference,
    PositionReferenceLifetime,
    PositionReferenceStore,
)


def _reference(name="aligned OFF",plane="sample"):
    return PositionReference(
        name=name,
        plane_name=plane,
        lattice_indices=np.array([[0,1,0,1],[0,0,1,1]],dtype=np.int64),
        positions_px=np.array(
            [[10.0,20.0,10.0,20.0],[10.0,10.0,20.0,20.0]],
            dtype=np.float64,
        ),
        image_shape=(64,64),
        detector_name="cam",
        metadata={"source_slm_serial":"SER-A"},
    )


def test_session_reference_is_memory_only(tmp_path):
    root = tmp_path / "position_references"
    store = PositionReferenceStore(root)
    record = store.add(_reference())

    assert record.lifetime is PositionReferenceLifetime.SESSION
    assert store.names("sample") == ("aligned OFF",)
    assert not root.exists()


def test_persist_changes_lifetime_without_duplicate(tmp_path):
    store = PositionReferenceStore(tmp_path)
    store.add(_reference())

    record = store.persist("sample","aligned OFF")

    assert record.lifetime is PositionReferenceLifetime.PERSISTENT
    assert len(store.list("sample")) == 1
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_persistent_reference_reloads_but_session_reference_does_not(tmp_path):
    store = PositionReferenceStore(tmp_path)
    store.add(_reference("session only"))
    store.add(_reference("persistent"))
    store.persist("sample","persistent")

    restored = PositionReferenceStore(tmp_path)

    assert not restored.exists("sample","session only")
    record = restored.get_record("sample","persistent")
    assert record.lifetime is PositionReferenceLifetime.PERSISTENT
    np.testing.assert_allclose(
        record.reference.positions_px,_reference("persistent").positions_px,
    )


def test_overwrite_preserves_existing_lifetime(tmp_path):
    store = PositionReferenceStore(tmp_path)
    original = _reference()
    store.add(original)
    store.persist("sample",original.name)
    updated = PositionReference(
        name=original.name,
        plane_name=original.plane_name,
        lattice_indices=original.lattice_indices,
        positions_px=np.asarray(original.positions_px)+3.0,
        image_shape=original.image_shape,
        detector_name=original.detector_name,
    )

    record = store.add(updated,overwrite=True)

    assert record.lifetime is PositionReferenceLifetime.PERSISTENT
    np.testing.assert_allclose(store.get("sample",original.name).positions_px,updated.positions_px)
    reloaded = PositionReferenceStore(tmp_path)
    np.testing.assert_allclose(reloaded.get("sample",original.name).positions_px,updated.positions_px)


def test_delete_removes_persistent_backing_but_session_delete_is_ram_only(tmp_path):
    store = PositionReferenceStore(tmp_path)
    store.add(_reference("session"))
    store.add(_reference("persistent"))
    store.persist("sample","persistent")

    store.delete("sample","session")
    assert not store.exists("sample","session")
    assert len(list(tmp_path.rglob("*.json"))) == 1

    store.delete("sample","persistent")
    assert not store.exists("sample","persistent")
    assert list(tmp_path.rglob("*.json")) == []


def test_duplicate_name_in_same_plane_requires_explicit_overwrite(tmp_path):
    store = PositionReferenceStore(tmp_path)
    store.add(_reference())

    with pytest.raises(FileExistsError):
        store.add(_reference())
