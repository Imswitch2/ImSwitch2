"""Build plot payloads from generic results-table data.

Turns ``(columns, records, spec)`` — the column list and list-of-dict records a
:class:`~imswitch.improcess.view.ResultsTableWidget.ResultsTableWidget` already
holds — into :class:`PlotPayload`\\ s rendered by the shared ``GraphWidget``.
This keeps all results-table plotting (histogram, X/Y line or scatter, 2D
histogram, PCA/UMAP) in one Qt-free, unit-testable place instead of being
re-implemented per panel.

``spec`` is a plain dict with a ``"kind"`` key:

* ``"histogram"`` — ``{"column": str, "bins"?: int}``
* ``"line"`` / ``"scatter"`` — ``{"x_column": str, "y_column": str}``
* ``"hist2d"`` — ``{"x_column": str, "y_column": str, "bins"?: int}``
* ``"pca"`` — ``{"columns": [str, ...], "standardize"?: bool}``
* ``"umap"`` — ``{"columns": [str, ...], "standardize"?: bool,
  "n_neighbors"?: int, "min_dist"?: float}``

Invalid specs raise :class:`ValueError` (or :class:`RuntimeError` when an
optional dependency such as ``umap-learn`` is missing) with a user-facing
message the caller can surface in the UI.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .plotting import PlotPayload, PlotSeries

_DEFAULT_BINS = 50
_DEFAULT_UMAP_NEIGHBORS = 15
_DEFAULT_UMAP_MIN_DIST = 0.1


def column_values(records: list[dict], column: str) -> np.ndarray:
    """Numeric column as a float array; non-numeric / missing cells become NaN.

    Booleans are treated as non-numeric so flag columns never leak into plots.
    """
    out = np.empty(len(records), dtype=float)
    for index, record in enumerate(records):
        value = record.get(column)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            out[index] = np.nan
        else:
            out[index] = float(value)
    return out


def numeric_columns(columns: list[str], records: list[dict]) -> list[str]:
    """Columns that hold at least one finite numeric value, in input order."""
    result = []
    for column in columns:
        values = column_values(records, column)
        if np.any(np.isfinite(values)):
            result.append(column)
    return result


#: Length units the table understands, as multiples of a metre. Rows measured
#: in different ones can be compared only after conversion; rows measured in
#: something not on this list cannot be compared at all.
_LENGTH_UNITS = {
    "m": 1.0,
    "cm": 1e-2,
    "mm": 1e-3,
    "um": 1e-6,
    "µm": 1e-6,
    "μm": 1e-6,
    "nm": 1e-9,
    "pm": 1e-12,
    "A": 1e-10,
    "Å": 1e-10,
}

#: Column that records what a row's calibrated values are in.
UNIT_COLUMN = "spatial_unit"


def _unit_kind(column: str) -> str:
    """Whether a calibrated column is a length or an area, from its name."""
    if column.endswith("_cal"):
        return "area" if column.startswith("area") or "int_den" in column else "length"
    return ""


def harmonize_units(records: list[dict], columns) -> list[dict]:
    """Rows with their calibrated columns converted to one common unit.

    Rows from a nm result and a µm result share the column ``area_cal``, and
    the generic plot builders would happily draw them on one axis. Here the
    rows are converted to the smallest unit present, or the request is refused
    by name — a silent factor of a thousand is worse than no plot (A-20).

    Rows with no ``spatial_unit`` are left alone: a table that never recorded
    one is not a table with a conflict in it.
    """
    wanted = [column for column in columns if column and _unit_kind(column)]
    if not wanted:
        return records

    units = {
        str(record.get(UNIT_COLUMN) or "").strip()
        for record in records
        if record.get(UNIT_COLUMN)
    }
    units.discard("")
    if len(units) <= 1:
        return records
    # "px" is deliberately *not* discarded before the conflict check. It is a
    # unit like any other here, and the one thing it certainly is not is
    # convertible to micrometres — so a table holding both must be refused,
    # not silently plotted on one axis.

    unknown = sorted(unit for unit in units if unit not in _LENGTH_UNITS)
    if unknown:
        raise ValueError(
            "Rows are measured in different units that cannot be converted "
            f"({', '.join(sorted(units))}); plot them separately."
        )

    target = min(units, key=lambda unit: _LENGTH_UNITS[unit])
    converted = []
    for record in records:
        unit = str(record.get(UNIT_COLUMN) or "").strip()
        factor = _LENGTH_UNITS.get(unit)
        if not factor or unit == target:
            converted.append(record)
            continue
        ratio = factor / _LENGTH_UNITS[target]
        row = dict(record)
        for column in wanted:
            value = row.get(column)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            row[column] = float(value) * (ratio ** (2 if _unit_kind(column) == "area" else 1))
        row[UNIT_COLUMN] = target
        converted.append(row)
    return converted


def _plotted_columns(spec: dict[str, Any]) -> list[str]:
    """The columns a spec will read, whatever kind of plot it asks for."""
    named = [
        str(spec.get(key, "") or "")
        for key in ("column", "x_column", "y_column")
    ]
    named.extend(str(column) for column in (spec.get("columns") or ()))
    return [column for column in named if column]


def build_plot_payloads(
    columns: list[str], records: list[dict], spec: dict[str, Any]
) -> list[PlotPayload]:
    """Build the plot payload(s) described by ``spec`` from table data."""
    if not records:
        raise ValueError("No rows to plot.")
    kind = str(spec.get("kind", "")).lower()
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"Unknown plot kind: {spec.get('kind')!r}")
    # Done here rather than in each builder, so every panel that plots a table
    # inherits the rule instead of each one remembering it.
    records = harmonize_units(records, _plotted_columns(spec))
    return builder(records, spec)


def _require_column(records: list[dict], column: str, role: str) -> np.ndarray:
    if not column:
        raise ValueError(f"Choose a {role} column.")
    values = column_values(records, column)
    if not np.any(np.isfinite(values)):
        raise ValueError(f"Column {column!r} has no numeric values to plot.")
    return values


def _build_histogram(records: list[dict], spec: dict[str, Any]) -> list[PlotPayload]:
    column = str(spec.get("column", ""))
    values = _require_column(records, column, "histogram")
    finite = values[np.isfinite(values)]
    bins = int(spec.get("bins", _DEFAULT_BINS))
    return [
        PlotPayload(
            title=f"{column} histogram",
            x_label=column,
            y_label="Count",
            series=[
                PlotSeries(name=column, y=finite, kind="histogram", style={"bins": bins})
            ],
            metadata={"n": int(finite.size)},
        )
    ]


def _build_xy(records: list[dict], spec: dict[str, Any], kind: str) -> list[PlotPayload]:
    x_column = str(spec.get("x_column", ""))
    y_column = str(spec.get("y_column", ""))
    x = _require_column(records, x_column, "X")
    y = _require_column(records, y_column, "Y")
    valid = np.isfinite(x) & np.isfinite(y)
    if not np.any(valid):
        raise ValueError("No rows have a numeric value in both columns.")
    x = x[valid]
    y = y[valid]
    if kind == "line":
        order = np.argsort(x, kind="stable")
        x, y = x[order], y[order]
    return [
        PlotPayload(
            title=f"{y_column} vs {x_column}",
            x_label=x_column,
            y_label=y_column,
            series=[PlotSeries(name=f"{y_column} vs {x_column}", x=x, y=y, kind=kind)],
            metadata={"n": int(x.size)},
        )
    ]


def _build_line(records: list[dict], spec: dict[str, Any]) -> list[PlotPayload]:
    return _build_xy(records, spec, "line")


def _build_scatter(records: list[dict], spec: dict[str, Any]) -> list[PlotPayload]:
    return _build_xy(records, spec, "scatter")


def _build_hist2d(records: list[dict], spec: dict[str, Any]) -> list[PlotPayload]:
    x_column = str(spec.get("x_column", ""))
    y_column = str(spec.get("y_column", ""))
    x = _require_column(records, x_column, "X")
    y = _require_column(records, y_column, "Y")
    valid = np.isfinite(x) & np.isfinite(y)
    if not np.any(valid):
        raise ValueError("No rows have a numeric value in both columns.")
    bins = int(spec.get("bins", _DEFAULT_BINS))
    counts, x_edges, y_edges = np.histogram2d(x[valid], y[valid], bins=bins)
    return [
        PlotPayload(
            title=f"{y_column} vs {x_column} (2D histogram)",
            x_label=x_column,
            y_label=y_column,
            series=[
                PlotSeries(
                    name="density",
                    # counts[i, j] is the x_bin i / y_bin j cell; GraphWidget
                    # maps it to world coords via the supplied bin edges.
                    y=counts,
                    kind="image",
                    style={"x_edges": x_edges, "y_edges": y_edges},
                )
            ],
            metadata={"n": int(valid.sum())},
        )
    ]


def _feature_matrix(
    records: list[dict], columns: list[str]
) -> tuple[np.ndarray, list[str]]:
    """Stack the chosen numeric columns into ``(n_rows, n_features)``.

    Rows with a non-finite value in any feature are dropped so the reducers get
    a clean matrix. Returns the matrix and the feature names actually used.
    """
    used = numeric_columns(columns, records)
    if len(used) < 2:
        raise ValueError("Select at least two numeric columns.")
    matrix = np.column_stack([column_values(records, column) for column in used])
    matrix = matrix[np.all(np.isfinite(matrix), axis=1)]
    if matrix.shape[0] < 3:
        raise ValueError("Need at least three complete rows for dimensionality reduction.")
    return matrix, used


def _standardize(matrix: np.ndarray) -> np.ndarray:
    mean = matrix.mean(axis=0, keepdims=True)
    std = matrix.std(axis=0, keepdims=True)
    std[std == 0] = 1.0
    return (matrix - mean) / std


def _pca_embedding(matrix: np.ndarray, n_components: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """PCA scores via SVD (no scikit-learn dependency).

    Returns the ``(n_rows, n_components)`` score matrix and the explained
    variance ratio per returned component.

    SVD component signs are mathematically arbitrary and can flip between
    numpy/LAPACK builds, which would mirror the plot. We pin them with the
    ``svd_flip`` convention (largest-magnitude loading per component made
    positive) so the same data always yields the same embedding.
    """
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    _u, singular, vt = np.linalg.svd(centered, full_matrices=False)
    components = vt[:n_components]
    max_abs_rows = np.argmax(np.abs(components), axis=1)
    signs = np.sign(components[np.arange(components.shape[0]), max_abs_rows])
    signs[signs == 0] = 1.0
    components = components * signs[:, np.newaxis]
    scores = centered @ components.T
    variance = singular**2
    total = variance.sum()
    ratio = variance[:n_components] / total if total > 0 else np.zeros(n_components)
    return scores, ratio


def _build_pca(records: list[dict], spec: dict[str, Any]) -> list[PlotPayload]:
    matrix, used = _feature_matrix(records, list(spec.get("columns", [])))
    if spec.get("standardize", True):
        matrix = _standardize(matrix)
    scores, ratio = _pca_embedding(matrix, 2)
    x_label = f"PC1 ({ratio[0] * 100:.1f}%)"
    y_label = f"PC2 ({ratio[1] * 100:.1f}%)" if ratio.size > 1 else "PC2"
    return [
        PlotPayload(
            title=f"PCA of {len(used)} columns",
            x_label=x_label,
            y_label=y_label,
            series=[
                PlotSeries(
                    name=f"{scores.shape[0]} points",
                    x=scores[:, 0],
                    y=scores[:, 1],
                    kind="scatter",
                )
            ],
            metadata={"columns": used, "explained_variance_ratio": ratio.tolist()},
        )
    ]


def _umap_embedding(
    matrix: np.ndarray, n_neighbors: int, min_dist: float
) -> np.ndarray:
    try:
        import umap  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only without umap-learn
        raise RuntimeError(
            "UMAP requires the optional 'umap-learn' package. "
            "Install it with: pip install umap-learn"
        ) from exc
    reducer = umap.UMAP(
        n_neighbors=min(int(n_neighbors), max(2, matrix.shape[0] - 1)),
        min_dist=float(min_dist),
        n_components=2,
        random_state=42,
    )
    return np.asarray(reducer.fit_transform(matrix))


def _build_umap(records: list[dict], spec: dict[str, Any]) -> list[PlotPayload]:
    matrix, used = _feature_matrix(records, list(spec.get("columns", [])))
    if spec.get("standardize", True):
        matrix = _standardize(matrix)
    embedding = _umap_embedding(
        matrix,
        int(spec.get("n_neighbors", _DEFAULT_UMAP_NEIGHBORS)),
        float(spec.get("min_dist", _DEFAULT_UMAP_MIN_DIST)),
    )
    return [
        PlotPayload(
            title=f"UMAP of {len(used)} columns",
            x_label="UMAP 1",
            y_label="UMAP 2",
            series=[
                PlotSeries(
                    name=f"{embedding.shape[0]} points",
                    x=embedding[:, 0],
                    y=embedding[:, 1],
                    kind="scatter",
                )
            ],
            metadata={"columns": used},
        )
    ]


_BUILDERS = {
    "histogram": _build_histogram,
    "line": _build_line,
    "scatter": _build_scatter,
    "hist2d": _build_hist2d,
    "pca": _build_pca,
    "umap": _build_umap,
}
