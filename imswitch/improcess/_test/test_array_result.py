"""Tests for ArrayProcessingResult, including lazy-safe duplicate."""

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.lazy_array import LazySubsetArray


class _LazyArray:
    """Array-like test double: __getitem__ is allowed, __array__ is not."""

    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.ndim = self._data.ndim
        self.dtype = self._data.dtype
        self.backend = "test-lazy"
        self.keys = []

    def __getitem__(self, key):
        self.keys.append(key)
        return self._data[key]

    def __array__(self, dtype=None):
        raise AssertionError("duplicate should not materialize a lazy source")


def _source(data, name="source"):
    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=["Z", "Y", "X"][-np.ndim(data):] if hasattr(data, "shape") else ["Z", "Y", "X"],
    )


def test_duplicate_copies_plain_ndarray():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    source = _source(data)

    duplicate = ArrayProcessingResult.duplicate(source)

    assert duplicate.name == "source (duplicate)"
    assert isinstance(duplicate.data, np.ndarray)
    assert not np.shares_memory(duplicate.data, data)
    np.testing.assert_array_equal(duplicate.data, data)


def test_duplicate_does_not_materialize_lazy_source():
    data = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    lazy = _LazyArray(data)
    source = _source(lazy)

    duplicate = ArrayProcessingResult.duplicate(source)

    assert lazy.keys == []
    assert isinstance(duplicate.data, LazySubsetArray)
    assert duplicate.data.shape == data.shape
    np.testing.assert_array_equal(np.asarray(duplicate.data[0]), data[0])
    assert lazy.keys, "reading a plane should defer through __getitem__"
