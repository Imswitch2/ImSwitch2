"""P-J: measurement jobs — off the GUI thread, cancellable, never stale.

The three failure modes this closes: a worker reading a napari layer from
another thread, a slow job overwriting a fast one, and a moving image being
measured half before and half after it moved.
"""

import subprocess
import sys
import textwrap
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord, new_uid
from imswitch.imcommon.algorithms.spatial_frame import AxisDescriptor, SpatialFrame
from imswitch.improcess.analysis.roi_jobs import (
    ArrayPlaneSource,
    CancellationToken,
    LazyPlaneSource,
    LiveDataError,
    MeasurementCache,
    MeasurementJob,
    MeasurementRunner,
    planes_for_axis,
    run_job,
    stack_axis_labels,
)


def _roi(name="a", bounds=(0, 4, 0, 4), **kwargs):
    return ROIRecord(name, "rectangle", bounds, uid=new_uid(), **kwargs)


def _mean(image, roi, plane):
    from imswitch.improcess.analysis.roi_manager import roi_values

    return {"roi": roi.name, "plane": plane, "mean": float(roi_values(image, roi).mean())}


def _stack(planes=4, size=8):
    return np.stack([np.full((size, size), float(i)) for i in range(planes)])


def _frame(shape=(8, 8), z=4):
    return SpatialFrame(
        coordinate_space_uid="space",
        result_uid="result",
        dataset_uid="data",
        plane_axes=("Y", "X"),
        axes=(AxisDescriptor("Z", z), AxisDescriptor("Y", shape[0]), AxisDescriptor("X", shape[1])),
        shape=shape,
    )


# --------------------------------------------------------------------------
# the worker must not need a viewer (F-26)
# --------------------------------------------------------------------------

def test_the_job_module_imports_no_viewer_or_gui_toolkit():
    """Checked on imports, not source text: the docstring names napari
    precisely to say the worker must not touch it."""
    import ast
    import inspect

    from imswitch.improcess.analysis import roi_jobs

    tree = ast.parse(inspect.getsource(roi_jobs))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    offenders = [
        name for name in imported if name.startswith(("napari", "qtpy", "PyQt", "PySide"))
    ]
    assert not offenders, offenders


def test_a_job_runs_with_no_viewer_present():
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))
    job = MeasurementJob.create([_roi()], source, [(("Z", 2),)])

    result = run_job(job, _mean)

    assert result.publishable
    assert result.rows[0]["mean"] == pytest.approx(2.0)


def test_planes_are_indexed_by_label_not_position():
    """Axis order is view-mode dependent; indexing positionally would follow a
    transposition onto the wrong axis."""
    stack = np.stack([np.full((8, 8), float(i)) for i in range(5)], axis=-1)
    source = ArrayPlaneSource(stack, axis_labels=("Y", "X", "T"))
    job = MeasurementJob.create([_roi()], source, [(("T", 3),)])

    result = run_job(job, _mean)

    assert result.rows[0]["mean"] == pytest.approx(3.0)


# --------------------------------------------------------------------------
# live data: snapshot or refuse, never opportunistic (F-33)
# --------------------------------------------------------------------------

def test_a_live_array_is_snapshotted_so_one_run_sees_one_moment():
    live = _stack()
    source = ArrayPlaneSource(live, axis_labels=("Z", "Y", "X"), live=True)

    live[2] = 999.0  # the acquisition writes while we measure
    job = MeasurementJob.create([_roi()], source, [(("Z", 2),)])
    result = run_job(job, _mean)

    assert result.rows[0]["mean"] == pytest.approx(2.0), "the snapshot was not taken"


def test_a_live_array_can_be_refused_instead_of_snapshotted():
    """Snapshotting a long stack can be larger than memory; refusing is then
    the right answer, and it must be explicit."""
    with pytest.raises(LiveDataError):
        ArrayPlaneSource(
            _stack(), axis_labels=("Z", "Y", "X"), live=True, refuse_live=True
        )


def test_results_are_withheld_when_the_image_moved_mid_run():
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))
    job = MeasurementJob.create([_roi()], source, [(("Z", 0),)])

    source.mutation_token = "something-else"  # the pixels changed under us
    result = run_job(job, _mean)

    assert result.stale
    assert not result.publishable


# --------------------------------------------------------------------------
# cancellation and progress (P-J.2)
# --------------------------------------------------------------------------

def test_cancelling_stops_within_a_plane():
    source = ArrayPlaneSource(_stack(planes=20), axis_labels=("Z", "Y", "X"))
    planes = planes_for_axis(_frame(z=20), "Z")
    job = MeasurementJob.create([_roi()], source, planes)
    cancellation = CancellationToken()

    def measure(image, roi, plane):
        cancellation.cancel()  # cancelled on the very first plane
        return _mean(image, roi, plane)

    result = run_job(job, measure, cancellation=cancellation)

    assert result.cancelled
    assert not result.publishable
    assert len(result.rows) < 10, "cancellation did not take effect promptly"


def test_progress_is_reported_per_step():
    source = ArrayPlaneSource(_stack(planes=3), axis_labels=("Z", "Y", "X"))
    planes = planes_for_axis(_frame(z=3), "Z")
    job = MeasurementJob.create([_roi("a"), _roi("b")], source, planes)
    seen = []

    run_job(job, _mean, progress=lambda done, total: seen.append((done, total)))

    assert seen[-1] == (6, 6)


# --------------------------------------------------------------------------
# stale-result rejection (P-J.3)
# --------------------------------------------------------------------------

def test_a_superseded_job_is_not_publishable():
    """A slow Multi Measure must not overwrite a quick Measure started after.

    Driven inline rather than with real threads: the supersession rule is
    ordinary logic, and spawning OS threads inside a suite that also holds Qt
    and napari open makes teardown crash intermittently. The condition is
    reproduced exactly by starting the quicker job from inside the slower
    one's measure callback.
    """
    runner = MeasurementRunner()
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))
    slow = MeasurementJob.create([_roi()], source, [(("Z", 0),)])
    quick = MeasurementJob.create([_roi()], source, [(("Z", 1),)])
    results = {}

    def slow_measure(image, roi, plane):
        # The user starts a quicker measurement while this one is running.
        runner.submit(
            quick, _mean, on_done=lambda r: results.setdefault("quick", r),
            threaded=False,
        )
        return _mean(image, roi, plane)

    runner.submit(
        slow, slow_measure,
        on_done=lambda r: results.setdefault("slow-published", r),
        on_discarded=lambda r: results.setdefault("slow-discarded", r),
        threaded=False,
    )

    assert results["quick"].publishable
    # on_done is the publish hook: a superseded run must not reach it at all,
    # rather than arriving with a flag the caller has to remember to check.
    assert "slow-published" not in results, "the superseded job published anyway"
    assert not results["slow-discarded"].publishable


def test_a_cancelled_run_is_discarded_not_published():
    runner = MeasurementRunner()
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))
    job = MeasurementJob.create([_roi()], source, [(("Z", 0),)])
    published, discarded = [], []

    stop = CancellationToken()

    def measure(image, roi, plane):
        stop.cancel()
        return _mean(image, roi, plane)

    # run_job checks the token between ROIs and planes, so a job cancelled
    # during its only measurement still reports itself cancelled.
    from imswitch.improcess.analysis.roi_jobs import run_job as _run

    result = _run(job, measure, cancellation=stop)
    (published if result.publishable else discarded).append(result)

    assert published == []
    assert discarded and discarded[0].cancelled


def test_callbacks_run_on_the_worker_thread_and_say_so():
    """Documented rather than marshalled here: this module stays Qt-free, so
    the panel is responsible for getting back to the GUI thread."""
    import inspect

    from imswitch.improcess.analysis.roi_jobs import MeasurementRunner as Runner

    doc = inspect.getdoc(Runner.submit) or ""
    assert "worker thread" in doc and "GUI thread" in doc


_THREADED_RUN = textwrap.dedent(
    """
    import numpy as np
    from imswitch.imcommon.algorithms.roi import ROIRecord, new_uid
    from imswitch.improcess.analysis.roi_jobs import (
        ArrayPlaneSource, MeasurementJob, MeasurementRunner,
    )

    stack = np.stack([np.full((8, 8), float(i)) for i in range(200)])
    source = ArrayPlaneSource(stack, axis_labels=("Z", "Y", "X"))
    roi = ROIRecord("a", "rectangle", (0, 4, 0, 4), uid=new_uid())
    planes = [(("Z", i),) for i in range(200)]
    job = MeasurementJob.create([roi], source, planes)

    published = []
    runner = MeasurementRunner()
    runner.submit(job, lambda image, r, plane: {"m": float(image.mean())},
                  on_done=published.append)
    assert runner.busy or published, "the worker never started"

    assert runner.shutdown(timeout=30), "shutdown did not join the worker"
    assert not runner.busy

    # A run that completes must publish; one that is cancelled must not.
    runner2 = MeasurementRunner()
    done = []
    runner2.submit(job, lambda image, r, plane: {"m": float(image.mean())},
                   on_done=done.append)
    assert runner2.wait(timeout=30)
    assert done and done[0].publishable

    print("THREADING-OK")
    """
)


def test_the_worker_thread_really_runs_and_is_joined_by_shutdown():
    """Exercised in a subprocess, deliberately.

    The threading contract needs a real thread to mean anything, but starting
    OS threads inside this suite — which also has Qt, napari and vispy loaded —
    made teardown segfault intermittently (3 runs in 5). Isolating it proves
    the same behaviour without destabilising every other test in the process.
    """
    result = subprocess.run(
        [sys.executable, "-c", _THREADED_RUN],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "THREADING-OK" in result.stdout, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_a_current_job_publishes():
    runner = MeasurementRunner()
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))
    job = MeasurementJob.create([_roi()], source, [(("Z", 1),)])
    done = []

    runner.submit(job, _mean, on_done=done.append, threaded=False)

    assert done[0].publishable
    assert done[0].rows[0]["mean"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# caching (P-J.4)
# --------------------------------------------------------------------------

def test_repeating_a_measurement_uses_the_cache():
    # The token comes from something that actually tracks change — here a
    # result uid and revision. Sources no longer invent one.
    source = ArrayPlaneSource(
        _stack(), axis_labels=("Z", "Y", "X"), mutation_token="result-1:rev-3"
    )
    roi = _roi()
    cache = MeasurementCache()
    calls = []

    def counting(image, r, plane):
        calls.append(1)
        return _mean(image, r, plane)

    job = MeasurementJob.create([roi], source, [(("Z", 1),)], frame_uid="frame")
    run_job(job, counting, cache=cache)
    run_job(job, counting, cache=cache)

    assert len(calls) == 1, "the second run should have been served from cache"


def test_moving_an_roi_invalidates_it_but_renaming_does_not():
    from imswitch.imcommon.algorithms.roi import replaced

    source = ArrayPlaneSource(
        _stack(), axis_labels=("Z", "Y", "X"), mutation_token="result-1:rev-3"
    )
    roi = _roi()
    cache = MeasurementCache()
    plane = (("Z", 1),)

    job = MeasurementJob.create([roi], source, [plane], frame_uid="frame")
    key = MeasurementCache.key(job, plane, roi)
    cache.put(key, {"cached": True})

    renamed = replaced(roi, name="nucleus")
    moved = replaced(roi, bounds=(2, 8, 2, 8))

    assert cache.get(MeasurementCache.key(job, plane, renamed)) is not None
    assert cache.get(MeasurementCache.key(job, plane, moved)) is None


def test_caching_is_disabled_without_a_trustworthy_token():
    """A plain array carries no evidence that its pixels have not changed.

    Deriving a token from the buffer address and shape looked like a token and
    was not: an in-place write leaves both identical, so cached rows from
    before the write would be served afterwards.
    """
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))
    assert source.mutation_token == "", "a source must not invent a token"

    job = MeasurementJob.create([_roi()], source, [(("Z", 0),)])

    assert MeasurementCache.key(job, (("Z", 0),), job.rois[0]) is None


def test_an_in_place_rewrite_is_not_certified_as_unchanged():
    """The concrete failure the removed token would have allowed."""
    stack = _stack()
    before = ArrayPlaneSource(stack, axis_labels=("Z", "Y", "X"))
    stack[1] = 999.0
    after = ArrayPlaneSource(stack, axis_labels=("Z", "Y", "X"))

    # Neither claims a token, so nothing can be reused across the rewrite.
    assert before.mutation_token == after.mutation_token == ""
    job = MeasurementJob.create([_roi()], after, [(("Z", 1),)])
    assert MeasurementCache.key(job, (("Z", 1),), job.rois[0]) is None


def test_a_snapshot_may_carry_a_token_because_nothing_can_write_to_it():
    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"), live=True)

    assert source.mutation_token.startswith("snapshot:")


# --------------------------------------------------------------------------
# positions must identify a plane, or be refused (round-7 finding 5)
# --------------------------------------------------------------------------

def test_an_unknown_axis_label_is_refused_not_ignored():
    from imswitch.improcess.analysis.roi_jobs import PositionError

    source = ArrayPlaneSource(_stack(), axis_labels=("Z", "Y", "X"))

    with pytest.raises(PositionError):
        source.read_plane((("T", 0),))


def test_an_out_of_range_index_is_refused_not_clamped():
    """Clamping measured the nearest plane and reported a number for it."""
    from imswitch.improcess.analysis.roi_jobs import PositionError

    source = ArrayPlaneSource(_stack(planes=4), axis_labels=("Z", "Y", "X"))

    with pytest.raises(PositionError):
        source.read_plane((("Z", 99),))


def test_an_unpinned_stack_axis_is_refused_not_resolved_to_zero():
    from imswitch.improcess.analysis.roi_jobs import PositionError

    stack = np.zeros((3, 4, 8, 8))
    source = ArrayPlaneSource(stack, axis_labels=("T", "Z", "Y", "X"))

    with pytest.raises(PositionError):
        source.read_plane((("T", 1),))  # Z left unsaid

    plane = source.read_plane((("T", 1), ("Z", 2)))
    assert plane.shape == (8, 8)


def test_a_different_frame_does_not_reuse_cached_rows():
    source = ArrayPlaneSource(
        _stack(), axis_labels=("Z", "Y", "X"), mutation_token="result-1:rev-3"
    )
    roi = _roi()
    plane = (("Z", 1),)
    here = MeasurementJob.create([roi], source, [plane], frame_uid="frame-a")
    there = MeasurementJob.create([roi], source, [plane], frame_uid="frame-b")

    assert MeasurementCache.key(here, plane, roi) != MeasurementCache.key(there, plane, roi)


# --------------------------------------------------------------------------
# plane enumeration
# --------------------------------------------------------------------------

def test_planes_for_axis_covers_the_whole_axis():
    planes = planes_for_axis(_frame(z=4), "Z")

    assert len(planes) == 4
    assert planes[2] == (("Z", 2),)


def test_stack_axes_exclude_the_displayed_plane_and_singletons():
    frame = SpatialFrame(
        coordinate_space_uid="space",
        result_uid="result",
        dataset_uid="data",
        plane_axes=("Y", "X"),
        axes=(
            AxisDescriptor("T", 5),
            AxisDescriptor("C", 1),   # single channel: nothing to step through
            AxisDescriptor("Y", 8),
            AxisDescriptor("X", 8),
        ),
        shape=(8, 8),
    )

    assert stack_axis_labels(frame) == ("T",)


def test_lazy_source_reads_one_plane_at_a_time():
    class _Lazy:
        """Stands in for a virtual array; records what was materialised."""

        def __init__(self, data):
            self._data = data
            self.reads = 0
            self.shape = data.shape

        def __getitem__(self, item):
            self.reads += 1
            return self._data[item]

    handle = _Lazy(_stack(planes=6))
    source = LazyPlaneSource(handle, axis_labels=("Z", "Y", "X"))
    planes = planes_for_axis(_frame(z=6), "Z")
    job = MeasurementJob.create([_roi()], source, planes)

    run_job(job, _mean)

    assert handle.reads == 6, "the whole stack should never be materialised at once"


# --------------------------------------------------------------------------
# review round 8 — the cache holds values, and a bad ROI cannot kill a run
# --------------------------------------------------------------------------

def test_the_cache_holds_values_not_decorated_rows():
    """A cached row would carry the name it was first measured under."""
    source = ArrayPlaneSource(
        array=np.ones((2, 4, 4)),
        axis_labels=("Z", "Y", "X"),
        mutation_token="token",
    )
    roi = SimpleNamespace(uid="u1", revision=0, name="before")
    cache = MeasurementCache()
    job = MeasurementJob.create([roi], source, planes=[(("Z", 0),)])

    run_job(
        job,
        lambda image, r, plane: {"mean": 1.0},
        decorate=lambda values, r, plane: {"roi": r.name, **values},
        cache=cache,
    )
    assert list(cache._entries.values()) == [{"mean": 1.0}]

    # Renaming deliberately does not bump the revision, so the cache is hit —
    # and the published row must still say the new name.
    roi.name = "after"
    result = run_job(
        MeasurementJob.create([roi], source, planes=[(("Z", 0),)]),
        lambda image, r, plane: pytest.fail("should have come from the cache"),
        decorate=lambda values, r, plane: {"roi": r.name, **values},
        cache=cache,
    )
    assert result.rows[0] == {"roi": "after", "mean": 1.0}


def test_one_failing_roi_does_not_cost_the_others():
    source = ArrayPlaneSource(
        array=np.ones((1, 4, 4)), axis_labels=("Z", "Y", "X"), mutation_token="t"
    )
    good = SimpleNamespace(uid="good", revision=0)
    bad = SimpleNamespace(uid="bad", revision=0)
    errors = []

    def measure(image, roi, plane):
        if roi is bad:
            raise ValueError("this ROI is a line")
        return {"mean": 1.0}

    result = run_job(
        MeasurementJob.create([bad, good], source, planes=[(("Z", 0),)]),
        measure,
        on_error=lambda roi, plane, exc: errors.append((roi, str(exc))),
    )
    assert result.publishable
    assert len(result.rows) == 1
    assert errors and errors[0][0] is bad


def test_a_failing_run_still_ends_the_job():
    """A worker that dies must not leave the panel showing a progress bar."""
    class _Exploding:
        mutation_token = "t"

        def read_plane(self, position):
            raise RuntimeError("the source is gone")

    runner = MeasurementRunner()
    job = MeasurementJob.create(
        [SimpleNamespace(uid="u", revision=0)], _Exploding(), planes=[(("Z", 0),)]
    )
    published, discarded, errors = [], [], []

    runner.submit(
        job,
        lambda image, roi, plane: {"mean": 1.0},
        on_done=published.append,
        on_discarded=discarded.append,
        on_error=lambda roi, plane, exc: errors.append(exc),
        threaded=False,
    )
    # read_plane failing is a per-ROI failure, so the run completes with no
    # rows rather than dying.
    assert len(published) == 1
    assert published[0].rows == ()
    assert errors


def test_a_measure_that_returns_none_is_a_skip_not_a_failure():
    source = ArrayPlaneSource(
        array=np.ones((3, 4, 4)), axis_labels=("Z", "Y", "X"), mutation_token="t"
    )
    roi = SimpleNamespace(uid="u", revision=0)
    errors = []

    result = run_job(
        MeasurementJob.create(
            [roi], source, planes=[(("Z", z),) for z in range(3)]
        ),
        lambda image, r, plane: {"mean": 1.0} if plane == (("Z", 1),) else None,
        on_error=lambda *a: errors.append(a),
    )
    assert len(result.rows) == 1
    assert errors == []
