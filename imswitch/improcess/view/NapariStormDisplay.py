"""Retained-mode GPU point-cloud rendering for localization results.

An optional display backend for
:class:`~imswitch.improcess.model.localization_result.LocalizationResult`,
drawing each molecule as a summed Gaussian through napari-storm instead of the
low-resolution histogram preview a result carries by default.

**Why this sits outside the normal display path.** ``setDisplayLayers`` is
stateless: every result-list click clears the managed layers and rebuilds them
from a spec carrying a plain array. napari-storm is the opposite — datasets are
opened once, updated in place, and closed explicitly, and dataset ids must
never be reused. Driving it from the stateless path would open and close on
every click, which is the churn its architecture exists to prevent, and its
tables cannot survive the ``np.asarray()`` a display spec applies. So
napari-storm layers live here, in a parallel retained channel keyed by result
identity, and only explicit removal closes a dataset.

Everything is best-effort: an absent package, a GL session without instancing,
or a dataset napari-storm refuses all degrade to ``False`` from :meth:`show`,
and the caller falls back to the preview histogram.
"""

from __future__ import annotations

import itertools
import weakref
from dataclasses import dataclass
from typing import Any

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.localization_schema import napari_storm_table_kwargs

#: Gaussian width used when a table carries no usable uncertainty at all, in
#: nm. Only reached by data with neither precision, PSF width nor photons.
FALLBACK_SIGMA_NM = 20.0


@dataclass
class _Dataset:
    """One open napari-storm dataset, and the result it was built from."""

    dataset_id: int
    ref: "weakref.ref"
    table: Any
    name: str


class NapariStormDisplay:
    """Owns the napari-storm renderer and the result -> dataset id map."""

    def __init__(self, viewer, logger=None):
        self._viewer = viewer
        self._logger = logger if logger is not None else initLogger(self)
        self._renderer = None
        self._core = None
        self._select_renderer = None
        self._budget = None
        self._import_failed = False
        # Ids are handed out once and never reissued: a recycled id is how a
        # stale handle gets mistaken for a live one.
        self._ids = itertools.count(1)
        self._datasets: dict[int, _Dataset] = {}
        # The *map key* (a result's id), not a dataset id: the two are
        # different integers and mixing them looks up nothing.
        self._visible: int | None = None

    # -- availability -----------------------------------------------------

    @property
    def importable(self) -> bool:
        """Whether napari-storm can be imported at all."""
        return self._load() is not None

    def _load(self):
        """Import napari-storm lazily; ``None`` when it is not installed."""
        if self._core is not None or self._import_failed:
            return self._core
        try:
            from napari_storm import core, memory_budget
            from napari_storm.napari_particles.selection import select_renderer
        except Exception as exc:  # noqa: BLE001 - optional dependency
            self._import_failed = True
            self._logger.debug("napari-storm not available: %s", exc)
            return None
        self._core = core
        self._budget = memory_budget
        self._select_renderer = select_renderer
        return self._core

    def _ensure_renderer(self):
        """Build the renderer on first use, when a GL session exists."""
        if self._renderer is not None:
            return self._renderer
        if self._load() is None:
            return None
        try:
            self._renderer = self._select_renderer(self._viewer)
        except Exception as exc:  # noqa: BLE001 - never take the viewer down
            self._logger.warning("Could not create a napari-storm renderer: %s", exc)
            self._import_failed = True
            return None
        return self._renderer

    # -- the display contract --------------------------------------------

    def show(self, result) -> bool:
        """Render ``result``, opening it if this is the first time.

        Returns False when napari-storm cannot draw it, so the caller can fall
        back to the result's own preview rather than showing an empty canvas.
        """
        renderer = self._ensure_renderer()
        if renderer is None or result is None:
            return False

        entry = self._entry(result)
        try:
            if entry is None:
                entry = self._open(result)
                # Frame the new cloud once, on open only: doing it on every
                # reselection would throw away a zoom the user set.
                self._focus(entry)
            else:
                self._reveal(entry)
        except Exception as exc:  # noqa: BLE001 - degrade to the preview
            self._logger.warning(
                "napari-storm could not render %r: %s",
                getattr(result, "name", "result"), exc,
            )
            return False

        self._visible = id(result)
        self._apply_ndisplay(result)
        return True

    def update(self, result) -> bool:
        """Replan an open dataset in place, keeping its GPU resources.

        This is the acquisition-loop path; ``open`` would recreate the layer
        on every growth step, which is the leak the split exists to avoid.
        """
        renderer = self._ensure_renderer()
        entry = self._entry(result)
        if renderer is None or entry is None:
            return self.show(result)
        try:
            entry.table.set_records(result.locs, copy=False)
            self._limit_to_budget(entry.table)
            renderer.update(entry.dataset_id, self._plan(entry.table, result, entry.name))
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("napari-storm update failed for %r: %s", entry.name, exc)
            return False
        return True

    def hide(self) -> None:
        """Hide whatever is currently drawn, without closing it."""
        if self._visible is None:
            return
        self._set_visible(self._visible, False)
        self._visible = None

    def retain_only(self, results) -> None:
        """Close datasets whose result is no longer loaded.

        The results list announces that *something* was removed rather than
        which, so the live set is reconciled against rather than tracked.
        """
        live = {id(result) for result in results or ()}
        for key in [key for key in self._datasets if key not in live]:
            self._close(key)

    def close_all(self) -> None:
        for key in list(self._datasets):
            self._close(key)
        if self._renderer is not None:
            try:
                self._renderer.close_all()
            except Exception as exc:  # noqa: BLE001
                self._logger.debug("close_all: %s", exc)
        self._visible = None

    # -- internals --------------------------------------------------------

    def _entry(self, result) -> _Dataset | None:
        """The open dataset for ``result``, if it is still the same object.

        Verified through a weak reference rather than trusted: ``id()`` is
        reused after a garbage collection, and a recycled key would hand back
        another result's layer.
        """
        entry = self._datasets.get(id(result))
        if entry is None:
            return None
        if entry.ref() is not result:
            self._close(id(result))
            return None
        return entry

    def _open(self, result) -> _Dataset:
        core = self._core
        table = core.LocalizationTable(
            result.locs, copy=False, **napari_storm_table_kwargs(result.locs)
        )
        self._limit_to_budget(table)

        dataset_id = next(self._ids)
        name = str(getattr(result, "name", None) or f"localizations {dataset_id}")
        self._renderer.open(dataset_id, self._plan(table, result, name))

        entry = _Dataset(dataset_id, weakref.ref(result), table, name)
        self._datasets[id(result)] = entry
        # A freshly opened dataset is the only visible one.
        for key, other in self._datasets.items():
            if other.dataset_id != dataset_id:
                self._set_visible(key, False)
        return entry

    def _reveal(self, entry: _Dataset) -> None:
        for key, other in self._datasets.items():
            self._set_visible(key, other.dataset_id == entry.dataset_id)

    def _plan(self, table, result, name):
        core = self._core
        traits = self._traits(result)
        settings = self._settings(traits)
        return core.RenderPlanner(on_repaired=self._on_repaired).plan(
            table, settings, traits, name=name,
        )

    def _traits(self, result):
        """Declare what this table actually recorded, not what it could have.

        ``sigma_present`` means *every declared width axis is real*. Declaring
        an axial width that was never fitted is refused by napari-storm rather
        than degraded, and our own 2D localizer leaves ``sigma_z`` zero-filled.
        """
        locs = result.locs
        three_d = getattr(result, "dims", "2D") == "3D"
        widths = napari_storm_table_kwargs(locs)["sigma_columns"]
        axes = ("x", "y", "z") if three_d else ("x", "y")
        sigma_present = all(
            self._has_positive(locs, widths[axis]) for axis in axes
        )
        return self._core.DatasetTraits(
            zdim_present=three_d,
            sigma_present=sigma_present,
            photon_count_present=self._has_positive(locs, "photons"),
            pixel_size_nm=float(getattr(result, "pixel_size_nm", 1.0) or 1.0),
        )

    def _settings(self, traits):
        """Variable-width when there is a real uncertainty, fixed otherwise."""
        core = self._core
        if traits.uncertainty_defined:
            return core.GaussianSettings(mode=1)
        return core.GaussianSettings(
            mode=0,
            fixed_sigma_xy_nm=FALLBACK_SIGMA_NM,
            fixed_sigma_z_nm=FALLBACK_SIGMA_NM,
        )

    @staticmethod
    def _has_positive(locs, column) -> bool:
        names = getattr(np.asarray(locs).dtype, "names", None) or ()
        if column not in names or not len(locs):
            return False
        return bool(np.any(locs[column] > 0))

    def _limit_to_budget(self, table) -> None:
        """Thin the *display* set to what the GPU can afford.

        Only the renderer sees this; exports and counts read our own result,
        so a display budget can never reach a file we write.
        """
        try:
            table.limit_active_to(
                self._budget.max_localizations_for_budget(
                    self._budget.default_render_budget_mb()
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("Could not apply the render budget: %s", exc)

    def _on_repaired(self, column, repaired, total) -> None:
        self._logger.warning(
            "napari-storm substituted %d of %d unusable %s values", repaired, total, column
        )

    def _layer(self, entry: _Dataset):
        for layer in getattr(self._viewer, "layers", ()):
            if getattr(layer, "name", None) == entry.name:
                return layer
        return None

    def _focus(self, entry: _Dataset) -> None:
        """Frame the camera on this dataset alone.

        ``reset_view`` frames every layer, and ImProcess's protected image
        layer is a 1x1 blank pinned at the origin that napari counts even when
        it is hidden. Localizations sit at real sample coordinates, so the
        union runs from the origin out to the data and the cloud ends up a
        speck in the corner. Correcting the camera afterwards keeps napari's
        own canvas-size arithmetic instead of reimplementing it.

        Read off the layer's reported extent rather than our own columns, so
        it stays right whatever axis order the renderer draws in.
        """
        layer = self._layer(entry)
        if layer is None:
            return
        try:
            self._viewer.reset_view()
            union = np.asarray(self._viewer.layers.extent.world, dtype=float)
            target = np.asarray(layer.extent.world, dtype=float)
            # Compare only the two displayed axes; a flat z would give 0/0.
            union_span = (union[1] - union[0])[-2:]
            target_span = (target[1] - target[0])[-2:]
            ratios = [
                whole / part
                for whole, part in zip(union_span, target_span)
                if part > 0 and whole > 0
            ]
            self._viewer.camera.center = tuple(target.mean(axis=0))
            if ratios:
                self._viewer.camera.zoom = self._viewer.camera.zoom * min(ratios)
        except Exception as exc:  # noqa: BLE001 - framing is never fatal
            self._logger.debug("Could not frame %r: %s", entry.name, exc)

    def _apply_ndisplay(self, result) -> None:
        """3D data needs napari's 3D canvas; 2D is left alone.

        Only ever raised, never lowered: dropping back to 2D would fight a
        user who turned it on, and an image result restores its own view.
        """
        if getattr(result, "dims", "2D") != "3D":
            return
        try:
            if self._viewer.dims.ndisplay != 3:
                self._viewer.dims.ndisplay = 3
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("Could not switch to the 3D canvas: %s", exc)

    def _set_visible(self, key: int, visible: bool) -> None:
        entry = self._datasets.get(key)
        if entry is None or self._renderer is None:
            return
        try:
            self._renderer.set_appearance(
                entry.dataset_id, self._core.LayerAppearance(visible=visible)
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("Could not set visibility on %r: %s", entry.name, exc)

    def _close(self, key: int) -> None:
        entry = self._datasets.pop(key, None)
        if entry is None:
            return
        if self._visible == key:
            self._visible = None
        if self._renderer is None:
            return
        try:
            self._renderer.close(entry.dataset_id)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("Could not close %r: %s", entry.name, exc)


__all__ = ["NapariStormDisplay"]
