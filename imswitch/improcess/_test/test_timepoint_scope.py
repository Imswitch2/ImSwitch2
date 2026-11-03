import pytest

from imswitch.improcess.model.timepoint_scope import (
    TimepointScope,
    TimepointScopeMode,
    coerce_timepoint_scope,
)


def test_all_scope_keeps_open_ended_live_bounds_until_total_is_known():
    scope = TimepointScope.all()
    assert scope.bounds() == (0, None)
    assert scope.bounds(20) == (0, 20)
    assert scope.label(20) == "All T (20)"
    assert scope.is_full(20)


def test_single_and_range_are_zero_based_but_present_one_based():
    single = TimepointScope.single(6)
    assert single.bounds(20) == (6, 7)
    assert single.label(20) == "T7"
    assert not single.is_full(20)

    selected = TimepointScope.range(3, 11)
    assert selected.bounds(20) == (3, 12)
    assert selected.count(20) == 9
    assert selected.label(20) == "T4–T12"
    assert selected.to_dict(20)["index_base"] == 0


def test_scope_validation_and_mapping_roundtrip():
    with pytest.raises(ValueError):
        TimepointScope.range(4, 3)
    with pytest.raises(IndexError):
        TimepointScope.single(20).bounds(20)

    scope = coerce_timepoint_scope({"mode": "range", "first": 2, "last": 5})
    assert scope.mode == TimepointScopeMode.RANGE
    assert scope == TimepointScope.range(2, 5)
