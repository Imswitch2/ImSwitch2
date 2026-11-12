"""Completed scientific T must remain visible without phantom aborted T."""

from types import SimpleNamespace

import numpy as np

from imswitch.improcess.execution.result_assembly import ResultAssemblyPlan
from imswitch.improcess.execution.result_publication import PublicationHooks, ResultPublication
from imswitch.improcess.model.timepoint_scope import TimepointScope


def _part(value):
    return SimpleNamespace(
        name='result', data=np.full((3, 4), value, dtype=np.float32),
        background=np.full((3, 4), -value, dtype=np.float32),
        axis_labels=['Y', 'X'], axis_scales=[1., 1.], scale_unit='um',
        kind='image', metadata={},
    )


def test_terminated_auto_retains_exact_scientific_t_prefix_with_planned_count():
    published = []
    effective_scopes = []
    plan = ResultAssemblyPlan.for_timepoints(
        'job', 'dataset', scope=TimepointScope.all(), total=5,
    )
    publisher = ResultPublication(plan, PublicationHooks(
        finalized=lambda *args: published.append(args),
        annotate_scope=lambda _result, scope, total: effective_scopes.append((scope, total)),
    ))
    output = publisher.append_completed(_part(2), 0)
    assert publisher.append_completed(_part(3), 1) is output
    assert output.data.shape == (5, 3, 4)  # allocated full plan while acquiring
    assert publisher.finish('cancelled') is output
    assert len(published) == 1
    assert output.data.shape == (2, 3, 4)
    assert output.background.shape == (2, 3, 4)
    np.testing.assert_array_equal(output.data[:, 0, 0], [2, 3])
    np.testing.assert_array_equal(output.background[:, 0, 0], [-2, -3])
    assert output.metadata['result_assembly']['published_t_ranges'] == [[0, 2]]
    assert output.metadata['result_assembly']['expected_steps'] == 5
    assert output.metadata['result_assembly']['status'] == 'cancelled'
    assert output.metadata['incremental_completion']['completed_timepoints'] == 2
    assert output.metadata['incremental_completion']['requested_timepoints'] == 5
    assert effective_scopes[-1][0].bounds(5) == (0, 2)
    assert effective_scopes[-1][1] == 5


def test_completed_prefix_keeps_full_scientific_extent_when_not_stopped():
    plan = ResultAssemblyPlan.for_timepoints(
        'job', 'dataset', scope=TimepointScope.range(0, 1), total=5,
    )
    publisher = ResultPublication(plan, PublicationHooks())
    output = publisher.append_completed(_part(2), 0)
    publisher.append_completed(_part(3), 1)
    publisher.finish('complete')
    assert output.data.shape == (2, 3, 4)
    assert output.metadata['result_assembly']['status'] == 'complete'


def test_stopped_follow_trims_preallocated_streaming_snapshot():
    scopes = []
    plan = ResultAssemblyPlan.for_timepoints('job', 'dataset', total=5)
    publisher = ResultPublication(plan, PublicationHooks(
        annotate_scope=lambda _result, scope, total: scopes.append((scope, total)),
    ))
    snapshot = SimpleNamespace(
        name='follow', axis_labels=['T', 'Y', 'X'],
        data=np.arange(5 * 3 * 4, dtype=np.float32).reshape(5, 3, 4),
        background=np.ones((5, 3, 4), dtype=np.float32), metadata={},
    )
    publisher.snapshot(snapshot)
    publisher.timepoint_completed(2)
    assert publisher.finish('cancelled') is snapshot
    assert snapshot.data.shape == (2, 3, 4)
    assert snapshot.background.shape == (2, 3, 4)
    assert scopes[-1][0].bounds(5) == (0, 2)
    assert snapshot.metadata['result_assembly']['expected_steps'] == 5
