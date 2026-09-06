"""The legacy MoNaLISA reconstruction as a Reconstructor.

``MoNaLISAController.runLegacyReconstruct`` loaded the data, corrected
bleaching, extracted coefficients and built the result itself, reading every
setting from widgets. That is the one reconstruction path in ImProcess that
no ``process()`` call covered, so nothing could record its provenance and
nothing could run it without the GUI. This adapter is the same algorithm
behind the ordinary contract: ``process(data_obj, params)`` with every
setting in ``params``, and ``consolidate()`` for the multi-dataset case. The
controller now builds ``params`` from its widgets and delegates.
"""

from __future__ import annotations

import copy


import numpy as np

from imswitch.improcess.reconstructors.base import Reconstructor

from .result import MonalisaProcessingResult
from .scan_params import DEFAULT_LABELS, AxisLabels, default_scan_params

BG_MODELLINGS = ("Constant", "Gaussian", "No background")
DEVICES = ("CPU", "GPU")


def default_axis_label_map(labels: AxisLabels = DEFAULT_LABELS) -> dict:
    return {
        "r_l_text": labels.r_l,
        "u_d_text": labels.u_d,
        "b_f_text": labels.b_f,
        "timepoints_text": labels.timepoints,
        "p_text": labels.p,
        "n_text": labels.n,
    }


def bleaching_correction(data: np.ndarray) -> np.ndarray:
    """Scale every frame so its total energy matches the first frame's."""
    corrected = np.array(data, copy=True)
    energy = np.sum(data, axis=(1, 2))
    for index in range(data.shape[0]):
        # Power-1 energy normalization (was **4, which over-corrected).
        corrected[index, :, :] = data[index, :, :] * (energy[0] / energy[index])
    return corrected


def extraction_sigmas(psf_fwhm_nm, bg_modelling: str, bg_gaussian_size_nm: float,
                      pixel_size_nm: float) -> np.ndarray:
    """PSF sigmas in pixels, with the background model encoded the way
    ``SignalExtractor`` expects (9999 = constant background, 0 = none)."""
    fwhm = np.atleast_1d(np.asarray(psf_fwhm_nm, dtype=float))
    if bg_modelling == "Constant":
        fwhm = np.append(fwhm, 9999)
    elif bg_modelling == "No background":
        fwhm = np.append(fwhm, 0)
    elif bg_modelling == "Gaussian":
        fwhm = np.append(fwhm, float(bg_gaussian_size_nm))
    else:
        raise ValueError(
            f'Invalid BG modelling "{bg_modelling}" specified; must be one of {BG_MODELLINGS}'
        )
    return np.divide(fwhm, 2.355 * float(pixel_size_nm))


class LegacyMonalisaReconstructor(Reconstructor):
    """Classic MoNaLISA (signal extraction on a detected pattern)."""

    name = "MoNaLISA (classic)"
    id = "monalisa-legacy"
    file_extensions = ["hdf5", "tiff", "zarr"]
    supports_consolidation = True
    params_version = 1

    def __init__(self):
        # SignalExtractor may need a CUDA DLL; construct on first use.
        self._signal_extractor = None

    def make_param_widget(self, parent):
        # The classic path is driven from the MoNaLISA parameter widget the
        # main view owns; this adapter has no widget of its own.
        return None

    def make_metadata_dialog(self, parent):
        return None

    @classmethod
    def default_params(cls) -> dict:
        return {
            "bleaching_correction": False,
            "psf_fwhm_nm": [250.0],
            "bg_modelling": "Constant",
            "bg_gaussian_size_nm": 1000.0,
            "pixel_size_nm": 35.0,
            "device": "CPU",
            "pattern": None,
            "scan_params": default_scan_params(),
            "axis_label_map": default_axis_label_map(),
        }

    def prepare_params(self, data_obj, params: dict | None) -> dict:
        """Fill ``scan_params`` from the acquisition attributes when absent."""
        from .scan_params import apply_scan_attrs

        params = dict(params or {})
        if params.get("scan_params") is None:
            try:
                frames = int(data_obj.numFrames)
            except Exception:
                frames = None
            params["scan_params"] = apply_scan_attrs(
                default_scan_params(), getattr(data_obj, "attrs", None) or {}, DEFAULT_LABELS, frames
            )
        if params.get("axis_label_map") is None:
            params["axis_label_map"] = default_axis_label_map()
        return params

    def _extractor(self):
        if self._signal_extractor is None:
            from .signal_extractor import SignalExtractor

            self._signal_extractor = SignalExtractor()
        return self._signal_extractor

    def extract(self, data: np.ndarray, params: dict) -> np.ndarray:
        device = str(params.get("device", "CPU"))
        if device not in DEVICES:
            raise ValueError(f'Invalid device "{device}" specified; must be one of {DEVICES}')
        pattern = params.get("pattern")
        if pattern is None:
            raise ValueError("classic MoNaLISA needs the detected pattern in params['pattern']")
        sigmas = extraction_sigmas(
            params.get("psf_fwhm_nm", [250.0]),
            str(params.get("bg_modelling", "Constant")),
            float(params.get("bg_gaussian_size_nm", 1000.0)),
            float(params.get("pixel_size_nm", 35.0)),
        )
        return self._extractor().extractSignal(data, sigmas, pattern, device.lower())

    def process(self, data_obj, params: dict, context=None) -> MonalisaProcessingResult:
        scan_params = params.get("scan_params")
        if scan_params is None:
            raise ValueError("classic MoNaLISA requires scan_params in params")
        if np.prod(np.array(scan_params["steps"], dtype=int)) < int(data_obj.numFrames):
            raise ValueError("Too many frames in data for the scan parameters")

        preloaded = bool(getattr(data_obj, "dataLoaded", True))
        try:
            data_obj.checkAndLoadData()
            data = np.asarray(data_obj.data)
            if params.get("bleaching_correction", False):
                data = bleaching_correction(data)
            coeffs = self.extract(data, params)
        finally:
            if not preloaded and hasattr(data_obj, "checkAndUnloadData"):
                data_obj.checkAndUnloadData()

        axis_label_map = dict(params.get("axis_label_map") or default_axis_label_map())
        return MonalisaProcessingResult.from_coeffs(
            data_obj.name, np.stack([coeffs], axis=0), copy.deepcopy(scan_params), axis_label_map
        )

    def consolidate(self, results: list) -> MonalisaProcessingResult:
        if not results:
            raise ValueError("nothing to consolidate")
        coeffs = np.concatenate([np.asarray(r.coeffs) for r in results], axis=0)
        first = results[0]
        return MonalisaProcessingResult.from_coeffs(
            first.name, coeffs, copy.deepcopy(first.scan_params), dict(first.axis_label_map)
        )


__all__ = [
    "BG_MODELLINGS",
    "DEVICES",
    "LegacyMonalisaReconstructor",
    "bleaching_correction",
    "default_axis_label_map",
    "extraction_sigmas",
]


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
