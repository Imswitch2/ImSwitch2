"""Detector-to-alignment transform parsing shared by acquisition and readers.

The tiling manifest records the transform that was believed at acquisition
time.  Both imcontrol (when accepting a detector into a save set) and
improcess (when placing its payload) must interpret that record identically,
so parsing belongs in imcommon and produces one normalized homogeneous matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

import numpy as np


Matrix3x3 = Tuple[
    Tuple[float, float, float],
    Tuple[float, float, float],
    Tuple[float, float, float],
]


@dataclass(frozen=True)
class DetectorTransform:
    """A validated detector-local to alignment-grid affine transform.

    Coordinates are ordered ``(row, column, 1)`` and integer coordinates name
    pixel centres.  ``provenance`` retains the descriptive manifest fields so
    a later reconstruction can say which calibration produced its geometry.
    """

    kind: str
    matrix: Matrix3x3
    source: str | None = None
    calibration_id: str | None = None
    measured: str | None = None
    provenance: Mapping[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )

    def as_array(self) -> np.ndarray:
        """Return a fresh float64 matrix suitable for composition."""
        return np.asarray(self.matrix, dtype=np.float64)

    @property
    def is_identity(self) -> bool:
        return bool(np.array_equal(self.as_array(), np.eye(3, dtype=np.float64)))

    def as_manifest(self) -> dict[str, Any]:
        """Return the normalized schema plus its descriptive provenance."""
        result = dict(self.provenance)
        result.update({
            'kind': self.kind,
            'matrix': [list(row) for row in self.matrix],
        })
        if self.source is not None:
            result['source'] = self.source
        if self.calibration_id is not None:
            result['calibration_id'] = self.calibration_id
        if self.measured is not None:
            result['measured'] = self.measured
        result.pop('declared', None)
        return result


def _matrix_tuple(value: Any, *, label: str) -> Matrix3x3:
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label}: matrix must contain numeric values') from exc

    if matrix.shape != (3, 3):
        raise ValueError(
            f'{label}: matrix must be 3x3, got shape {tuple(matrix.shape)}'
        )
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f'{label}: matrix contains a non-finite value')
    if not np.allclose(matrix[2], (0.0, 0.0, 1.0), rtol=0.0, atol=1e-12):
        raise ValueError(
            f'{label}: matrix must be affine homogeneous with last row [0, 0, 1]'
        )
    if abs(float(np.linalg.det(matrix[:2, :2]))) <= np.finfo(np.float64).eps:
        raise ValueError(f'{label}: matrix is singular')

    return tuple(tuple(float(item) for item in row) for row in matrix)  # type: ignore[return-value]


def parse_detector_transform(
    value: Any,
    *,
    allow_reference: bool = False,
    label: str = 'detector transform',
) -> DetectorTransform:
    """Parse transform shorthand or schema into one validated value.

    ``"identity"`` is the stable shorthand.  ``"reference"`` is accepted
    only when the caller has established that this is the manifest's named
    alignment detector; it is normalized to identity and retained in
    provenance as a compatibility spelling.
    """

    if isinstance(value, str):
        declared = value.strip().lower()
        if declared == 'reference':
            if not allow_reference:
                raise ValueError(
                    f'{label}: "reference" is valid only for the alignment detector'
                )
            return DetectorTransform(
                kind='identity',
                matrix=_matrix_tuple(np.eye(3), label=label),
                provenance={'declared': 'reference'},
            )
        if declared != 'identity':
            raise ValueError(f'{label}: unknown transform kind {value!r}')
        return DetectorTransform(
            kind='identity',
            matrix=_matrix_tuple(np.eye(3), label=label),
            provenance={'declared': 'identity'},
        )

    if not isinstance(value, Mapping):
        raise ValueError(
            f'{label}: expected "identity" or a transform object, '
            f'got {type(value).__name__}'
        )

    declared_kind = str(value.get('kind') or '').strip().lower()
    if declared_kind not in ('identity', 'affine'):
        shown = declared_kind or '<missing>'
        raise ValueError(f'{label}: unknown transform kind {shown!r}')

    if declared_kind == 'identity':
        matrix_value = value.get('matrix', np.eye(3, dtype=np.float64))
        matrix = _matrix_tuple(matrix_value, label=label)
        if not np.array_equal(np.asarray(matrix), np.eye(3, dtype=np.float64)):
            raise ValueError(f'{label}: kind "identity" must contain identity matrix')
    else:
        if 'matrix' not in value:
            raise ValueError(f'{label}: affine transform is missing its matrix')
        matrix = _matrix_tuple(value['matrix'], label=label)

    def optional_text(key: str) -> str | None:
        item = value.get(key)
        return None if item is None else str(item)

    return DetectorTransform(
        kind=declared_kind,
        matrix=matrix,
        source=optional_text('source'),
        calibration_id=optional_text('calibration_id'),
        measured=optional_text('measured'),
        provenance=dict(value),
    )
