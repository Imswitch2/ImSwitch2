"""Measurement rows as CSV, shared by imcontrol and ImProcess.

The ImProcess Results table's formatting, defined here so that a profile saved
from imcontrol — which has no Results table — is written exactly the way
ImProcess writes it. ``imswitch.improcess.view.ResultsTableWidget`` re-exports
the same functions.
"""

from __future__ import annotations

import csv
import io

import numpy as np


def merge_columns(existing: list[str], incoming) -> list[str]:
    """Returns a NEW list: existing columns in order, then any incoming columns not already present (stringified, deduped)."""
    result = list(existing)
    for column in incoming:
        column = str(column)
        if column not in result:
            result.append(column)
    return result


def format_table_value(value) -> str:
    """None→""; NaN→"" (guard `value != value`); float→f"{v:.6g}"; else str(value)."""
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except Exception:
        pass
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def records_to_csv(columns: list[str], records: list[dict]) -> str:
    """CSV text with header row = columns and one row per record using format_table_value(record.get(col, ""))."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(columns)
    for record in records:
        row = [format_table_value(record.get(col, "")) for col in columns]
        writer.writerow(row)
    return output.getvalue()


def series_to_csv(series) -> str:
    """The plotted curves themselves, as CSV: an x and a y column per curve.

    ``series`` is ``(name, x, y)`` triples. The summary rows above say what a
    profile measured; this is the profile, for plotting elsewhere. Curves of
    different lengths (the two projections of a non-square rectangle, two
    detectors with different pixel counts) share one file, so shorter columns
    are padded with empty cells rather than the longer ones being cut.
    """
    series = [
        (
            str(name),
            np.asarray(x, dtype=float).ravel(),
            np.asarray(y, dtype=float).ravel(),
        )
        for name, x, y in series
    ]
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    header = []
    for name, _x, _y in series:
        header.extend([f"{name} x", f"{name} y"])
    writer.writerow(header)
    rows = max((max(x.size, y.size) for _name, x, y in series), default=0)
    for index in range(rows):
        row = []
        for _name, x, y in series:
            row.append(format_table_value(float(x[index])) if index < x.size else "")
            row.append(format_table_value(float(y[index])) if index < y.size else "")
        writer.writerow(row)
    return output.getvalue()


__all__ = ["format_table_value", "merge_columns", "records_to_csv", "series_to_csv"]
