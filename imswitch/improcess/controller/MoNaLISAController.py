import copy

import numpy as np

from imswitch.improcess.reconstructors.monalisa.pattern_finder import PatternFinder
from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult
from imswitch.improcess.reconstructors.monalisa.signal_extractor import SignalExtractor
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
        self._patternFinder = PatternFinder()

        self._pattern = self._widget.getPatternParams()
        self._settingPatternParams = False
        self._scanParDict = {
            'dimensions': [self._widget.u_d_text, self._widget.r_l_text, self._widget.b_f_text,
                           self._widget.timepoints_text],
            'directions': [self._widget.p_text, self._widget.p_text, self._widget.p_text],
            'steps': ['35', '35', '1', '1'],
            'step_sizes': ['35', '35', '35', '1'],
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
        what unblocks the pass-through auto-route in currentDataChanged —
        otherwise the KeyError-only try/except blocks would let a TypeError
        escape and the auto-render path never ran."""
        attrs = dataObj.attrs if dataObj is not None else None
        if not attrs:
            return

        # The resolver already decided what this recording's axes are. Reading
        # the same attributes again here is what let the dialog disagree with
        # the reconstruction about the same file.
        if self._applyLayoutScanParams(dataObj):
            return

        dimensionMap = {
            b'X': self._widget.r_l_text,
            b'Y': self._widget.u_d_text,
            b'Z': self._widget.b_f_text
        }
        try:
            targetsAttr = attrs['ScanStage:target_device']
            for i in range(0, min(3, len(targetsAttr))):
                self._scanParDict['dimensions'][i] = dimensionMap[targetsAttr[i]]
        except (KeyError, TypeError):
            pass

        try:
            positiveDirectionAttr = attrs['ScanStage:positive_direction']
            for i in range(0, min(3, len(positiveDirectionAttr))):
                self._scanParDict['directions'][i] = (
                    self._widget.p_text if positiveDirectionAttr[i]
                    else self._widget.n_text
                )
        except (KeyError, TypeError):
            pass

        # There used to be a sqrt(numFrames) square guess here. It filled the
        # dialog with 25x25 for the motivating 648-frame 18x18x2 scan, which is
        # the same wrong shape BeadRec used to invent. A scan whose geometry
        # nothing records is left for the user to state.

        try:
            stepSizesAttr = attrs['ScanStage:axis_step_size']
        except (KeyError, TypeError):
            pass
        else:
            for i in range(0, min(4, len(stepSizesAttr))):
                self._scanParDict['step_sizes'][i] = str(stepSizesAttr[i] * 1000)  # convert um->nm

        self.updateScanParams()

    def _applyLayoutScanParams(self, dataObj) -> bool:
        """Fill the dialog from the resolved layout. True when it answered."""
        from imswitch.improcess.reconstructors.monalisa.scan_geometry import (
            scan_params_from_layout,
        )

        try:
            resolved = getattr(dataObj, 'acquisition_layout', None)
            values = scan_params_from_layout(
                resolved,
                {
                    'r_l_text': self._widget.r_l_text,
                    'u_d_text': self._widget.u_d_text,
                    'b_f_text': self._widget.b_f_text,
                    'timepoints_text': self._widget.timepoints_text,
                    'p_text': self._widget.p_text,
                    'n_text': self._widget.n_text,
                },
            )
        except Exception as error:
            # Pre-filling a dialog must never stop a file from opening.
            self._logger.debug(f'Could not read the acquisition layout: {error}')
            return False
        if values is None:
            return False
        self._scanParDict.update(values)
        self.updateScanParams()
        return True

    def extractData(self, data):
        fwhmNm = self._widget.getFwhmNm()
        bgModelling = self._widget.getBgModelling()
        if bgModelling == 'Constant':
            fwhmNm = np.append(fwhmNm, 9999)  # Code for constant bg
        elif bgModelling == 'No background':
            fwhmNm = np.append(fwhmNm, 0)  # Code for zero bg
        elif bgModelling == 'Gaussian':
            self._logger.debug('In Gaussian version')
            fwhmNm = np.append(fwhmNm, self._widget.getBgGaussianSize())
            self._logger.debug('Appended to sigmas')
        else:
            raise ValueError(f'Invalid BG modelling "{bgModelling}" specified; must be either'
                             f' "Constant", "Gaussian" or "No background".')

        sigmas = np.divide(fwhmNm, 2.355 * self._widget.getPixelSizeNm())

        device = self._widget.getComputeDevice()
        pattern = self._pattern
        if device == 'CPU' or device == 'GPU':
            if self._signalExtractor is None:
                self._signalExtractor = SignalExtractor()
            coeffs = self._signalExtractor.extractSignal(data, sigmas, pattern, device.lower())
        else:
            raise ValueError(f'Invalid device "{device}" specified; must be either "CPU" or "GPU"')

        return coeffs

    def runLegacyReconstruct(self, dataObjs, consolidate):
        """Legacy MoNaLISA reconstruction workflow. Runs signal extraction
        and emits MonalisaProcessingResult to the reconstruction viewer."""
        consolidatedCoeffs = []
        consolidatedName = None
        for index, dataObj in enumerate(dataObjs):
            preloaded = dataObj.dataLoaded
            try:
                dataObj.checkAndLoadData()

                if np.prod(np.array(self._scanParDict['steps'], dtype=int)) < dataObj.numFrames:
                    self._logger.error('Too many frames in data')
                    return

                data = dataObj.data
                if self._widget.bleachBool.value():
                    data = self.bleachingCorrection(data)

                coeffs = self.extractData(data)
            finally:
                if not preloaded:
                    dataObj.checkAndUnloadData()

            if consolidate:
                if index == 0:
                    consolidatedName = dataObj.name
                consolidatedCoeffs.append(coeffs)
            else:
                result = self._buildMonalisaResult(dataObj.name, [coeffs])
                self._commChannel.sigResultProduced.emit(result, result.name)

        if consolidate and consolidatedCoeffs:
            result = self._buildMonalisaResult(consolidatedName, consolidatedCoeffs)
            self._commChannel.sigResultProduced.emit(result, f'{result.name}_multi')
            self._commChannel.sigExecutionFinished.emit(self._main.reconstructionController.getImage())

    def _buildMonalisaResult(self, name, coeffsList):
        """Assemble a MonalisaProcessingResult from one or more datasets' coeffs.

        ``coeffsList`` holds per-dataset 4D ``(Base, frames, gridRows, gridCols)``
        arrays from ``extractData``; they are stacked along a new leading Dataset
        axis. The result retains the coefficients and the widget-text axis-label
        map so the viewer can re-reconstruct on scan-param edits and export
        coefficients.
        """
        coeffs = np.stack(coeffsList, axis=0)
        axis_label_map = {
            'r_l_text': self._widget.r_l_text,
            'u_d_text': self._widget.u_d_text,
            'b_f_text': self._widget.b_f_text,
            'timepoints_text': self._widget.timepoints_text,
            'p_text': self._widget.p_text,
            'n_text': self._widget.n_text,
        }
        return MonalisaProcessingResult.from_coeffs(
            name, coeffs, copy.deepcopy(self._scanParDict), axis_label_map
        )

    def bleachingCorrection(self, data):
        correctedData = data.copy()
        energy = np.sum(data, axis=(1, 2))
        for i in range(data.shape[0]):
            # Power-1 energy normalization: scale each frame so its total energy
            # matches the reference frame (sum -> energy[0]). (Was **4, which
            # over-corrected to energy[0]**4 / energy[i]**3.)
            c = energy[0] / energy[i]
            correctedData[i, :, :] = data[i, :, :] * c
        return correctedData


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
