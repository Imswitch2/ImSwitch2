"""The transform record: a model plus everything needed to trust and place it.

A bare matrix is not enough to act on. Three things travel with it:

* **Which spaces it connects** (``source_frame`` -> ``target_frame``). A
  transform is an edge between named frames, not a property of a detector. Step
  0 uses two frames and no composition beyond :meth:`SpatialTransform.then`,
  but the field is here from the start so the frame graph can land later
  without a file migration.
* **Residuals.** A transform with no stated error is one nobody can trust.
  Every calibration path in this module reports them.
* **The acquisition context it was measured under.** A pixel-space calibration
  is only valid for the ROI, binning and pixel size it was taken at; recording
  them is what lets a later apply detect the mismatch instead of silently
  producing an offset result.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np

from .base import TransformModel

__all__ = ["AcquisitionContext", "ResidualStats", "SpatialTransform"]


@dataclass(frozen=True)
class ResidualStats:
    """How well the fitted model actually reproduced its correspondences."""

    n_points: int
    n_inliers: int
    mean: float
    median: float
    max: float
    rms: float
    threshold: float | None = None
    unit: str = "px"

    @classmethod
    def from_residuals(
        cls,
        residuals: Any,
        *,
        inliers: Any = None,
        threshold: float | None = None,
        unit: str = "px",
    ) -> "ResidualStats":
        values = np.asarray(residuals, dtype=np.float64).reshape(-1)
        if values.size == 0:
            raise ValueError("cannot summarize an empty residual array")
        n_inliers = (
            int(values.size)
            if inliers is None
            else int(np.count_nonzero(np.asarray(inliers, dtype=bool)))
        )
        return cls(
            n_points=int(values.size),
            n_inliers=n_inliers,
            mean=float(np.mean(values)),
            median=float(np.median(values)),
            max=float(np.max(values)),
            rms=float(np.sqrt(np.mean(values**2))),
            threshold=None if threshold is None else float(threshold),
            unit=str(unit),
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "n_points": self.n_points,
            "n_inliers": self.n_inliers,
            "mean": self.mean,
            "median": self.median,
            "max": self.max,
            "rms": self.rms,
            "unit": self.unit,
        }
        if self.threshold is not None:
            data["threshold"] = self.threshold
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResidualStats":
        return cls(
            n_points=int(data["n_points"]),
            n_inliers=int(data.get("n_inliers", data["n_points"])),
            mean=float(data["mean"]),
            median=float(data["median"]),
            max=float(data["max"]),
            rms=float(data["rms"]),
            threshold=(
                None if data.get("threshold") is None else float(data["threshold"])
            ),
            unit=str(data.get("unit", "px")),
        )

    def summary(self) -> str:
        """One line, in the terms a person judging a calibration cares about."""
        text = (
            f"mean {self.mean:.3f} {self.unit}, median {self.median:.3f} "
            f"{self.unit}, max {self.max:.3f} {self.unit}, "
            f"RMS {self.rms:.3f} {self.unit}"
        )
        return f"{text} ({self.n_inliers}/{self.n_points} inliers)"


@dataclass(frozen=True)
class AcquisitionContext:
    """What the rig was doing when this calibration was measured.

    Every field is optional -- a file-loaded calibration may know none of them
    -- but each one that *is* known lets a later apply verify it still holds.
    """

    detector: str | None = None
    image_shape: tuple[int, ...] | None = None
    roi: tuple[int, int, int, int] | None = None
    binning: int | None = None
    pixel_size_um: tuple[float, ...] | None = None
    objective: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self.extra)
        if self.detector is not None:
            data["detector"] = self.detector
        if self.image_shape is not None:
            data["image_shape"] = list(self.image_shape)
        if self.roi is not None:
            data["roi"] = list(self.roi)
        if self.binning is not None:
            data["binning"] = self.binning
        if self.pixel_size_um is not None:
            data["pixel_size_um"] = list(self.pixel_size_um)
        if self.objective is not None:
            data["objective"] = self.objective
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AcquisitionContext":
        if not data:
            return cls()
        known = {
            "detector",
            "image_shape",
            "roi",
            "binning",
            "pixel_size_um",
            "objective",
        }
        return cls(
            detector=(
                None if data.get("detector") is None else str(data["detector"])
            ),
            image_shape=(
                None
                if data.get("image_shape") is None
                else tuple(int(v) for v in data["image_shape"])
            ),
            roi=(
                None
                if data.get("roi") is None
                else tuple(int(v) for v in data["roi"])  # type: ignore[misc]
            ),
            binning=(None if data.get("binning") is None else int(data["binning"])),
            pixel_size_um=(
                None
                if data.get("pixel_size_um") is None
                else tuple(float(v) for v in data["pixel_size_um"])
            ),
            objective=(
                None if data.get("objective") is None else str(data["objective"])
            ),
            extra={key: value for key, value in data.items() if key not in known},
        )


@dataclass(frozen=True)
class SpatialTransform:
    """A model, the frames it connects, and the evidence behind it."""

    model: TransformModel
    source_frame: str = "source"
    target_frame: str = "target"
    units: str = "px"
    source_context: AcquisitionContext = field(default_factory=AcquisitionContext)
    target_context: AcquisitionContext = field(default_factory=AcquisitionContext)
    residuals: ResidualStats | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict, compare=False)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    def __post_init__(self) -> None:
        if not isinstance(self.model, TransformModel):
            raise TypeError(
                f"model must be a TransformModel, got {type(self.model).__name__}"
            )
        if self.source_frame == self.target_frame:
            raise ValueError(
                f"source and target frame must differ, both are "
                f"{self.source_frame!r}"
            )

    @property
    def kind(self) -> str:
        return self.model.kind

    @property
    def ndim(self) -> int:
        return self.model.ndim

    def map_points(self, points: Any) -> np.ndarray:
        return self.model.map_points(points)

    def as_matrix(self) -> np.ndarray | None:
        """The homogeneous matrix, or ``None`` if this model is not affine."""
        return self.model.as_matrix()

    def inverse(self) -> "SpatialTransform":
        """The target -> source transform, with frames and contexts swapped."""
        return replace(
            self,
            model=self.model.inverse(),
            source_frame=self.target_frame,
            target_frame=self.source_frame,
            source_context=self.target_context,
            target_context=self.source_context,
            provenance={**dict(self.provenance), "inverted_from": self.source_frame},
        )

    def then(self, other: "SpatialTransform") -> "SpatialTransform":
        """Chain onto ``other``, giving ``self.source -> other.target``.

        All-affine chains collapse into a single matrix. That is not just an
        optimization: a composed transform that keeps its matrix can still be
        applied as a non-resampling display transform, and one that does not
        cannot.

        Residuals are deliberately dropped rather than combined. Errors do
        accumulate along a chain, but inventing a combined figure would assert
        more than is known; a composed transform reports no residual and says
        in its provenance how it was built.
        """
        if self.target_frame != other.source_frame:
            raise ValueError(
                f"cannot chain {self.source_frame!r}->{self.target_frame!r} onto "
                f"{other.source_frame!r}->{other.target_frame!r}: frames do not meet"
            )
        if self.ndim != other.ndim:
            raise ValueError(
                f"cannot chain a {self.ndim}-D transform onto a {other.ndim}-D one"
            )

        from .models.affine import AffineTransform
        from .models.composed import ComposedTransform

        first = self.model.as_matrix()
        second = other.model.as_matrix()
        if first is not None and second is not None:
            model: TransformModel = AffineTransform(second @ first)
        else:
            model = ComposedTransform([self.model, other.model])

        return SpatialTransform(
            model=model,
            source_frame=self.source_frame,
            target_frame=other.target_frame,
            units=self.units,
            source_context=self.source_context,
            target_context=other.target_context,
            residuals=None,
            provenance={
                "composed_path": [
                    self.source_frame,
                    self.target_frame,
                    other.target_frame,
                ],
                "composed_kinds": [self.kind, other.kind],
            },
        )

    def summary(self) -> str:
        """One line describing this transform for a status bar or a log."""
        text = (
            f"{self.source_frame} -> {self.target_frame} "
            f"[{self.kind}, {self.ndim}-D, {self.units}]"
        )
        if self.residuals is not None:
            text = f"{text}  {self.residuals.summary()}"
        return text
