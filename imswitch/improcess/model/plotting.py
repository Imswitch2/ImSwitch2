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
