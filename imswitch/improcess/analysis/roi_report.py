"""Turning measured ROIs into rows, plots and files.

The panel measures; this module decides what a *row* looks like, what a Multi
Plot series is, and how a long-form table folds into the wide form ImageJ users
expect. Kept apart from both the measuring and the widget so the shape of a
published row can be tested without a viewer, and so Measure, Multi Measure and
the CSV export cannot disagree about it.

Long form is the storage form (Q-02a): one row per (ROI, plane), with the plane
written out as one column per stepped axis. Wide form — one row per plane, one
column per ROI — is derived on export, not kept in parallel.
"""

from __future__ import annotations

import numpy as np

from imswitch.improcess.model.plotting import PlotPayload, PlotSeries

#: Columns every published row starts with, in this order (§4.8). Identity
#: first because the Results dock is an accumulating log: without ``source``
#: and ``roi`` two runs are distinguishable only by their numbers, and without
#: ``frame_uid`` and ``roi_revision`` a row cannot be traced back to the plane
#: and the version of the ROI it was actually measured on.
IDENTITY_COLUMNS = (
    "source", "kind", "label", "roi", "roi_uid", "roi_revision", "frame_uid",
)

#: The value of the ``kind`` column, so rows from this panel can be told apart
#: from Profile's and ROI statistics' in the shared table.
ROW_KIND = "roi-measure"


def measurement_row(
    roi, values: dict, *, source: str, plane=(), frame_uid: str = "",
    label: bool = False,
) -> dict:
    """One published row for one ROI on one plane.

    Built here and nowhere else, from *values* that carry no identity of their
    own — which is what lets the measurement cache hold values keyed by
    identity without a cached row carrying a stale name into a later run.
    """
    row = {
        "source": str(source or ""),
        "kind": ROW_KIND,
        "roi": str(getattr(roi, "name", "")),
        "roi_uid": str(getattr(roi, "uid", "")),
        "roi_revision": int(getattr(roi, "revision", 0) or 0),
        "frame_uid": str(frame_uid or getattr(roi, "frame_uid", "") or ""),
    }
    if label:
        # ImageJ's *Display label*, additive: the machine-readable identity
        # columns above are required on every row either way (§4.8), so this
        # is a convenience for reading the table, never the identity itself.
        row["label"] = f"{row['source']}:{row['roi']}"
    for label, index in plane:
        row[str(label)] = int(index)
    row.update(values)
    return row


def roi_belongs_on_plane(roi, plane) -> bool:
    """Whether an ROI applies to this slice.

    ImageJ's default is that an ROI applies to every slice, and an ROI with no
    recorded position keeps that. One that *was* associated with a slice —
    which is what "Associate with slices" records — belongs to that slice
    only: measuring it on all 500 planes of a stack reports 500 numbers for a
    region that was only ever drawn on one.

    Axes the ROI says nothing about are not constraints: an ROI bound to
    ``Z=12`` is measured at every ``T`` on that ``Z``.
    """
    position = dict(getattr(roi, "position", ()) or ())
    if not position:
        return True
    for label, index in plane:
        bound = position.get(str(label))
        if bound is not None and int(bound) != int(index):
            return False
    return True


def rows_to_columns(rows) -> list[str]:
    """Column order for a set of rows: identity, then plane, then measurements.

    Derived from the rows rather than fixed, because which measurements are in
    them is the user's choice — but the leading columns are pinned, so a table
    does not reorder itself when a different measurement set is published.
    """
    columns: list[str] = []
    for column in IDENTITY_COLUMNS:
        if any(column in row for row in rows):
            columns.append(column)
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    return columns


def multi_plot_payload(rows, *, column: str, axis_label: str, unit: str = "") -> PlotPayload:
    """One series per ROI: the measurement against the stepped axis.

    Raises rather than plotting an empty graph — a Multi Plot that silently
    produces nothing looks identical to one whose numbers are all zero.
    """
    if not rows:
        raise ValueError("Nothing measured yet; run Multi Measure first.")
    series: list[PlotSeries] = []
    for name in dict.fromkeys(str(row.get("roi", "")) for row in rows):
        points = [
            (row.get(axis_label), row.get(column))
            for row in rows
            if str(row.get("roi", "")) == name
        ]
        pairs = [
            (float(x), float(y))
            for x, y in points
            if isinstance(x, (int, float))
            and not isinstance(x, bool)
            and isinstance(y, (int, float))
            and not isinstance(y, bool)
            and np.isfinite(float(y))
        ]
        if not pairs:
            continue
        pairs.sort(key=lambda pair: pair[0])
        series.append(
            PlotSeries(
                name=name,
                x=np.asarray([pair[0] for pair in pairs], dtype=float),
                y=np.asarray([pair[1] for pair in pairs], dtype=float),
            )
        )
    if not series:
        raise ValueError(f"No numeric {column!r} values to plot.")
    label = f"{column} ({unit})" if unit and unit != "px" else column
    return PlotPayload(
        title=f"{column} along {axis_label}",
        x_label=axis_label,
        y_label=label,
        series=series,
        metadata={"kind": ROW_KIND, "column": column, "axis": axis_label},
    )


def wide_form(rows, *, column: str, axis_label: str) -> tuple[list[str], list[dict]]:
    """Long-form rows folded into ImageJ's Multi Measure layout.

    One row per plane, one column per ROI. Derived on demand rather than stored
    beside the long form: two representations of the same measurement are two
    things that can disagree.
    """
    if not rows:
        return [axis_label], []
    names = list(dict.fromkeys(str(row.get("roi", "")) for row in rows))
    planes = sorted(
        {row.get(axis_label) for row in rows if row.get(axis_label) is not None},
        key=lambda value: (isinstance(value, str), value),
    )
    columns = [axis_label, *names]
    records = []
    for plane in planes:
        record: dict = {axis_label: plane}
        for name in names:
            for row in rows:
                if str(row.get("roi", "")) == name and row.get(axis_label) == plane:
                    record[name] = row.get(column)
                    break
        records.append(record)
    return columns, records


__all__ = [
    "IDENTITY_COLUMNS",
    "ROW_KIND",
    "measurement_row",
    "multi_plot_payload",
    "roi_belongs_on_plane",
    "rows_to_columns",
    "wide_form",
]
