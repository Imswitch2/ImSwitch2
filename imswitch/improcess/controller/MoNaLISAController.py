import copy

import numpy as np

from imswitch.improcess.reconstructors.monalisa.legacy import (
    LegacyMonalisaReconstructor,
    bleaching_correction,
)
from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder
from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult
from imswitch.improcess.reconstructors.monalisa.scan_params import (
    AxisLabels,
    apply_scan_attrs,
    scan_params_for_source,
    positive_int_attr,
)
from .basecontrollers import ImProcessWidgetController


class MoNaLISAController(ImProcessWidgetController):
    """Manages MoNaLISA-specific pattern detection, scan-parameter housekeeping,
    signal extraction, and legacy reconstruction workflow. The coordinator
    retains general shared state; the MoNaLISA controller reaches it via
    self._main when needed.

    Extracted from ``ImProcessMainViewController`` following the pattern of
    ``FileIOController``, ``WidefieldStarssBatchController``, and
    ``ReconstructorManagerController``.
    """

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main = mainController

        # SignalExtractor is MoNaLISA-only and Windows-only (CUDA DLL). Defer
        # construction until the user actually triggers a MoNaLISA reconstruction;
        # otherwise the module fails to launch on macOS/Linux even when the user
        # only wants view-only / drag-and-drop.
        self._signalExtractor = None
        self._legacyAdapter = None
        self._patternFinder = PatternFinder()

        self._pattern = self._widget.getPatternParams()
        self._settingPatternParams = False
        self._scanParDict = {
            'dimensions': [self._widget.u_d_text, self._widget.r_l_text, self._widget.b_f_text,
                           self._widget.timepoints_text],
            'directions': [self._widget.p_text, self._widget.p_text, self._widget.p_text],
            'steps': ['35', '35', '1', '1'],
            'step_sizes': ['35', '35', '35', '1'],
            'n_linesteps': 1,
            'unidirectional': True
        }

    def findPattern(self):
        self._logger.debug('Find pattern clicked')
        if self._main._currentDataObj is None:
            return

        meanData = self._main._currentDataObj.getMeanData()
        if len(meanData) < 1:
            return

        self._logger.debug('Finding pattern')
        pattern = self._patternFinder.findPattern(meanData)
        self._logger.debug(f'Pattern found as: {self._pattern}')
        self.setPatternParams(pattern)
        self.updatePattern()

    def togglePattern(self, enabled):
        self._logger.debug('Toggling pattern')
        self._commChannel.sigPatternVisibilityChanged.emit(enabled)

    def updatePattern(self):
        if self._settingPatternParams:
            return

        self._logger.debug('Updating pattern')
        self._pattern = self._widget.getPatternParams()
        self._commChannel.sigPatternUpdated.emit(self._pattern)

    def setPatternParams(self, pattern):
        try:
            self._settingPatternParams = True
            self._widget.setPatternParams(*pattern)
        finally:
            self._settingPatternParams = False

    def updateScanParams(self, applyOnCurrentRecon=False):
        self._commChannel.sigScanParamsUpdated.emit(copy.deepcopy(self._scanParDict),
                                                    applyOnCurrentRecon)

    def scanParamsUpdated(self, scanParDict):
        self._scanParDict = scanParDict

    def showScanParamsDialog(self):
        self.updateScanParams()
        self._widget.showScanParamsDialog()

    def parseScanParamsFromAttrs(self, dataObj):
        """MoNaLISA-specific scan-params housekeeping. Only runs when the
        DataObj actually carries Imswitch acquisition metadata (HDF5/Zarr
        written by Imcontrol).  TIFF stacks and most external acquisitions
        have ``attrs is None``; bailing here is the right thing, and is
        what unblocks the pass-through auto-route in currentDataChanged.

        The parsing itself is the pure function
        :func:`~imswitch.improcess.reconstructors.monalisa.scan_params.apply_scan_attrs`,
        shared with the headless runner; this method only supplies the
        widget's axis-label strings and stores the answer."""
        attrs = dataObj.attrs if dataObj is not None else None
        if not attrs:
            return
        # The resolver already decided what this recording's axes are; the
        # dialog asks it first, through the same function the headless runs
        # use, so the dialog and the reconstruction cannot disagree about one
        # file. The attributes are the fallback for recordings without a
        # usable layout.
        self._scanParDict = scan_params_for_source(
            self._scanParDict, dataObj, self._axisLabels()
        )
        self.updateScanParams()

    def _axisLabels(self) -> AxisLabels:
        return AxisLabels(
            r_l=self._widget.r_l_text,
            u_d=self._widget.u_d_text,
            b_f=self._widget.b_f_text,
            timepoints=self._widget.timepoints_text,
            p=self._widget.p_text,
            n=self._widget.n_text,
        )

    @staticmethod
    def _positiveIntAttr(attrs, key):
        """Return a positive scalar integer metadata value, else ``None``."""
        return positive_int_attr(attrs, key)

    def legacyParams(self) -> dict:
        """Every setting the classic reconstruction reads, as one dict.

        This is what the widgets used to be read for inline; handing it to
        the adapter as ``params`` is what lets the run be recorded and, later,
        run again without the widgets.
        """
        return {
            'bleaching_correction': bool(self._widget.bleachBool.value()),
            'psf_fwhm_nm': [float(v) for v in np.atleast_1d(self._widget.getFwhmNm())],
            'bg_modelling': str(self._widget.getBgModelling()),
            'bg_gaussian_size_nm': float(self._widget.getBgGaussianSize()),
            'pixel_size_nm': float(self._widget.getPixelSizeNm()),
            'device': str(self._widget.getComputeDevice()),
            'pattern': self._pattern,
            'scan_params': copy.deepcopy(self._scanParDict),
            'axis_label_map': {
                'r_l_text': self._widget.r_l_text,
                'u_d_text': self._widget.u_d_text,
                'b_f_text': self._widget.b_f_text,
                'timepoints_text': self._widget.timepoints_text,
                'p_text': self._widget.p_text,
                'n_text': self._widget.n_text,
            },
        }

    def _legacyReconstructor(self) -> LegacyMonalisaReconstructor:
        if self._legacyAdapter is None:
            self._legacyAdapter = LegacyMonalisaReconstructor()
        return self._legacyAdapter

    def extractData(self, data):
        """Coefficients for ``data`` with the current widget settings."""
        return self._legacyReconstructor().extract(np.asarray(data), self.legacyParams())

    def runLegacyReconstruct(self, dataObjs, consolidate):
        """Classic MoNaLISA reconstruction through the adapter, so every
        result carries its provenance like any other reconstruction."""
        from imswitch.improcess.reconstructors.run import (
            run_consolidation,
            run_reconstruction,
        )

        adapter = self._legacyReconstructor()
        params = self.legacyParams()
        runs = []
        for dataObj in dataObjs:
            try:
                run = run_reconstruction(adapter, dataObj, params)
            except ValueError as exc:
                self._logger.error(str(exc))
                return
            if consolidate:
                runs.append(run)
            else:
                self._commChannel.sigResultProduced.emit(run.result, run.result.name)

        if consolidate and runs:
            result = run_consolidation(adapter, runs)
            self._commChannel.sigResultProduced.emit(result, f'{result.name}_multi')
            self._commChannel.sigExecutionFinished.emit(self._main.reconstructionController.getImage())

    def _buildMonalisaResult(self, name, coeffsList):
        """Assemble a MonalisaProcessingResult from one or more datasets' coeffs
        (kept for callers that extract coefficients themselves)."""
        coeffs = np.stack(coeffsList, axis=0)
        return MonalisaProcessingResult.from_coeffs(
            name, coeffs, copy.deepcopy(self._scanParDict), self.legacyParams()['axis_label_map']
        )

    def bleachingCorrection(self, data):
        return bleaching_correction(np.asarray(data))


# Copyright (C) 2020-2021 ImSwitch developers
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
