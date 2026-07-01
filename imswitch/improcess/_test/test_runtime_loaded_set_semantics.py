"""Tests for the runtime-loaded set semantics on ImProcessMainView.

Locks in the rule introduced after the 'config-driven panel resurrecting via
persistence' bug: only docks created through the runtime path may end up in
``runtimeAnalysisToolIdsLoaded`` — config-driven panels never persist as
runtime tools, so changing the setup JSON between sessions actually has the
expected effect.

Bound directly to the unbound methods so we can exercise the logic without
spinning up Qt or napari.
"""

from __future__ import annotations

from types import SimpleNamespace

from imswitch.improcess.model.runtime_tools import RuntimeAnalysisToolSpec
from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


def _stub_view(specs_keys=('segmentation', 'frc', 'roi-manager')):
    """Build a fake `self` that exposes only what runtimeAnalysisToolIdsLoaded
    and ensureRuntimeAnalysisWidget logic care about."""
    specs = {
        tid: RuntimeAnalysisToolSpec(
            id=tid,
            title=tid.replace('-', ' ').title(),
            attribute=f"{tid.replace('-', '_')}Widget",
            widget_kind=tid,
        )
        for tid in specs_keys
    }
    stub = SimpleNamespace(
        _runtimeAnalysisToolIds=set(),
        docks={},
        _runtimeAnalysisToolSpecs=lambda: specs,
    )
    return stub


def test_runtime_set_starts_empty_with_config_only_panels():
    stub = _stub_view()
    # Simulate the __init__ path: a config-driven panel populates self.docks
    # but does NOT touch _runtimeAnalysisToolIds.
    stub.docks['Segmentation'] = object()

    result = ImProcessMainView.runtimeAnalysisToolIdsLoaded(stub)

    # Config-driven panel must not appear in the persisted runtime set.
    assert result == []


def test_runtime_set_includes_only_explicitly_loaded_tools():
    stub = _stub_view()
    # Config-driven Segmentation dock + runtime-loaded FRC.
    stub.docks['Segmentation'] = object()
    stub.docks['Frc'] = object()
    stub._runtimeAnalysisToolIds.add('frc')

    result = ImProcessMainView.runtimeAnalysisToolIdsLoaded(stub)

    assert result == ['frc']


def test_runtime_set_drops_tools_whose_docks_were_closed():
    """If the user runtime-loaded a panel and then closed its dock the
    persisted set must not still resurrect it next launch."""
    stub = _stub_view()
    stub._runtimeAnalysisToolIds.add('frc')
    stub._runtimeAnalysisToolIds.add('psf-resolution')
    # frc dock survives, psf-resolution was closed.
    stub.docks['Frc'] = object()

    result = ImProcessMainView.runtimeAnalysisToolIdsLoaded(stub)

    assert result == ['frc']


def test_runtime_set_ignores_unknown_ids():
    """Defensive: a future tool id we've never heard of in this build of
    ImProcess should not crash the persistence path."""
    stub = _stub_view()
    stub._runtimeAnalysisToolIds.add('not-a-thing')
    stub._runtimeAnalysisToolIds.add('frc')
    stub.docks['Frc'] = object()

    result = ImProcessMainView.runtimeAnalysisToolIdsLoaded(stub)

    assert result == ['frc']


def test_runtime_set_is_sorted_for_stable_persistence_format():
    """A stable order makes the persisted JSON diffable across runs even
    when the user adds tools in a different sequence."""
    stub = _stub_view(specs_keys=('frc', 'roi-manager', 'segmentation'))
    stub._runtimeAnalysisToolIds.update({'segmentation', 'frc', 'roi-manager'})
    stub.docks.update({
        'Segmentation': object(), 'Frc': object(), 'Roi Manager': object(),
    })

    result = ImProcessMainView.runtimeAnalysisToolIdsLoaded(stub)

    assert result == sorted(result)
    assert set(result) == {'frc', 'roi-manager', 'segmentation'}
