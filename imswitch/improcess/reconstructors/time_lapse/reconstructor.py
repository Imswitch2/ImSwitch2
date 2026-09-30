"""Stack a saved time lapse along T.

A camera or scan time lapse is recorded one item per timepoint -- a file each,
or a group each inside one file. Open any one of them with this reconstructor
active and the whole lapse becomes the source; *Reconstruct* returns it as one
``(T, ...)`` stack that reads a timepoint only when it is shown, so a
2000-point lapse opens at once rather than after loading every point.

Finding the lapse, reading each point's header and placing points at their
recorded index is the data layer's job (``improcess.model.lapse_source``).
What is left here is what a person chooses: which detector, whether T is the
planned interval or the times the points actually started, whether an
incomplete point is marked in place or left off the end, and how the stack is
exported.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.lapse_source import (
    INCOMPLETE_MARK,
    INCOMPLETE_SKIP,
    TIME_LAPSE_SOURCE_KIND,
    TIMEPOINT_COMPLETE,
    TIMEPOINT_MISSING,
    LazyTimeLapseArray,
    NotATimeLapse,
    TimeLapseIndex,
    TimeLapsePlan,
    discover_time_lapse,
    plan_time_lapse,
    read_lapse_headers,
)
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.model.result_io import save_image_result
from imswitch.improcess.reconstructors.base import (
    ReconstructionContext,
    Reconstructor,
    SourceChoice,
    SourceInspection,
)

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


TIME_AXIS_PLANNED = "planned"
TIME_AXIS_ACTUAL = "actual"
#: Neither a planned interval nor two start times: T is a plain index.
TIME_AXIS_INDEX = "index"

#: A point counts as late when it started more than this fraction of the
#: interval after its schedule...
LATE_FRACTION_OF_INTERVAL = 0.1
#: ...and by more than this, so the few milliseconds between scheduling a
#: point and opening its file never make a fast lapse look late throughout.
LATE_MINIMUM_S = 0.1

#: At most this many header-progress updates per lapse; each is a signal to
#: the GUI thread.
_PROGRESS_STEPS = 100


class TimeLapseResult(ProcessingResult):
    """A lapse as one lazy ``(T, ...)`` stack, with what each point is.

    ``data`` is a :class:`LazyTimeLapseArray`: viewing a timepoint reads that
    timepoint, and saving streams one timepoint at a time. ``times_s`` is the
    T coordinate of every plane -- the planned schedule or the actual start
    times, whichever the stack was built on -- and ``points`` keeps both for
    every point, with its completion state.
    """

    #: Rows for the points worth a second look -- incomplete, missing, late --
    #: which is none at all for a clean lapse.
    publishes_table_rows = True

    def __init__(self, *args, plan: TimeLapsePlan, time_axis: str,
                 times_s: list[float] | None, points: list[dict[str, Any]],
                 metadata: dict[str, Any], **kwargs):
        super().__init__(*args, **kwargs)
        self.plan = plan
        self.time_axis = time_axis
        self.times_s = times_s
        self.points = points
        self.metadata = metadata

    def write_files(self, plan, document) -> None:
        """Streamed through the shared image writer, one timepoint at a time,
        with every plane's own time beside the T scale."""
        save_image_result(
            self, plan.primary, plan.fmt, document=document, times_s=self.times_s
        )

    def table_columns(self) -> list[str]:
        return [
            "lapse", "detector", "point", "state", "planned_s", "actual_s",
            "delay_s", "note",
        ]

    def table_records(self) -> list[dict[str, Any]]:
        lapse = self.plan.index.name
        rows = []
        for point in self.points:
            if point["state"] == TIMEPOINT_COMPLETE and not point.get("late"):
                continue
            note = point.get("reason") or ""
            if point.get("late"):
                note = "; ".join(filter(None, [note, "started late"]))
            rows.append({
                "lapse": lapse,
                "detector": self.plan.detector,
                "point": point["lapse_index"],
                "state": point["state"],
                "planned_s": point.get("planned_s"),
                "actual_s": point.get("actual_s"),
                "delay_s": point.get("delay_s"),
                "note": note,
            })
        return rows


class _TimeLapseParamsWidget(QtWidgets.QWidget):
    """Which detector, which T axis, and what to do with incomplete points."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Stacks a camera or scan time lapse along T.\n"
            "Open any one timepoint of it."
        ))

        form = QtWidgets.QFormLayout()
        self.detectorCombo = QtWidgets.QComboBox()
        self.detectorCombo.setToolTip(
            "Which detector's lapse to stack, for a lapse that recorded several."
        )
        form.addRow("Detector:", self.detectorCombo)

        self.timeAxisCombo = QtWidgets.QComboBox()
        self.timeAxisCombo.addItem("Planned interval", TIME_AXIS_PLANNED)
        self.timeAxisCombo.addItem("Actual start times", TIME_AXIS_ACTUAL)
        self._timeAxisTip = (
            "Planned interval: T is evenly spaced at the interval the lapse\n"
            "was set up with.\n"
            "Actual start times: T follows when each point really started;\n"
            "the viewer spaces planes by the typical step between them, and an\n"
            "export records every plane's own time.\n\n"
            "Either way, both times are kept for every point, and a point that\n"
            "started late is listed in the Results table."
        )
        self.timeAxisCombo.setToolTip(self._timeAxisTip)
        form.addRow("Time axis:", self.timeAxisCombo)

        self.incompleteCombo = QtWidgets.QComboBox()
        self.incompleteCombo.addItem("Mark in place", INCOMPLETE_MARK)
        self.incompleteCombo.addItem("Skip at the end", INCOMPLETE_SKIP)
        self.incompleteCombo.setToolTip(
            "A stopped lapse can end on a point that is short or empty.\n\n"
            "Mark in place: keep it, with the frames it never recorded (or\n"
            "the whole point, if it is missing) left blank, and list it in the\n"
            "Results table.\n"
            "Skip at the end: leave incomplete points off the end of the stack.\n\n"
            "A point missing in the middle is always kept and marked: leaving\n"
            "it out would move every later point onto the wrong time."
        )
        form.addRow("Incomplete points:", self.incompleteCombo)
        layout.addLayout(form)

        self.sourceStatus = QtWidgets.QLabel()
        self.sourceStatus.setWordWrap(True)
        layout.addWidget(self.sourceStatus)
        layout.addStretch()

    def get_values(self) -> dict:
        return {
            "detector": self.detectorCombo.currentData() or None,
            "time_axis": self.timeAxisCombo.currentData(),
            "incomplete": self.incompleteCombo.currentData(),
        }

    def set_source_inspection(self, inspection: SourceInspection | None) -> None:
        previous = self.detectorCombo.currentData()
        self.detectorCombo.blockSignals(True)
        self.detectorCombo.clear()
        choices = inspection.choices.get("detector", ()) if inspection else ()
        default_index = 0
        for position, choice in enumerate(choices):
            self.detectorCombo.addItem(choice.label, choice.value)
            if choice.metadata.get("default"):
                default_index = position
        index = self.detectorCombo.findData(previous)
        self.detectorCombo.setCurrentIndex(index if index >= 0 else default_index)
        self.detectorCombo.blockSignals(False)
        self.detectorCombo.setEnabled(len(choices) > 1)

        metadata = inspection.metadata if inspection else {}
        planned = bool(metadata.get("planned_interval_available", True))
        item = self.timeAxisCombo.model().item(0)
        if item is not None:
            item.setEnabled(planned)
        if not planned:
            self.timeAxisCombo.setCurrentIndex(1)
            self.timeAxisCombo.setToolTip(
                "This lapse recorded no planned interval -- a scan lapse waits a "
                "delay after each scan rather than keeping a schedule -- so T "
                "follows the actual start times.\n\n" + self._timeAxisTip
            )
        else:
            self.timeAxisCombo.setToolTip(self._timeAxisTip)

        parts = [str(metadata["summary"])] if metadata.get("summary") else []
        if inspection is not None and inspection.warning:
            parts.append(str(inspection.warning))
        self.sourceStatus.setText(" ".join(parts))


class TimeLapseReconstructor(Reconstructor):
    """Stack a saved camera or scan time lapse into one lazy T-stack."""

    name = "Time lapse"
    id = "time-lapse"
    file_extensions = ["hdf5", "h5", "tiff", "tif", "ome.tiff", "zarr"]
    description = "Stack a camera or scan time lapse along T, read as viewed"
    default_save_subdir = "timelapse"
    accepted_source_kinds = ("image", TIME_LAPSE_SOURCE_KIND)
    # Reading every point's header opens every file of a multi-file lapse.
    execution_policy = "worker"

    @classmethod
    def default_params(cls) -> dict:
        # No detector means the one whose item was opened.
        return {
            "detector": None,
            "time_axis": TIME_AXIS_PLANNED,
            "incomplete": INCOMPLETE_MARK,
        }

    def __init__(self):
        super().__init__()
        self._logger = initLogger(self)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _TimeLapseParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget):
        # Everything the stack needs is recorded on its items.
        return None

    @staticmethod
    def _index_for(data_obj: "DataObj") -> TimeLapseIndex:
        """The lapse behind ``data_obj``: opened as one, or found from one item."""
        index = getattr(data_obj, "sourceMetadata", None)
        if isinstance(index, TimeLapseIndex):
            return index
        path = getattr(data_obj, "dataPath", None) or getattr(data_obj, "name", None)
        if not path:
            raise NotATimeLapse(
                "The Time lapse reconstructor needs a file on disk: open one "
                "timepoint of a saved lapse."
            )
        return discover_time_lapse(path, getattr(data_obj, "datasetName", None))

    def inspect_source(self, data_obj: "DataObj") -> SourceInspection:
        """Describe the lapse from discovery alone; no sibling is opened."""
        source_kind = getattr(data_obj, "sourceKind", "image")
        try:
            index = self._index_for(data_obj)
        except NotATimeLapse as exc:
            return SourceInspection(source_kind=source_kind, warning=str(exc))
        except Exception as exc:
            return SourceInspection(
                source_kind=source_kind,
                warning=f"Could not read this source as a time lapse: {exc}",
            )

        choices = tuple(
            SourceChoice(
                value=channel.detector,
                label=f"{channel.label} ({len(channel.candidates)} points)",
                metadata={"default": channel.detector == index.anchor_detector},
            )
            for channel in index.channels
        )
        found = len(index.channel().candidates)
        warning = None
        if found < index.planned_count:
            warning = (
                f"{found} of {index.planned_count} planned points were recorded; "
                "the lapse was stopped, or points are missing."
            )
        return SourceInspection(
            source_kind=source_kind,
            choices={"detector": choices},
            metadata={
                "summary": index.describe(),
                "planned_count": index.planned_count,
                "found": found,
                "interval_s": index.interval_s,
                "storage": index.storage,
                "planned_interval_available": _usable_interval(index.interval_s),
            },
            warning=warning,
        )

    def process(
        self,
        data_obj: "DataObj",
        params: dict,
        context: ReconstructionContext | None = None,
    ) -> TimeLapseResult:
        def report(phase, completed, total, message):
            if context is not None:
                context.report(phase, completed, total, message)

        def check_cancelled():
            if context is not None:
                context.check_cancelled()

        report("inspect", 0, 1, "Finding the lapse")
        index = self._index_for(data_obj)
        # Listed again rather than trusted from when it was opened: a lapse
        # still recording has gained points since, and this is cheap.
        index = discover_time_lapse(index.anchor.path, index.anchor.dataset)
        detector = params.get("detector") or index.anchor_detector
        try:
            index.channel(detector)
        except KeyError as exc:
            raise ValueError(str(exc.args[0])) from exc

        step = max(1, len(index.channel(detector).candidates) // _PROGRESS_STEPS)

        def header_progress(done, total):
            if done == total or done % step == 0:
                report("inspect", done, total, f"Read {done} of {total} timepoint headers")

        headers = read_lapse_headers(
            index, detector,
            progress=header_progress,
            check_cancelled=check_cancelled,
        )
        report("assemble", 0, 1, "Placing timepoints")
        plan = plan_time_lapse(
            index, headers, incomplete=params.get("incomplete") or INCOMPLETE_MARK
        )
        check_cancelled()
        result = build_time_lapse_result(
            plan, time_axis=params.get("time_axis") or TIME_AXIS_PLANNED
        )
        for line in describe_time_lapse_result(result):
            self._logger.info(line)
        report("finalize", 1, 1, "Time lapse ready")
        return result


def _usable_interval(interval_s: float | None) -> bool:
    return interval_s is not None and float(interval_s) > 0


def _median_step(times: list[float | None], positions: list[int]) -> float | None:
    """Typical time per lapse index, from consecutive known times."""
    known = [
        (position, time)
        for position, time in zip(positions, times)
        if time is not None
    ]
    steps = [
        (later - earlier) / (later_position - earlier_position)
        for (earlier_position, earlier), (later_position, later)
        in zip(known, known[1:])
        if later_position > earlier_position
    ]
    steps = [step for step in steps if step > 0]
    return float(np.median(steps)) if steps else None


def build_time_lapse_result(plan: TimeLapsePlan, *, time_axis: str = TIME_AXIS_PLANNED) -> TimeLapseResult:
    """Wrap a planned lapse as a result, with the chosen T axis.

    The planned axis needs a recorded interval; a lapse without one -- every
    scan lapse -- falls back to the actual start times, and one with fewer than
    two of those to a plain index. What was used is recorded in the result.
    """
    index = plan.index
    positions = [slot.lapse_index for slot in plan.slots]
    first = positions[0] if positions else 0
    interval = float(index.interval_s) if _usable_interval(index.interval_s) else None
    uniform = (
        [(position - first) * interval for position in positions]
        if interval is not None else [None] * len(positions)
    )
    scheduled = list(plan.planned_times_s())
    actual = list(plan.actual_times_s())
    actual_step = _median_step(actual, positions)
    # Actual times count from the first point that recorded one. When that is
    # not point 0 -- the lapse's first point is missing -- it is moved to where
    # the typical step puts it, or it and the blank point 0 would share t = 0.
    first_known = next(
        (position for position, time in zip(positions, actual) if time is not None),
        None,
    )
    actual_origin_estimated = bool(
        first_known is not None and first_known != first and actual_step
    )
    if actual_origin_estimated:
        offset = (first_known - first) * actual_step
        actual = [time + offset if time is not None else None for time in actual]

    requested = time_axis
    if time_axis == TIME_AXIS_PLANNED and interval is None:
        time_axis = TIME_AXIS_ACTUAL
    if time_axis == TIME_AXIS_ACTUAL and actual_step is None:
        time_axis = TIME_AXIS_PLANNED if interval is not None else TIME_AXIS_INDEX

    if time_axis == TIME_AXIS_PLANNED:
        scale = interval
        times = uniform
    elif time_axis == TIME_AXIS_ACTUAL:
        scale = actual_step
        # A point with no start time -- one that is missing -- sits where the
        # typical step puts it, and its entry below says the time is estimated.
        times = [
            time if time is not None else (position - first) * actual_step
            for position, time in zip(positions, actual)
        ]
    else:
        scale = 1.0
        times = None

    points = []
    for slot, position, planned_time, uniform_time, actual_time in zip(
        plan.slots, positions, scheduled, uniform, actual
    ):
        planned_value = planned_time if planned_time is not None else uniform_time
        delay = _delay_s(slot, planned_value, actual_time)
        late = (
            delay is not None and interval is not None
            and delay > max(LATE_FRACTION_OF_INTERVAL * interval, LATE_MINIMUM_S)
        )
        points.append({
            "lapse_index": position,
            "state": slot.state,
            "reason": slot.reason,
            "planned_s": _rounded(planned_value),
            "actual_s": _rounded(actual_time),
            "delay_s": _rounded(delay),
            "late": bool(late),
            "time_estimated": (
                time_axis == TIME_AXIS_ACTUAL and actual_time is None
            ),
            "item": _item_label(slot),
        })

    labels = ["T", *plan.item_axis_labels]
    scales = [float(scale), *plan.item_axis_scales]
    stack = LazyTimeLapseArray(plan)
    incomplete = [point for point in points if point["state"] != TIMEPOINT_COMPLETE]
    name = f"{index.name} {plan.detector} time lapse"
    flags = []
    if incomplete:
        flags.append(f"{len(incomplete)} of {len(points)} marked")
    if plan.skipped:
        flags.append(f"{len(plan.skipped)} skipped")
    if flags:
        name += f" ({', '.join(flags)})"

    metadata = {
        "time_lapse": {
            "kind": "imswitch-time-lapse/1",
            "source": str(index.anchor.path),
            "storage": index.storage,
            "detector": plan.detector,
            "planned_count": index.planned_count,
            "stacked": len(points),
            "time_axis": time_axis,
            "time_axis_requested": requested,
            "interval_s": interval,
            "actual_step_s": actual_step,
            # Point 0 recorded no start time; the actual times are placed by
            # the typical step from the first point that did.
            "actual_origin_estimated": actual_origin_estimated,
            "incomplete_points": plan.incomplete,
            "points": points,
            "skipped": [
                {"lapse_index": slot.lapse_index, "state": slot.state,
                 "reason": slot.reason}
                for slot in plan.skipped
            ],
            "excluded": [
                {"item": _ref_label(ref), "reason": reason}
                for ref, reason in plan.excluded
            ],
        }
    }
    return TimeLapseResult(
        name=name,
        data=stack,
        axis_labels=labels,
        view_modes=[ViewMode("Standard", tuple(range(len(labels))))],
        axis_scales=scales,
        scale_unit=plan.scale_unit,
        plan=plan,
        time_axis=time_axis,
        times_s=times,
        points=points,
        metadata=metadata,
    )


def _delay_s(slot, planned_value, actual_time) -> float | None:
    """How late a point started against its own schedule.

    The recorded schedule and start are compared directly when both exist, so
    a first point that started late is not hidden by measuring every later one
    from it.
    """
    if slot.planned_start is not None and slot.started_at is not None:
        return (slot.started_at - slot.planned_start).total_seconds()
    if planned_value is None or actual_time is None:
        return None
    return actual_time - planned_value


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _ref_label(ref) -> str:
    return f"{ref.path.name}::{ref.dataset}" if ref.dataset else ref.path.name


def _item_label(slot) -> str | None:
    if slot.ref is None:
        return None
    dataset = slot.dataset or slot.ref.dataset
    return f"{slot.ref.path.name}::{dataset}" if dataset else slot.ref.path.name


def describe_time_lapse_result(result: TimeLapseResult) -> list[str]:
    """Log lines saying what the stack is and what in it needs a look."""
    info = result.metadata["time_lapse"]
    lines = [
        f"{result.name}: {info['stacked']} of {info['planned_count']} planned "
        f"points, T from the {info['time_axis']} times"
        + (
            f" (every {result.axis_scales[0]:g} s)"
            if info["time_axis"] != TIME_AXIS_INDEX else ""
        )
        + "."
    ]
    if info["time_axis"] != info["time_axis_requested"]:
        lines.append(
            f"No {info['time_axis_requested']} times were recorded for this "
            f"lapse; T uses the {info['time_axis']} times instead."
        )
    for point in info["points"]:
        if point["state"] == TIMEPOINT_MISSING:
            lines.append(
                f"Point {point['lapse_index']} is missing ({point['reason']}); "
                "it is a blank plane at its own time."
            )
        elif point["state"] != TIMEPOINT_COMPLETE:
            lines.append(
                f"Point {point['lapse_index']} is incomplete ({point['reason']}); "
                "frames it never recorded are blank."
            )
    late = [point for point in info["points"] if point["late"]]
    if late:
        worst = max(late, key=lambda point: point["delay_s"])
        lines.append(
            f"{len(late)} point(s) started late; the latest, point "
            f"{worst['lapse_index']}, by {worst['delay_s']:.3g} s."
        )
    for entry in info["skipped"]:
        lines.append(
            f"Point {entry['lapse_index']} was left off the end "
            f"({entry['reason'] or entry['state']})."
        )
    for entry in info["excluded"]:
        lines.append(f"Not stacked: {entry['item']} {entry['reason']}.")
    return lines
