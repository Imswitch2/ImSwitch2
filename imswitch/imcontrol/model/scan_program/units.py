"""Units in words, shared by the model and the panels: a duration in the
unit that reads best. Qt-free, and imported by nothing but leaf modules, so
``program`` can word a refusal without importing the schedule."""

from __future__ import annotations


def format_seconds(seconds) -> str:
    """A duration in the unit that reads best (``4.03 ms``)."""
    seconds = float(seconds)
    if seconds == 0:
        return '0 s'
    for unit, factor in (('s', 1.0), ('ms', 1e-3), ('µs', 1e-6), ('ns', 1e-9)):
        if abs(seconds) >= factor or unit == 'ns':
            return f'{seconds / factor:.6g} {unit}'


__all__ = ['format_seconds']
