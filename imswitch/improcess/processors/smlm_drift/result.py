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
        **kwargs,
    ):
        self.drift_frames = np.asarray(drift_frames)
        self.drift_x_nm = np.asarray(drift_x_nm, dtype=np.float64)
        self.drift_y_nm = np.asarray(drift_y_nm, dtype=np.float64)
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
            ],
        )
        return [drift_payload, *super().plot_payloads()]


__all__ = ["DriftCorrectedLocalizationResult"]
