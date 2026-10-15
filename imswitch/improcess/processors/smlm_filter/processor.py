"""Filter a localization table on one of its properties.

One property at a time, by design: the panel shows that property's
histogram and the band placed on it, and **Apply filter** produces the
filtered table, which the panel then follows -- so a second property is
filtered by applying again. The first application copies (a new result);
the panel's *Copy* box then turns itself off, and the following ones replace
that copy in place rather than leaving one result per step. Band-pass keeps
the rows inside the band, band-stop drops them.

The earlier contract (``min_photons``/``max_photons``, the two sigma bounds
applied to both lateral sigmas, the frame range) is ``params_version`` 1;
a recorded run that bounded one property migrates, one that bounded several
is refused with the way to say it now.
"""

from __future__ import annotations

from typing import Callable

from qtpy import QtWidgets

from imswitch.improcess.analysis.smlm_tables import FILTER_MODES, filter_by_property
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import LOCALIZATION_COLUMNS
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor

from .widget import PROPERTY_LABELS, SmlmFilterWidget

DEFAULT_PROPERTY = "photons"

#: ``params_version`` 1 keys, by the property each pair bounded.
_V1_BOUNDS: dict[str, tuple[str, str]] = {
    "photons": ("min_photons", "max_photons"),
    "frame": ("min_frame", "max_frame"),
}
_V1_SIGMA_KEYS = ("min_sigma_nm", "max_sigma_nm")


def _number_or_none(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _v1_bound(encoded: dict, key: str) -> float | None:
    """A version-1 bound: 0 (or below, or missing) meant "off"."""
    try:
        number = float(encoded.get(key) or 0.0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def migrate_v1_params(encoded: dict) -> dict:
    """The version-2 shape of a version-1 run, when one filter can say it.

    A run that bounded one property is one band on that property. One that
    bounded several, or the sigma range (which applied to both lateral
    sigmas at once), has no single-property equivalent and is refused.
    """
    if any(_v1_bound(encoded, key) is not None for key in _V1_SIGMA_KEYS):
        raise ValueError(
            "smlm-filter parameters of version 1 bounded both lateral sigmas at "
            "once; filter sigma_x_nm and then sigma_y_nm in two steps instead"
        )
    bands = {}
    for prop, (low_key, high_key) in _V1_BOUNDS.items():
        lower, upper = _v1_bound(encoded, low_key), _v1_bound(encoded, high_key)
        if lower is not None or upper is not None:
            bands[prop] = (lower, upper)
    if len(bands) > 1:
        raise ValueError(
            f"smlm-filter parameters of version 1 bounded {sorted(bands)} at once; "
            "run one filter per property instead"
        )
    if not bands:
        return {"property": DEFAULT_PROPERTY, "mode": "bandpass", "lower": None, "upper": None}
    ((prop, (lower, upper)),) = bands.items()
    return {"property": prop, "mode": "bandpass", "lower": lower, "upper": upper}


class SmlmFilterProcessor(Processor):
    """Keep the rows whose chosen property lies inside, or outside, a band."""

    name = "SMLM filter"
    id = "smlm-filter"
    category = "Localization"
    kinds = ("localization",)
    run_label = "Apply filter"
    params_version = 2

    @classmethod
    def default_params(cls) -> dict:
        return {
            "property": DEFAULT_PROPERTY,
            "mode": "bandpass",
            # None: that side of the band is open. The panel fills both from
            # the data's own range as soon as a table is shown.
            "lower": None,
            "upper": None,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "property", "select", DEFAULT_PROPERTY, label="Property",
                options=LOCALIZATION_COLUMNS,
                help="The localization property the band applies to.",
            ),
            ParamField(
                "mode", "select", "bandpass", label="Mode", options=FILTER_MODES,
                help="bandpass keeps the rows inside the band, bandstop drops them; "
                     "both edges are inclusive.",
            ),
            ParamField(
                "lower", "float", None, label="Lower", decimals=2, nullable=True,
                help="Lower edge of the band, in the property's own unit; "
                     "unset = open below.",
            ),
            ParamField(
                "upper", "float", None, label="Upper", decimals=2, nullable=True,
                help="Upper edge of the band, in the property's own unit; "
                     "unset = open above.",
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return SmlmFilterWidget(parent)

    def migrate_params(self, encoded: dict | None, from_version: int) -> dict:
        encoded = dict(encoded or {})
        if from_version >= 2:
            return encoded
        return migrate_v1_params(encoded)

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("SMLM filter requires a LocalizationResult input")

        prop = str(params.get("property", DEFAULT_PROPERTY))
        mode = str(params.get("mode", "bandpass"))
        lower = _number_or_none(params.get("lower"))
        upper = _number_or_none(params.get("upper"))
        filtered, mask = filter_by_property(
            result.locs, prop, lower=lower, upper=upper, mode=mode
        )
        kept, total = int(mask.sum()), int(len(mask))
        label = PROPERTY_LABELS.get(prop, prop)
        return LocalizationResult(
            name=f"{result.name} ({label} filtered {kept}/{total})",
            locs=filtered,
            pixel_size_nm=result.pixel_size_nm,
            z_step_nm=result.z_step_nm,
            dims=result.dims,
            source_name=result.source_name,
            source_shape=result.source_shape,
            preview_pixel_size_nm=result.preview_pixel_size_requested_nm,
            metadata={
                **result.metadata,
                "filter_params": dict(params),
                "filter_property": prop,
                "filter_kept": kept,
                "filter_total": total,
            },
        )


__all__ = ["SmlmFilterProcessor", "migrate_v1_params"]
