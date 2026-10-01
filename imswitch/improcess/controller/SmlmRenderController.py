"""Connects the point-cloud render controls to the reconstruction viewer.

The panel emits intent and the reconstruction viewer's own controller applies
it, so this controller never reaches across to another widget — it only
translates between the two, and keeps the panel in step with whichever result
is selected.
"""

from __future__ import annotations

import numpy as np

from imswitch.improcess.model.result import result_kind
from .basecontrollers import ImProcessWidgetController


class SmlmRenderController(ImProcessWidgetController):
    """Drives :class:`~imswitch.improcess.view.SmlmRenderWidget`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._result = None

        self._widget.sigSettingsChanged.connect(self._onSettingsChanged)
        self._widget.sigAppearanceChanged.connect(self._onAppearanceChanged)
        self._commChannel.sigCurrentResultChanged.connect(self.setResult)

        # The panel is usually opened after a result is already selected, and
        # the signal above only reports *future* changes — seed from whatever
        # is selected now so the controls are live the moment it appears.
        try:
            selected = self._commChannel.getSelectedResults()
            self.setResult(selected[0][1] if selected else None)
        except Exception as exc:
            self._logger.debug("Could not seed render controls: %s", exc)

    def setResult(self, result) -> None:
        """Point the controls at ``result``, or disable them."""
        if result is None or result_kind(result) != "localization":
            self._result = None
            self._widget.setResultContext(available=False)
            return

        self._result = result
        locs = getattr(result, "locs", None)
        self._widget.setResultContext(
            available=True,
            has_z=getattr(result, "dims", "2D") == "3D",
            has_uncertainty=self._hasUncertainty(locs),
            name=str(getattr(result, "name", "result")),
            count=len(result),
        )

    @staticmethod
    def _hasUncertainty(locs) -> bool:
        """Whether variable-width mode has anything real to work from.

        Mirrors what the display declares to napari-storm: a width or a photon
        count that is actually populated, not merely a column that exists.
        """
        if locs is None or not len(locs):
            return False
        names = getattr(np.asarray(locs).dtype, "names", None) or ()
        for column in ("lp_x_nm", "sigma_x_nm", "photons"):
            if column in names and bool(np.any(locs[column] > 0)):
                return True
        return False

    def _onSettingsChanged(self, overrides, renderRange) -> None:
        if self._result is None:
            return
        self._commChannel.sigSmlmRenderSettingsChanged.emit(
            self._result, dict(overrides), dict(renderRange)
        )

    def _onAppearanceChanged(self, appearance) -> None:
        if self._result is None:
            return
        self._commChannel.sigSmlmRenderAppearanceChanged.emit(
            self._result, dict(appearance)
        )


__all__ = ["SmlmRenderController"]
