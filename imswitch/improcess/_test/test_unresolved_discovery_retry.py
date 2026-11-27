"""Pure scheduling checks: no Qt or Zarr installation required."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Keep the retry policy testable without importing the Qt-dependent live package.
_module_path = Path(__file__).resolve().parents[1] / "live" / "discovery_retry.py"
_spec = importlib.util.spec_from_file_location("_unresolved_discovery_retry", _module_path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
UnresolvedDiscoveryRetry = _module.UnresolvedDiscoveryRetry


def test_retry_starts_after_three_seconds_and_backs_off_to_thirty():
    retries = UnresolvedDiscoveryRetry()
    retries.track("slow.zarr", now=0)
    assert retries.due(now=2.99) == ()
    assert retries.due(now=3.0) == ("slow.zarr",)
    assert retries.due(now=8.99) == ()
    assert retries.due(now=9.0) == ("slow.zarr",)
    assert retries.due(now=20.99) == ()
    assert retries.due(now=21.0) == ("slow.zarr",)
    assert retries.due(now=44.99) == ()
    assert retries.due(now=45.0) == ("slow.zarr",)
    assert retries.due(now=74.99) == ()
    assert retries.due(now=75.0) == ("slow.zarr",)
    assert retries.due(now=105.0) == ("slow.zarr",)


def test_retry_is_fair_and_rate_limited_with_backlog():
    retries = UnresolvedDiscoveryRetry(max_per_tick=2)
    for name in ("a.zarr", "b.zarr", "c.zarr", "d.zarr", "e.zarr"):
        retries.track(name, now=0)
    assert retries.due(now=3) == ("a.zarr", "b.zarr")
    assert retries.due(now=3) == ("c.zarr", "d.zarr")
    assert retries.due(now=3) == ("e.zarr",)
    assert retries.due(now=3) == ()
    assert retries.due(now=9) == ("a.zarr", "b.zarr")


def test_repeat_track_does_not_reset_deadline_or_generate_arrivals():
    retries = UnresolvedDiscoveryRetry()
    retries.track("recording.zarr", now=0)
    retries.track("recording.zarr", now=10)
    assert retries.due(now=3) == ("recording.zarr",)
    retries.track("recording.zarr", now=50)
    assert retries.due(now=9) == ("recording.zarr",)


def test_resolved_or_removed_entry_stops_retry_and_clear_is_idempotent():
    retries = UnresolvedDiscoveryRetry()
    retries.track("one.zarr", now=0)
    retries.track("two.zarr", now=0)
    retries.discard("one.zarr")
    assert retries.due(now=3) == ("two.zarr",)
    retries.clear()
    retries.discard("missing.zarr")
    assert not retries
    assert retries.due(now=100) == ()


@pytest.mark.parametrize("kwargs", [
    {"initial_delay_s": 0},
    {"initial_delay_s": 4, "max_delay_s": 3},
    {"max_per_tick": 0},
])
def test_invalid_retry_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        UnresolvedDiscoveryRetry(**kwargs)
