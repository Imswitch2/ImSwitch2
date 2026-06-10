"""Regression test for the VContainer.raiseDock AttributeError crash.

pyqtgraph's Dock.raiseDock() only works inside tab containers; it crashes
with ``AttributeError: 'VContainer' object has no attribute 'raiseDock'``
when called on a dock placed in a vertical / horizontal stack (which is
exactly what addDock('bottom', anchor) produces for runtime analysis
panels). The fix wraps the call in _safeRaiseDock which swallows the
AttributeError but propagates nothing harmful.
"""

from __future__ import annotations

from types import SimpleNamespace

from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


class _DockInVContainer:
    """A pyqtgraph Dock whose container is a VContainer-like object."""

    def __init__(self):
        self.name = 'fake-dock'

    def raiseDock(self):
        raise AttributeError("'VContainer' object has no attribute 'raiseDock'")


class _DockInTabContainer:
    """A pyqtgraph Dock whose container is a tab container (works fine)."""

    def __init__(self):
        self.name = 'fake-tab-dock'
        self.raise_calls = 0

    def raiseDock(self):
        self.raise_calls += 1


def _stub_view():
    return SimpleNamespace(
        _logger=SimpleNamespace(debug=lambda *_a, **_kw: None),
    )


def test_safe_raise_dock_swallows_attribute_error_from_vcontainer():
    stub = _stub_view()
    dock = _DockInVContainer()
    # Should not raise even though dock.raiseDock() does.
    ImProcessMainView._safeRaiseDock(stub, dock)


def test_safe_raise_dock_forwards_to_real_dock_when_supported():
    stub = _stub_view()
    dock = _DockInTabContainer()

    ImProcessMainView._safeRaiseDock(stub, dock)

    # The underlying raise was actually delivered.
    assert dock.raise_calls == 1


def test_safe_raise_dock_logs_unexpected_errors_but_does_not_raise():
    captured = []
    stub = SimpleNamespace(
        _logger=SimpleNamespace(
            debug=lambda msg, exc_info=False: captured.append((msg, exc_info)),
        ),
    )

    class _BoomDock:
        name = 'boom-dock'

        def raiseDock(self):
            raise RuntimeError('unexpected qt failure')

    ImProcessMainView._safeRaiseDock(stub, _BoomDock())

    # The error was logged at debug level (not error) so the runtime path
    # keeps going, and exc_info is requested so the traceback is visible.
    assert any('boom-dock' in msg for msg, _ in captured)
    assert any(exc_info for _, exc_info in captured)
