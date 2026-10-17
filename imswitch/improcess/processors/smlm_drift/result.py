"""Drift-corrected localization result carrying the estimated drift trace."""

from __future__ import annotations

import numpy as np

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries


class DriftCorrectedLocalizationResult(LocalizationResult):
    """A LocalizationResult plus the per-frame drift that was subtracted."""

    def __init__(
        self,
        name,
        locs,
        *,
        drift_frames: np.ndarray,
        drift_x_nm: np.ndarray,
        drift_y_nm: np.ndarray,
        drift_z_nm: np.ndarray | None = None,
        **kwargs,
    ):
        self.drift_frames = np.asarray(drift_frames)
        self.drift_x_nm = np.asarray(drift_x_nm, dtype=np.float64)
        self.drift_y_nm = np.asarray(drift_y_nm, dtype=np.float64)
        #: Axial drift, only from a correction that estimated one (COMET on
        #: a 3D table); the lateral-only correction leaves it None.
        self.drift_z_nm = (
            None if drift_z_nm is None else np.asarray(drift_z_nm, dtype=np.float64)
        )
        super().__init__(name, locs, **kwargs)

    def plot_payloads(self) -> list[PlotPayload]:
        drift_payload = PlotPayload(
            title="Estimated drift",
            x_label="Frame",
            y_label="Drift (nm)",
            series=[
                PlotSeries(
                    name="X drift",
                    x=self.drift_frames,
                    y=self.drift_x_nm,
                    kind="line",
                ),
                PlotSeries(
                    name="Y drift",
                    x=self.drift_frames,
                    y=self.drift_y_nm,
                    kind="line",
                ),
                *(
                    [
                        PlotSeries(
                            name="Z drift",
                            x=self.drift_frames,
                            y=self.drift_z_nm,
                            kind="line",
                        )
                    ]
                    if self.drift_z_nm is not None
                    else []
                ),
            ],
        )
        return [drift_payload, *super().plot_payloads()]


__all__ = ["DriftCorrectedLocalizationResult"]
