"""The ROI set a user controls: segmented, drawn, edited, saved, loaded.

A :class:`RoiSet` is a label image on the detector's frame as delivered
(rows, columns of the camera image after its ROI and binning -- the frame
``roi-dff`` measures), plus what each ROI is: its id, where it came from
(``segment`` or ``drawn``), and the vertices it was drawn with. Ids are
stable: deleting a ROI never renumbers the others, a new ROI (drawn, or a
re-segmentation) gets an id that was never used in this set, so the log, the
event files and the summary can name a ROI by its id for the whole run.

The set records the camera grid it was made on
(:class:`~imswitch.imcommon.model.smartpipeline.FrameIdentity`), the
detector, the region (tile) and the stage position, so a saved set is only
used on the grid it belongs to and a tiled run can keep one per region.

Shapes drawn on the live view are in *displayed* pixels: the live view may
rotate or flip the camera image (``displayRotation``/``displayFlipX``/
``displayFlipY``). :func:`display_polygon_to_detector` maps them back to the
camera's (row, column) exactly, through the same transform the view applies
(:mod:`imswitch.imcontrol.controller.display_transform`); a pixel belongs to
a drawn ROI when its centre lies inside the polygon.

Saved as a label TIFF (``.tif``: what ``roi-dff``'s ``mask_source: file``
reads) with a JSON sidecar (``.json``: ids, sources, shapes, grid, region,
stage position).
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

#: Points on a drawn ellipse's outline when it is turned into a polygon.
ELLIPSE_POINTS = 64
SEGMENTATION_KEYS = ("threshold_method", "threshold_value", "min_area", "smooth_sigma", "morphology_radius",
                     "fill_holes", "clear_border")


@dataclass
class RoiSet:
    labels: np.ndarray
    info: dict = field(default_factory=dict)        # id -> {"source": ..., "vertices": [...]}
    next_id: int = 1
    detector: str = ""
    identity: dict | None = None                     # FrameIdentity.to_dict() of the grid it was made on
    region: int = 0
    stage_position_um: dict = field(default_factory=dict)
    segmentation: dict = field(default_factory=dict)  # the parameters of its last segmentation
    created: float = field(default_factory=time.time)
    #: Bumped on every change: what ``roi-dff``'s generation follows.
    version: int = 0

    def __post_init__(self):
        self.labels = np.asarray(self.labels).astype(np.int32, copy=True)
        if self.labels.ndim != 2:
            raise ValueError(f"a ROI set is a 2D label image, not shape {self.labels.shape}")
        present = set(int(v) for v in np.unique(self.labels) if v > 0)
        self.info = {int(k): dict(v) for k, v in (self.info or {}).items() if int(k) in present}
        for label in present - set(self.info):
            self.info[label] = {"source": "input"}
        self.next_id = max(int(self.next_id), max(present, default=0) + 1)

    @classmethod
    def empty(cls, shape, **kwargs) -> "RoiSet":
        return cls(np.zeros(tuple(shape), dtype=np.int32), **kwargs)

    # -- what it holds -------------------------------------------------------------

    @property
    def shape(self) -> tuple:
        return tuple(self.labels.shape)

    def ids(self) -> list[int]:
        return sorted(self.info)

    def __len__(self) -> int:
        return len(self.info)

    def table(self) -> list[dict]:
        """One row per ROI: id, source, area (px), centroid (row, col) and
        box (row0, col0, row1, col1) in pixel-centre coordinates."""
        rows = []
        flat = self.labels.reshape(-1)
        nz = np.flatnonzero(flat)
        if nz.size == 0:
            return rows
        values = flat[nz]
        order = np.argsort(values, kind="stable")
        nz, values = nz[order], values[order]
        ids, starts, counts = np.unique(values, return_index=True, return_counts=True)
        r_all, c_all = nz // self.labels.shape[1], nz % self.labels.shape[1]
        for label, start, count in zip(ids, starts, counts):
            r, c = r_all[start:start + count], c_all[start:start + count]
            rows.append({"id": int(label), "source": self.info.get(int(label), {}).get("source", "?"),
                         "area": int(count), "centroid": (float(r.mean()), float(c.mean())),
                         "bbox": (float(r.min()) - 0.5, float(c.min()) - 0.5, float(r.max()) + 0.5,
                                  float(c.max()) + 0.5)})
        return rows

    def outline(self) -> np.ndarray:
        """The label image reduced to each ROI's one-pixel border (for a
        cheap display: the inside stays visible)."""
        labels = self.labels
        inner = labels.copy()
        edge = np.zeros_like(labels, dtype=bool)
        edge[:-1, :] |= labels[:-1, :] != labels[1:, :]
        edge[1:, :] |= labels[1:, :] != labels[:-1, :]
        edge[:, :-1] |= labels[:, :-1] != labels[:, 1:]
        edge[:, 1:] |= labels[:, 1:] != labels[:, :-1]
        edge[0, :] = edge[-1, :] = True
        edge[:, 0] = edge[:, -1] = True
        inner[~edge] = 0
        return inner

    # -- changes -------------------------------------------------------------------

    def _changed(self) -> None:
        self.version += 1

    def replace_segmented(self, labels: np.ndarray, parameters: dict | None = None) -> list[int]:
        """Put a new segmentation in place of the segmented ROIs; drawn ROIs
        are kept (where they overlap, the drawn ROI keeps its pixels). The
        new ROIs get new ids. Returns them."""
        labels = np.asarray(labels)
        if labels.shape != self.labels.shape:
            raise ValueError(f"the segmentation has shape {labels.shape}, the ROI set {self.labels.shape}")
        drawn = {k for k, v in self.info.items() if v.get("source") == "drawn"}
        keep = np.isin(self.labels, list(drawn)) if drawn else np.zeros(self.labels.shape, dtype=bool)
        result = np.where(keep, self.labels, 0).astype(np.int32)
        info = {k: v for k, v in self.info.items() if k in drawn}
        new_ids = []
        for old in (int(v) for v in np.unique(labels) if v > 0):
            where = (labels == old) & ~keep
            if not where.any():
                continue
            new = self.next_id
            self.next_id += 1
            result[where] = new
            info[new] = {"source": "segment"}
            new_ids.append(new)
        self.labels, self.info = result, info
        if parameters is not None:
            self.segmentation = {k: parameters[k] for k in SEGMENTATION_KEYS if k in parameters}
        self._prune()
        self._changed()
        return [i for i in new_ids if i in self.info]

    def add_polygon(self, vertices_rc: Sequence[Sequence[float]], *, kind: str = "polygon") -> int | None:
        """A drawn ROI: the pixels whose centres are inside the polygon
        (detector (row, col)). The drawn ROI takes its pixels from any ROI
        under it; a ROI left with no pixel is gone. Returns its id, or None
        when the polygon holds no pixel centre."""
        mask = polygon_mask(vertices_rc, self.labels.shape)
        if not mask.any():
            return None
        new = self.next_id
        self.next_id += 1
        self.labels[mask] = new
        self.info[new] = {"source": "drawn", "kind": kind,
                          "vertices": [[round(float(r), 3), round(float(c), 3)] for r, c in vertices_rc]}
        self._prune()
        self._changed()
        return new

    def remove(self, ids) -> list[int]:
        """Delete these ROIs (the others keep their ids). Returns those removed."""
        ids = [int(i) for i in ids if int(i) in self.info]
        if ids:
            self.labels[np.isin(self.labels, ids)] = 0
            for label in ids:
                self.info.pop(label, None)
            self._changed()
        return ids

    def clear(self) -> None:
        if self.info:
            self.labels[:] = 0
            self.info = {}
            self._changed()

    def label_at(self, row: float, col: float) -> int:
        r, c = int(round(row)), int(round(col))
        if 0 <= r < self.labels.shape[0] and 0 <= c < self.labels.shape[1]:
            return int(self.labels[r, c])
        return 0

    def _prune(self) -> None:
        present = set(int(v) for v in np.unique(self.labels) if v > 0)
        self.info = {k: v for k, v in self.info.items() if k in present}

    # -- the grid ------------------------------------------------------------------

    def grid_problems(self, identity: dict | None) -> list[str]:
        """Why this set does not fit a camera delivering ``identity`` (a
        FrameIdentity dict), with what to do; [] when it fits."""
        if identity is None or self.identity is None:
            shape = None if identity is None else tuple(identity.get("shape") or ())
            if shape and tuple(shape) != self.shape:
                return [f"the ROIs were made on a {self.shape[0]}x{self.shape[1]} frame, the camera now delivers "
                        f"{shape[0]}x{shape[1]}: segment or draw them again, or load a set made on this grid"]
            return []
        problems = []
        for key, what in (("shape", "frame size"), ("binning", "binning"), ("offset", "camera ROI offset")):
            mine, theirs = list(self.identity.get(key) or []), list(identity.get(key) or [])
            if mine != theirs:
                problems.append(f"{what} {mine} when the ROIs were made, {theirs} now")
        if problems:
            return ["the ROIs do not fit the camera image (" + "; ".join(problems) + "): set the camera ROI and "
                    "binning back, or segment / draw the ROIs again"]
        return []

    # -- files ---------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"kind": "imswitch-roi-set", "version": 1, "detector": self.detector, "identity": self.identity,
                "region": self.region, "stage_position_um": self.stage_position_um,
                "segmentation": self.segmentation, "created": self.created, "next_id": self.next_id,
                "rois": {str(k): v for k, v in sorted(self.info.items())},
                "table": [{**row, "centroid": list(row["centroid"]), "bbox": list(row["bbox"])}
                          for row in self.table()]}

    def save(self, path) -> str:
        """``<path>.tif`` (labels) and ``<path>.json`` (the rest); ``path``
        may name either or neither extension. Returns the TIFF's path."""
        import tifffile

        base = _base(path)
        os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)
        tifffile.imwrite(base + ".tif", self.labels.astype(np.int32))
        with open(base + ".json", "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=1, default=_plain)
        return base + ".tif"

    @classmethod
    def load(cls, path) -> "RoiSet":
        """A set saved by :meth:`save` (``.tif`` + ``.json``), or any label
        image (``.tif``/``.npy``) -- its labels become the ids."""
        base = _base(path)
        image_path = next((p for p in (str(path), base + ".tif", base + ".tiff", base + ".npy")
                           if os.path.isfile(p) and not p.endswith(".json")), None)
        if image_path is None:
            raise FileNotFoundError(f"no label image for {path!r} (.tif or .npy)")
        if image_path.endswith(".npy"):
            labels = np.load(image_path)
        else:
            import tifffile

            labels = tifffile.imread(image_path)
        meta = {}
        if os.path.isfile(base + ".json"):
            with open(base + ".json", encoding="utf-8") as handle:
                meta = json.load(handle)
        return cls(labels, info={int(k): v for k, v in (meta.get("rois") or {}).items()},
                   next_id=int(meta.get("next_id", 1)), detector=str(meta.get("detector", "")),
                   identity=meta.get("identity"), region=int(meta.get("region", 0)),
                   stage_position_um=dict(meta.get("stage_position_um") or {}),
                   segmentation=dict(meta.get("segmentation") or {}),
                   created=float(meta.get("created", time.time())))


def _base(path) -> str:
    path = str(path)
    for extension in (".tif", ".tiff", ".json", ".npy"):
        if path.lower().endswith(extension):
            return path[: -len(extension)]
    return path


def _plain(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


# -- segmentation -------------------------------------------------------------------

def segment(image: np.ndarray, parameters: dict) -> np.ndarray:
    """Labels of ``image`` with ``roi-dff``'s segmentation parameters (the
    same code its ``mask_source: segment`` runs)."""
    from imswitch.imcommon.algorithms.segmentation import segment_image

    method = str(parameters.get("threshold_method", "otsu"))
    analysis = segment_image(
        np.asarray(image, dtype=np.float64), threshold_method=method,
        threshold_value=float(parameters.get("threshold_value", 0.0)) if method == "manual" else None,
        min_area=int(parameters.get("min_area", 20)), smooth_sigma=float(parameters.get("smooth_sigma", 1.0)),
        morphology_radius=int(parameters.get("morphology_radius", 0)),
        fill_holes=bool(parameters.get("fill_holes", True)), clear_border=bool(parameters.get("clear_border", False)),
    )
    return np.asarray(analysis.labels)


# -- shapes on the live view ----------------------------------------------------------

def shape_polygon(kind: str, vertices) -> np.ndarray:
    """A napari shape's outline as a polygon (rows of (row, col) in the
    shape's coordinates): a rectangle's or polygon's own vertices; an
    ellipse from the four corners of its (possibly rotated) box."""
    vertices = np.asarray(vertices, dtype=np.float64)[:, -2:]
    if kind == "ellipse":
        if vertices.shape[0] != 4:
            raise ValueError("an ellipse is given by the 4 corners of its box")
        centre = vertices.mean(axis=0)
        u = (vertices[1] - vertices[0]) / 2.0
        v = (vertices[3] - vertices[0]) / 2.0
        t = np.linspace(0.0, 2.0 * math.pi, ELLIPSE_POINTS, endpoint=False)
        return centre + np.outer(np.cos(t), u) + np.outer(np.sin(t), v)
    if kind in ("rectangle", "polygon"):
        return vertices
    raise ValueError(f"a ROI is drawn as a rectangle, an ellipse or a polygon, not a {kind}")


def display_polygon_to_detector(polygon_display, raw_shape, transform) -> np.ndarray:
    """Vertices in displayed pixels -> the camera's (row, col): through the
    inverse of the live view's display transform (rotation, flips)."""
    from imswitch.imcontrol.controller.display_transform import display_to_raw_point

    return np.array([display_to_raw_point(r, c, raw_shape, transform) for r, c in np.asarray(polygon_display)],
                    dtype=np.float64).reshape(-1, 2)


def detector_points_to_display(points_rc, raw_shape, transform) -> np.ndarray:
    from imswitch.imcontrol.controller.display_transform import raw_to_display_point

    return np.array([raw_to_display_point(r, c, raw_shape, transform) for r, c in np.asarray(points_rc)],
                    dtype=np.float64).reshape(-1, 2)


def polygon_mask(vertices_rc, shape) -> np.ndarray:
    """The pixels of ``shape`` whose centres lie inside the polygon."""
    from skimage.draw import polygon as fill

    vertices = np.asarray(vertices_rc, dtype=np.float64).reshape(-1, 2)
    mask = np.zeros(tuple(shape), dtype=bool)
    if vertices.shape[0] < 3:
        return mask
    rows, cols = fill(vertices[:, 0], vertices[:, 1], shape=tuple(shape))
    mask[rows, cols] = True
    return mask


#: The mask sources of a roi-dff step the panel's ROI set stands in for:
#: segmenting at arm (which the set replaces when the user wants it) and a
#: label file. ``input`` reads the labels another step makes: no ROI set.
ROI_SET_MASK_SOURCES = ("segment", "file")


def roi_set_steps(workflow) -> list:
    """The steps of ``workflow`` that measure ROIs the panel's ROI set can
    give them (roi-dff, mask from ``segment`` or ``file``), in file order.
    A pipeline without one needs no ROIs and no segmentation: the panel
    hides its ROIs box and arming asks for neither."""
    from imswitch.imcommon.model.workflow.steps import Process

    return [step for step in getattr(workflow, "steps", ())
            if isinstance(step, Process) and step.processor == "roi-dff"
            and str((step.params or {}).get("mask_source", "segment")) in ROI_SET_MASK_SOURCES]


__all__ = ["ELLIPSE_POINTS", "ROI_SET_MASK_SOURCES", "RoiSet", "detector_points_to_display",
           "display_polygon_to_detector", "polygon_mask", "roi_set_steps", "segment", "shape_polygon"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
