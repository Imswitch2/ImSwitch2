"""Point generators: a run is an ordered sequence of control setpoints.

A grid is one generator among others; its shape and traversal are metadata
of the sequence, not a property of the run engine.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from imswitch.imcommon.model.measurement_run import GridInfo

RASTER = 'raster'
SNAKE = 'snake'


@dataclass(frozen=True)
class PointSequence:
    kind: str
    controls: Tuple[str, ...]
    #: One tuple of values per point, in ``controls`` order, in run order.
    values: Tuple[Tuple[float, ...], ...]
    grid: Optional[GridInfo] = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.values)

    def setpoints(self, index: int) -> Dict[str, float]:
        return dict(zip(self.controls, self.values[index]))

    def grid_index(self, index: int) -> Optional[Tuple[int, ...]]:
        return None if self.grid is None else self.grid.index[index]

    def describe(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {'kind': self.kind, 'controls': list(self.controls),
                                'n_points': len(self), 'parameters': dict(self.parameters)}
        return info


def grid(axes: Sequence[Tuple[str, Sequence[float]]], traversal: str = RASTER) -> PointSequence:
    """Cartesian grid; ``axes`` outermost first. ``snake`` reverses the
    innermost axis on every other line."""
    if traversal not in (RASTER, SNAKE):
        raise ValueError(f'unknown traversal {traversal!r}')
    if not axes:
        raise ValueError('a grid needs at least one axis')
    names = tuple(name for name, _ in axes)
    if len(set(names)) != len(names):
        raise ValueError('grid axes must be distinct controls')
    value_lists = [tuple(float(v) for v in values) for _, values in axes]
    if any(len(v) == 0 for v in value_lists):
        raise ValueError('every grid axis needs at least one value')

    indices = []
    outer_shape = [len(v) for v in value_lists[:-1]]
    inner = len(value_lists[-1])
    for line, outer in enumerate(itertools.product(*[range(n) for n in outer_shape])):
        inner_range = range(inner)
        if traversal == SNAKE and line % 2 == 1:
            inner_range = reversed(range(inner))
        for i in inner_range:
            indices.append(tuple(outer) + (i,))
    values = tuple(
        tuple(value_lists[axis][idx] for axis, idx in enumerate(index))
        for index in indices
    )
    info = GridInfo(axes=tuple(zip(names, value_lists)), traversal=traversal,
                    index=tuple(indices))
    return PointSequence('grid', names, values, grid=info,
                         parameters={'traversal': traversal})


def points(controls: Sequence[str], values: Sequence[Sequence[float]], **parameters) -> PointSequence:
    """An explicit list of setpoints, e.g. fitted predictions to measure."""
    controls = tuple(controls)
    rows = tuple(tuple(float(v) for v in row) for row in values)
    for row in rows:
        if len(row) != len(controls):
            raise ValueError(f'point {row} does not have {len(controls)} values')
    if not rows:
        raise ValueError('a point list needs at least one point')
    return PointSequence('points', controls, rows, parameters=parameters)


def sweep(control: str, values: Sequence[float], **parameters) -> PointSequence:
    """One control over a list of values (e.g. a laser's raw drive)."""
    rows = tuple((float(v),) for v in values)
    if not rows:
        raise ValueError('a sweep needs at least one value')
    return PointSequence('sweep', (control,), rows, parameters=parameters)
