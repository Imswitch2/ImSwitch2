"""Hand-clicked point pairs: the lightweight backup when localisation fails.

Click a feature in one image, the same feature in the other, a few times;
fit any of the module's models to those pairs. Everything a point picker
needs that is not drawing lives here, Qt-free:

* :class:`PointPairEditor` -- the list of pairs being clicked: a click goes
  to the open pair or starts a new one, pairs can be deleted, reordered and
  cleared, and every change can be undone.
* :func:`snap_to_local_maximum` -- moves a click onto the brightness centre
  of the spot under it (a bead, a focus), so a click a pixel or two off
  still lands on the feature.
* :func:`check_point_geometry` -- refuses a set the chosen model cannot be
  fitted to honestly: too few pairs, two clicks on the same spot, or points
  on a line for a model with shear (affine, polynomial).
* :func:`fit_point_pairs` -- a least-squares fit over *every* pair (no
  RANSAC: with a handful of clicks each one counts, and a bad click must
  show rather than be dropped), the residual of every pair, and its
  leave-one-out residual -- the distance between where a pair's Moving
  point lands when the transform is fitted to the *other* pairs and where
  it was clicked, which singles out a bad click far better than the plain
  residual, which a least-squares fit spreads over all pairs.
* :func:`manual_correspondence` / :func:`manual_strategy_spec` -- the pairs
  as a :class:`~.registry.CorrespondenceResult`, so the shared fitting and
  persistence path takes them like any other strategy's.

Points are ``(row, col)``, pixel-centred, as everywhere in this module.

Minimum pairs per model (``minimum_pairs``): translation, identity and
rotation90 1; rigid, similarity, similarity (mirror allowed) and auto 2;
affine 3; polynomial 6 / 10 / 15 for degree 2 / 3 / 4. With exactly the
minimum the fit passes through every click and the residuals are zero:
they say something from one pair more.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from ..estimate import ESTIMATORS, ModelFit, fit_transform
from ..model import ResidualStats
from ..models.polynomial import polynomial_term_count
from .registry import CorrespondenceResult, CorrespondenceStrategySpec

__all__ = [
    "DegeneratePointsError",
    "PairFit",
    "PointPair",
    "PointPairEditor",
    "check_point_geometry",
    "fit_point_pairs",
    "manual_correspondence",
    "manual_strategy_spec",
    "minimum_pairs",
    "minimum_pairs_table",
    "snap_to_local_maximum",
]

#: Models that need points spanning two dimensions (they fit a shear).
_NEEDS_SPREAD = ("affine", "polynomial")


class DegeneratePointsError(ValueError):
    """The clicked points cannot determine the chosen model."""


# --------------------------------------------------------------------------
# minimum pairs
# --------------------------------------------------------------------------

def minimum_pairs(model: str, degree: int = 2) -> int:
    """The fewest point pairs ``model`` can be fitted to."""
    try:
        spec = ESTIMATORS[str(model)]
    except KeyError:
        raise ValueError(f"unknown model {model!r}; known: {sorted(ESTIMATORS)}") from None
    if spec.name == "polynomial":
        return polynomial_term_count(int(degree))
    return int(spec.min_samples)


def minimum_pairs_table() -> dict[str, int]:
    """``{model: minimum pairs}`` for every model (polynomial at degree 2)."""
    return {name: minimum_pairs(name) for name in ESTIMATORS}


# --------------------------------------------------------------------------
# snapping
# --------------------------------------------------------------------------

def snap_to_local_maximum(image: Any, point: Sequence[float], radius: float = 3.0) -> tuple[float, float]:
    """The brightness centre of the spot nearest ``point`` within ``radius`` px.

    The image is smoothed lightly (Gaussian, sigma 1 px) so a noise pixel does
    not win, the brightest pixel within ``radius`` of the click is taken, and
    its position is refined to sub-pixel precision by the background-subtracted
    centre of mass of its 5 x 5 neighbourhood. A flat neighbourhood, a click
    outside the image or ``radius`` <= 0 returns the click unchanged."""
    from scipy import ndimage

    data = np.asarray(image, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError(f"snapping needs a 2-D image, got shape {data.shape}")
    row, col = float(point[0]), float(point[1])
    if radius <= 0 or not (-0.5 <= row < data.shape[0] - 0.5 and -0.5 <= col < data.shape[1] - 0.5):
        return row, col
    reach = int(np.ceil(radius)) + 3
    r0, r1 = max(0, int(round(row)) - reach), min(data.shape[0], int(round(row)) + reach + 1)
    c0, c1 = max(0, int(round(col)) - reach), min(data.shape[1], int(round(col)) + reach + 1)
    patch = ndimage.gaussian_filter(data[r0:r1, c0:c1], 1.0, mode="nearest")
    rows, cols = np.mgrid[r0:r1, c0:c1]
    inside = (rows - row) ** 2 + (cols - col) ** 2 <= radius * radius
    if not inside.any():
        return row, col
    values = np.where(inside, patch, -np.inf)
    if not np.isfinite(values).any() or np.nanmax(patch[inside]) - np.nanmin(patch[inside]) <= 0:
        return row, col
    k = int(np.argmax(values))
    peak_r, peak_c = int(rows.flat[k]), int(cols.flat[k])
    w0, w1 = max(0, peak_r - 2), min(data.shape[0], peak_r + 3)
    v0, v1 = max(0, peak_c - 2), min(data.shape[1], peak_c + 3)
    window = data[w0:w1, v0:v1] - data[w0:w1, v0:v1].min()
    total = float(window.sum())
    if total <= 0:
        return float(peak_r), float(peak_c)
    wr, wc = np.mgrid[w0:w1, v0:v1]
    return float((window * wr).sum() / total), float((window * wc).sum() / total)


# --------------------------------------------------------------------------
# the pairs being clicked
# --------------------------------------------------------------------------

@dataclass
class PointPair:
    """One pair: the Moving (``source``) and Reference (``target``) click,
    either ``None`` until clicked."""

    source: tuple[float, float] | None = None
    target: tuple[float, float] | None = None

    @property
    def complete(self) -> bool:
        return self.source is not None and self.target is not None

    def get(self, role: str) -> tuple[float, float] | None:
        return self.source if _role(role) == "source" else self.target


def _role(role: str) -> str:
    value = str(role).lower().strip()
    value = {"moving": "source", "reference": "target"}.get(value, value)
    if value not in ("source", "target"):
        raise ValueError("role must be source/moving or target/reference")
    return value


@dataclass
class PointPairEditor:
    """The pairs of a manual calibration, editable with undo.

    A click (:meth:`add_point`) completes the last pair when that pair lacks
    the clicked image's point, replaces the last pair's point when it is
    still open and already has one in that image (a second try), and starts
    a new pair otherwise -- so the order is free: Moving then Reference, or
    the other way round."""

    pairs: list = field(default_factory=list)
    max_undo: int = 200
    _history: list = field(default_factory=list, repr=False)

    # -- reading ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self.pairs)

    @property
    def open_pair(self) -> int | None:
        """Index of the last pair when it is still missing a point."""
        if self.pairs and not self.pairs[-1].complete:
            return len(self.pairs) - 1
        return None

    def next_role(self) -> str:
        """The image the next click should go to: the open pair's missing
        point, else ``"source"`` (a new pair; either image may start it)."""
        index = self.open_pair
        if index is None:
            return "source"
        return "target" if self.pairs[index].source is not None else "source"

    def complete_pairs(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``(source (N, 2), target (N, 2), indices)`` of the complete pairs."""
        indices = [k for k, pair in enumerate(self.pairs) if pair.complete]
        source = np.array([self.pairs[k].source for k in indices], dtype=np.float64).reshape(-1, 2)
        target = np.array([self.pairs[k].target for k in indices], dtype=np.float64).reshape(-1, 2)
        return source, target, np.asarray(indices, dtype=int)

    @property
    def can_undo(self) -> bool:
        return bool(self._history)

    # -- changing ---------------------------------------------------------
    def _remember(self) -> None:
        self._history.append(copy.deepcopy(self.pairs))
        del self._history[:-self.max_undo]

    def add_point(self, role: str, point: Sequence[float]) -> int:
        """Record a click in ``role``'s image; returns the pair's index."""
        role = _role(role)
        value = (float(point[0]), float(point[1]))
        self._remember()
        index = self.open_pair
        if index is not None:
            setattr(self.pairs[index], role, value)
            return index
        self.pairs.append(PointPair(**{role: value}))
        return len(self.pairs) - 1

    def set_point(self, index: int, role: str, point: Sequence[float] | None) -> None:
        """Replace (or with ``None`` remove) one point of pair ``index``."""
        self._check(index)
        self._remember()
        setattr(self.pairs[index], _role(role),
                None if point is None else (float(point[0]), float(point[1])))

    def delete(self, index: int) -> None:
        self._check(index)
        self._remember()
        del self.pairs[index]

    def move(self, index: int, offset: int) -> int:
        """Move pair ``index`` by ``offset`` places; returns its new index."""
        self._check(index)
        new = min(max(0, index + int(offset)), len(self.pairs) - 1)
        if new != index:
            self._remember()
            self.pairs.insert(new, self.pairs.pop(index))
        return new

    def clear(self) -> None:
        if self.pairs:
            self._remember()
            self.pairs = []

    def undo(self) -> bool:
        """Undo the last change; False when there is nothing to undo."""
        if not self._history:
            return False
        self.pairs = self._history.pop()
        return True

    def set_pairs(self, source: Any, target: Any) -> None:
        """Replace every pair (e.g. pairs restored from a saved record)."""
        source = np.asarray(source, dtype=np.float64).reshape(-1, 2)
        target = np.asarray(target, dtype=np.float64).reshape(-1, 2)
        if source.shape != target.shape:
            raise ValueError("source and target need the same number of points")
        self._remember()
        self.pairs = [PointPair(tuple(map(float, s)), tuple(map(float, t))) for s, t in zip(source, target)]

    def _check(self, index: int) -> None:
        if not 0 <= int(index) < len(self.pairs):
            raise IndexError(f"no pair {index} (there are {len(self.pairs)})")


# --------------------------------------------------------------------------
# geometry checks and the fit
# --------------------------------------------------------------------------

def _spread(points: np.ndarray) -> tuple[float, float]:
    """Largest and smallest standard deviation along the principal axes."""
    if len(points) < 2:
        return 0.0, 0.0
    centred = points - points.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False) / np.sqrt(len(points))
    singular = np.concatenate([singular, np.zeros(2)])[:2]
    return float(singular[0]), float(singular[1])


def check_point_geometry(source: Any, target: Any, model: str, *, degree: int = 2,
                         min_separation: float = 0.5, collinear_ratio: float = 0.02) -> None:
    """Raise :class:`DegeneratePointsError` when ``model`` cannot be fitted
    honestly to these pairs: fewer than :func:`minimum_pairs`; two clicks in
    one image closer than ``min_separation`` px (the same spot twice); for a
    model with a scale or rotation, all clicks of an image on one spot; for
    affine and polynomial, the clicks of an image on (nearly) one line --
    their spread across the line below ``collinear_ratio`` of the spread
    along it -- which leaves the shear across the line undetermined."""
    source = np.asarray(source, dtype=np.float64).reshape(-1, 2)
    target = np.asarray(target, dtype=np.float64).reshape(-1, 2)
    if source.shape != target.shape:
        raise ValueError(f"source and target must match, got {source.shape} and {target.shape}")
    need = minimum_pairs(model, degree)
    if len(source) < need:
        raise DegeneratePointsError(
            f"{model} needs at least {need} point pair{'s' if need != 1 else ''}; {len(source)} so far")
    kind = str(model)
    for label, points in (("Moving", source), ("Reference", target)):
        if len(points) >= 2:
            gaps = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
            gaps[np.diag_indices(len(points))] = np.inf
            i, j = np.unravel_index(int(np.argmin(gaps)), gaps.shape)
            if gaps[i, j] < min_separation:
                raise DegeneratePointsError(
                    f"pairs {min(i, j) + 1} and {max(i, j) + 1} are on the same spot in the {label} image: "
                    "delete one or click a different feature")
        if kind in ("translation", "identity", "rotation90"):
            continue
        major, minor = _spread(points)
        if major <= 0:
            raise DegeneratePointsError(f"all {label} points are on one spot: a scale or rotation is undefined")
        if kind in _NEEDS_SPREAD and minor < collinear_ratio * major:
            raise DegeneratePointsError(
                f"the {label} points lie on a line: an {kind} transform is undetermined across it. "
                "Add a pair off that line, or choose a similarity (which needs only two points)")


@dataclass
class PairFit:
    """A fit to clicked pairs: ``fit`` (the module's :class:`ModelFit`,
    every pair an inlier), ``residuals`` per pair, ``loo`` the leave-one-out
    residual per pair (``None`` when there are no spare pairs), all in the
    target's units; ``worst`` the index of the most suspicious pair."""

    fit: ModelFit
    residuals: np.ndarray
    loo: np.ndarray | None
    model: str

    @property
    def worst(self) -> int | None:
        score = self.loo if self.loo is not None else self.residuals
        if score is None or len(score) == 0 or not np.isfinite(score).any():
            return None
        return int(np.nanargmax(score))


def _ls_fit(model: str, source: np.ndarray, target: np.ndarray, degree: int,
            input_shape: tuple[int, int] | None) -> ModelFit:
    # A threshold no residual reaches makes the robust fit an ordinary least-squares fit
    # over every pair (RANSAC's first all-inlier trial ends the search).
    return fit_transform(model, source, target, residual_threshold=1e300, max_trials=1, degree=degree,
                         input_shape=input_shape)


def fit_point_pairs(source: Any, target: Any, model: str, *, degree: int = 2,
                    input_shape: tuple[int, int] | None = None, unit: str = "px") -> PairFit:
    """Fit ``model`` to clicked pairs by least squares over every pair and
    report each pair's residual and leave-one-out residual (see the module
    docstring). Raises :class:`DegeneratePointsError` for a set the model
    cannot be fitted to. ``input_shape`` is needed by ``rotation90``."""
    source = np.asarray(source, dtype=np.float64).reshape(-1, 2)
    target = np.asarray(target, dtype=np.float64).reshape(-1, 2)
    check_point_geometry(source, target, model, degree=degree)
    raw = _ls_fit(model, source, target, degree, input_shape)
    residuals = np.linalg.norm(raw.mapped_points - target, axis=1)
    fit = ModelFit(transform=raw.transform, residuals=ResidualStats.from_residuals(residuals, unit=unit),
                   inliers=np.ones(len(source), dtype=bool), mapped_points=raw.mapped_points,
                   estimator=raw.estimator, selection=raw.selection)
    loo = None
    if len(source) > minimum_pairs(model, degree):
        loo = np.full(len(source), np.nan)
        for k in range(len(source)):
            keep = np.arange(len(source)) != k
            try:
                check_point_geometry(source[keep], target[keep], model, degree=degree)
                other = _ls_fit(model, source[keep], target[keep], degree, input_shape)
            except (ValueError, np.linalg.LinAlgError):
                continue
            loo[k] = float(np.linalg.norm(other.transform.map_points(source[k:k + 1])[0] - target[k]))
    return PairFit(fit=fit, residuals=residuals, loo=loo, model=str(model))


def manual_correspondence(source: Any, target: Any, metadata: Mapping[str, Any] | None = None
                          ) -> CorrespondenceResult:
    """Clicked pairs as a correspondence result (strategy ``manual``)."""
    source = np.asarray(source, dtype=np.float64).reshape(-1, 2)
    target = np.asarray(target, dtype=np.float64).reshape(-1, 2)
    return CorrespondenceResult(
        source, target, metadata={"strategy": "manual", "paired": int(len(source)), **dict(metadata or {})})


def _not_localized(*_args: Any) -> CorrespondenceResult:
    raise ValueError("Manual point pairs are clicked, not localized: open the point picker "
                     "(Pick points…) and click the same features in both images")


def manual_strategy_spec() -> CorrespondenceStrategySpec:
    """The 'Manual point pairs' method: pairs come from clicks, not images."""
    return CorrespondenceStrategySpec(
        name="manual",
        label="Manual point pairs (click)",
        description=(
            "Click a feature in one image, then the same feature in the other, a few times. "
            "A backup for when automatic localization fails. Clicks snap to the nearest spot "
            "centre (optional); the fit is redone after every pair, with each pair's residual, "
            "so a bad click shows. Minimum pairs: translation 1; rigid/similarity 2; affine 3; "
            "polynomial 6/10/15."),
        handler=_not_localized,
    )
