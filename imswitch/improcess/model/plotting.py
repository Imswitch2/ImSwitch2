"""Small plotting data contracts for ImProcess results."""

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np


PlotKind = Literal["line", "scatter", "histogram", "image"]


@dataclass(frozen=True)
class PlotSeries:
    """One plot series in a result graph."""

    name: str
    y: np.ndarray
    x: np.ndarray | None = None
    kind: PlotKind = "line"
    style: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlotPayload:
    """A complete graph that can be rendered by the ImProcess graph widget."""

    title: str
    x_label: str = ""
    y_label: str = ""
    series: list[PlotSeries] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def build_delta_x_record(
    title: str,
    x_axis: str,
    x_1: float,
    x_2: float,
    *,
    kind: str = "graph-delta-x",
) -> dict[str, Any]:
    """Build one CSV/table-ready manual horizontal-distance measurement."""
    start, end = sorted((float(x_1), float(x_2)))
    return {
        "kind": str(kind),
        "plot": str(title),
        "x_axis": str(x_axis or "x"),
        "x_1": start,
        "x_2": end,
        "delta_x": end - start,
    }


def build_graph_delta_x_record(
    payload: PlotPayload,
    x_1: float,
    x_2: float,
) -> dict[str, Any]:
    """Build a manual horizontal-distance record for a graph payload."""
    return build_delta_x_record(payload.title, payload.x_label, x_1, x_2)


def payload_summary_records(payload: PlotPayload) -> list[dict[str, Any]]:
    """Table rows describing a plot: its parameters, one row per curve.

    An analysis that fits something reports the fit in
    ``PlotPayload.metadata`` — resolution, time constants, R². Nothing renders
    that dict, so those numbers are visible only as a drawn line unless they
    can be read out here. Scalar metadata is merged into every row so a row
    stands on its own in the shared table; image series are skipped, having no
    meaningful one-line summary.
    """
    scalars = {
        key: value
        for key, value in (payload.metadata or {}).items()
        if isinstance(value, (int, float, str, bool)) and not isinstance(value, bool)
    }
    records = []
    for series in payload.series:
        if series.kind == "image":
            continue
        y = np.asarray(series.y, dtype=float).ravel()
        x = (
            np.asarray(series.x, dtype=float).ravel()
            if series.x is not None
            else np.arange(y.size, dtype=float)
        )
        finite = np.isfinite(y)
        record: dict[str, Any] = {
            "kind": "graph-series",
            "plot": str(payload.title),
            "series": str(series.name),
            "x_axis": str(payload.x_label or "x"),
            "n_points": int(y.size),
        }
        if x.size:
            record["x_min"] = float(np.nanmin(x))
            record["x_max"] = float(np.nanmax(x))
        if np.any(finite):
            record["y_min"] = float(np.nanmin(y[finite]))
            record["y_max"] = float(np.nanmax(y[finite]))
            record["y_mean"] = float(np.nanmean(y[finite]))
        record.update(scalars)
        records.append(record)
    return records
