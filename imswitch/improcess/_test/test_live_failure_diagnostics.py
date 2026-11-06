"""C0 diagnostic controls: one-shot fault consumption and portable log."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_diag():
    # Load the intentionally standalone module without importing Qt-heavy
    # improcess package initialization (also works on headless CI).
    module_path = Path(__file__).resolve().parents[1] / "execution" / "live_diagnostics.py"
    spec = importlib.util.spec_from_file_location("test_live_diagnostics_isolated", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_shot_fault_and_same_log_for_finite_retry(tmp_path, monkeypatch):
    d = _load_diag()
    monkeypatch.setattr(d.Path, "home", lambda: tmp_path)
    try:
        path = Path(d.start())
        assert path.is_file()
        d.arm("begin")
        assert d.state()["armed"] == "begin"
        assert d.claim_live("live-1") == "begin"
        assert d.claim_live("live-2") == "off"
        assert d.state()["armed"] == "off"
        d.injected("begin", "live-1")
        d.event("queue.finish_current", job="live-1", error="injected")
        d.event("queue.manual_submitted", job="finite-2")
        d.event("queue.offline_start", job="finite-2")
    finally:
        d.stop()
    output = path.read_text(encoding="utf-8")
    assert "fault.claimed" in output
    assert "fault.triggered" in output
    assert "queue.offline_start" in output
    assert output.index("fault.triggered") < output.index("queue.offline_start")


def test_logging_can_change_fault_at_runtime_and_stop_without_restart(tmp_path, monkeypatch):
    d = _load_diag()
    monkeypatch.setattr(d.Path, "home", lambda: tmp_path)
    try:
        d.start()
        d.arm("reader")
        d.arm("after_t0")
        assert d.claim_live("job") == "after_t0"
        d.arm("begin")
        d.arm("off")
        assert d.claim_live("job-2") == "off"
        with pytest.raises(ValueError):
            d.arm("unsupported")
    finally:
        d.stop()
    assert d.state()["active"] is False
    assert d.state()["path"]


def test_catalog_inventory_distinguishes_physical_scans(tmp_path, monkeypatch):
    """A cached scan0-only index must be visibly distinct from on-disk scan1."""
    from types import SimpleNamespace

    d = _load_diag()
    monkeypatch.setattr(d.Path, "home", lambda: tmp_path)
    root = tmp_path / "example.zarr"
    (root / "scan0").mkdir(parents=True)
    (root / "scan1").mkdir()
    item = SimpleNamespace(ordinal=0, path=str(root), dataset="scan0/CAM/data")
    class FakeIndex:
        planned_count = 2
        anchor_detector = "CAM"
        anchor = item
        anchor_index = 0
        def channel(self, _detector):
            return SimpleNamespace(candidates=[item])
    source = SimpleNamespace(sourceMetadata=FakeIndex(), dataPath=str(root))
    try:
        path = Path(d.start())
        d.log_lapse_inventory(source, scope="All", units=())
    finally:
        d.stop()
    log = path.read_text(encoding="utf-8")
    assert "finite.lapse_inventory" in log
    assert "catalog_indices='[0]'" in log
    assert "scan0" in log and "scan1" in log
