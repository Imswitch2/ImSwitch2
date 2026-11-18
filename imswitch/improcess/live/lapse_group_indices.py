"""Physical scan-group numbering is not the logical position in a list.

A missing ``scan1`` must never cause ``scan2`` to become logical T1.  This
module does not infer acquisition completion from missing groups or filenames.
"""

from __future__ import annotations


def scan_group_indices(names) -> tuple[int, ...]:
    """Return ordered *physical* ``scanN`` numbers (not list offsets)."""
    indices = []
    for name in names:
        text = str(name)
        if text.startswith("scan") and text[4:].isdigit():
            indices.append(int(text[4:]))
    if len(indices) != len(set(indices)):
        raise ValueError("Duplicate logical scan-group indices")
    return tuple(sorted(indices))


def next_scan_index(indices, current: int, stop: int | None = None) -> int | None:
    """Earliest observed group after current, inside an exclusive scope bound."""
    return next((idx for idx in indices
                 if idx > current and (stop is None or idx < stop)), None)
