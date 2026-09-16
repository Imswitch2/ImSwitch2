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
        what unblocks the pass-through auto-route in currentDataChanged —
        otherwise the KeyError-only try/except blocks would let a TypeError
        escape and the auto-render path never ran."""
        attrs = dataObj.attrs if dataObj is not None else None
        if not attrs:
            return

        dimensionMap = {
            'X': self._widget.r_l_text,
            'Y': self._widget.u_d_text,
            'Z': self._widget.b_f_text
        }
        try:
            targetsAttr = attrs['ScanStage:target_device']
            for i in range(0, min(3, len(targetsAttr))):
                target = targetsAttr[i]
                if isinstance(target, (bytes, np.bytes_)):
                    target = target.decode(errors='ignore')
                self._scanParDict['dimensions'][i] = dimensionMap[str(target).upper()]
        except (KeyError, TypeError, AttributeError):
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

        numLinesteps = self._positiveIntAttr(attrs, 'ScanTTL:n_linesteps') or 1
        self._scanParDict['n_linesteps'] = numLinesteps

        # Prefer the physical X/Y counts recorded by advanced scans.  Unlike
        # sqrt(numFrames), these remain correct when every physical line is
        # repeated for multiple line-step conditions.
        spatialSteps = {
            self._widget.r_l_text: self._positiveIntAttr(attrs, 'ScanTTL:Nx'),
            self._widget.u_d_text: self._positiveIntAttr(attrs, 'ScanTTL:Ny'),
        }
        for index, dimension in enumerate(self._scanParDict['dimensions'][:3]):
            if spatialSteps.get(dimension) is not None:
                self._scanParDict['steps'][index] = str(spatialSteps[dimension])

        # Older recordings may lack ScanTTL:Nx/Ny but still contain physical
        # axis lengths and pitches. Their convention is positions=length/step.
        try:
            axisLengths = np.asarray(attrs['ScanStage:axis_length'], dtype=float).flatten()
            axisSteps = np.asarray(attrs['ScanStage:axis_step_size'], dtype=float).flatten()
        except (KeyError, TypeError, ValueError):
            axisLengths = axisSteps = np.array([])
        for index in range(min(3, axisLengths.size, axisSteps.size)):
            if spatialSteps.get(self._scanParDict['dimensions'][index]) is not None:
                continue
            if axisSteps[index] != 0:
                count = max(1, int(round(abs(axisLengths[index] / axisSteps[index]))))
                self._scanParDict['steps'][index] = str(count)

        try:
            numFrames = int(dataObj.numFrames)
        except Exception:
            numFrames = None
        if numFrames:
            spatialProduct = int(np.prod(
                np.asarray(self._scanParDict['steps'][:3], dtype=int)
            ))
            if spatialProduct <= 0 or numFrames % spatialProduct != 0:
                # Last-resort compatibility for metadata-light square scans.
                framesPerCondition = (
                    numFrames // numLinesteps
                    if numFrames % numLinesteps == 0 else numFrames
                )
                side = int(np.sqrt(framesPerCondition))
                if side * side == framesPerCondition:
                    self._scanParDict['steps'][0] = str(side)
                    self._scanParDict['steps'][1] = str(side)
                    spatialProduct = side * side * int(self._scanParDict['steps'][2])

            if spatialProduct > 0 and numFrames % spatialProduct == 0:
                outputTimeSteps = numFrames // spatialProduct
                self._scanParDict['steps'][3] = str(outputTimeSteps)
                if outputTimeSteps % numLinesteps != 0:
                    # The metadata cannot describe this detector's frame
                    # stream; retain legacy ordering rather than mis-group it.
                    self._scanParDict['n_linesteps'] = 1

        try:
            stepSizesAttr = attrs['ScanStage:axis_step_size']
        except (KeyError, TypeError):
            pass
        else:
            for i in range(0, min(4, len(stepSizesAttr))):
                self._scanParDict['step_sizes'][i] = str(stepSizesAttr[i] * 1000)  # convert um->nm

        self.updateScanParams()

    @staticmethod
    def _positiveIntAttr(attrs, key):
        """Return a positive scalar integer metadata value, else ``None``."""
        try:
            value = attrs[key]
            if isinstance(value, (list, tuple, np.ndarray)):
                value = np.asarray(value).flatten()
                if value.size != 1:
                    return None
                value = value[0]
            if isinstance(value, (bytes, np.bytes_)):
                value = value.decode(errors='ignore')
            number = int(float(value))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return number if number > 0 else None

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
