import numpy as np

from .basesignaldesigners import ScanDesigner, ScanInfoContract
from ..scan_parameters import pixels_for_length_step, axis_pixel_positions
from imswitch.imcommon.model import initLogger

class BetaScanDesigner(ScanDesigner):
    """ Scan designer for X/Y/Z stages that move a sample.

    Designer params:

    - ``return_time`` -- time to wait between lines for the stage to return to
      the first position of the next line, in seconds.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)
        self._expectedParameters = ['target_device',
                                    'axis_length',
                                    'axis_step_size',
                                    'axis_startpos',
                                    'axis_centerpos',
                                    'return_time']

    def checkSignalComp(self, scanParameters, setupInfo, scanInfo):
        """ Check analog scanning signals so that they are inside the range of
        the acceptable scanner voltages.

        Every EMITTED waveform is checked, including axes held at a single
        position: unlike GalvoScanDesigner (which omits inactive axes from its
        signal dict, so parking is free), Beta drives parked axes to their
        center for the whole scan -- an out-of-range center is a real
        out-of-range output. ``scanInfo['minmaxes']`` carries one [min, max]
        per emitted signal, in ``target_device`` (fast, middle, slow) order.
        Positioners without ``minVolt``/``maxVolt`` are skipped rather than
        rejected, since older stage configs omit them.
        """
        minmaxes = scanInfo.get('minmaxes') if scanInfo else None
        if not minmaxes:
            return True
        targets = scanParameters['target_device']
        for i in range(min(len(targets), len(minmaxes))):
            name = targets[i]
            if name == 'None' or 'Mock' in name:
                continue
            props = setupInfo.positioners[name].managerProperties
            minv = props.get('minVolt')
            maxv = props.get('maxVolt')
            if minv is not None and minmaxes[i][0] < minv:
                return False
            if maxv is not None and minmaxes[i][1] > maxv:
                return False
        return True

    def make_signal(self, parameterDict, setupInfo):
        n_linesteps = int(parameterDict.get("n_linesteps", 1))
        n_linesteps = max(1, n_linesteps)

        if not self.parameterCompatibility(parameterDict):
            self._logger.error([*parameterDict])
            self._logger.error(self._expectedParameters)
            self._logger.error('Stage scan parameters seem incompatible, this error should not be'
                               ' since this should be checked at program start-up')
            return None

        if len(parameterDict['target_device']) != 3:
            raise ValueError(f'{self.__class__.__name__} requires 3 target devices/axes')

        for i in range(3):
            if len(parameterDict['axis_startpos'][i]) > 1:
                raise ValueError(f'{self.__class__.__name__} does not support multi-axis'
                                 f' positioners')

        # Conversion factors looked up per target device NAME. target_device is
        # GUI-dim-ordered (fast, middle, slow) and need not match the
        # setupInfo.positioners order -- the previous positional indexing
        # silently applied another device's factor when scan dims were
        # reordered (e.g. a Z piezo on dim 0 under a galvo-first setup got the
        # galvo's factor: 17x range shrink under example_sted). See
        # docs/galvo-designer-single-axis-findings.md, defect 4.
        scanningProps = {
            name: positioner.managerProperties
            for name, positioner in setupInfo.positioners.items()
            if positioner.forScanning
        }
        unknown = [dev for dev in parameterDict['target_device']
                   if dev not in scanningProps]
        if unknown:
            raise ValueError(
                f'{self.__class__.__name__}: target device(s) {unknown} not '
                f'found among forScanning positioners '
                f'({sorted(scanningProps)})'
            )
        convFactors = [scanningProps[dev].get('conversionFactor', 1)
                       for dev in parameterDict['target_device']]

        # Retrieve sizes
        [fast_axis_size, middle_axis_size, slow_axis_size] = \
            [(parameterDict['axis_length'][i] / convFactors[i]) for i in range(3)]

        # Retrieve step sizes
        [fast_axis_step_size, middle_axis_step_size, slow_axis_step_size] = \
            [(parameterDict['axis_step_size'][i] / convFactors[i]) for i in range(3)]

        # Retrieve center positions. The scan is centered on axis_centerpos (like
        # GalvoScanDesigner) so the ROI 'Center' actually moves stage scans;
        # previously fast_axis_start = startpos - center = 0 made Center inert and
        # every scan ran from 0 (see scan-beta-center-ignored).
        [fast_axis_center, middle_axis_center, slow_axis_center] = \
            [(parameterDict['axis_centerpos'][i] / convFactors[i]) for i in range(3)]

        # Canonical pixel count = round(size / step) via the shared helper, so the
        # number of scanned lines matches the GUI "Pixels (#)" display and the
        # recorded OME dimensions (previously this used ceil while the GUI used
        # round -- the off-by-one for non-divisible ratios).
        fast_axis_positions = 1 if fast_axis_size == 0 else \
            pixels_for_length_step(fast_axis_size, fast_axis_step_size)
        middle_axis_positions = 1 if middle_axis_size == 0 else \
            pixels_for_length_step(middle_axis_size, middle_axis_step_size)
        slow_axis_positions = 1 if slow_axis_size == 0 else \
            pixels_for_length_step(slow_axis_size, slow_axis_step_size)

        # First-pixel position of each axis so the N pixels (pitch = step) are
        # centered on the axis center. Downstream ramp/flyback/wrap logic is
        # start-anchored on these, so centering happens purely here.
        fast_axis_start = fast_axis_center - (fast_axis_positions - 1) * fast_axis_step_size / 2.0
        middle_axis_start = middle_axis_center - (middle_axis_positions - 1) * middle_axis_step_size / 2.0
        slow_axis_start = slow_axis_center - (slow_axis_positions - 1) * slow_axis_step_size / 2.0

        sampleRate = setupInfo.scan.sampleRate
        sequenceSamples = parameterDict['sequence_time'] * sampleRate
        returnSamples = parameterDict['return_time'] * sampleRate
        if not sequenceSamples.is_integer():
            self._logger.warning('Non-integer number of sequence samples, rounding up')
        sequenceSamples = int(np.ceil(sequenceSamples))
        if not returnSamples.is_integer():
            self._logger.warning('Non-integer number of return samples, rounding up')
        returnSamples = int(np.ceil(returnSamples))

        # Make fast axis signal
        rampSamples = fast_axis_positions * sequenceSamples
        lineSamples = rampSamples + returnSamples
        rampSignal = np.zeros(rampSamples)
        self._logger.debug(fast_axis_positions)
        # Pixels spaced exactly one step (realized pitch == reported step); the
        # first pixel stays at fast_axis_start so the scan's start corner does
        # not move. See axis_pixel_positions / scan-realized-step-spacing.
        rampValues = axis_pixel_positions(
            fast_axis_positions, fast_axis_step_size, start=fast_axis_start)
        self._logger.debug(rampValues)
        for s in range(fast_axis_positions):
            start = s * sequenceSamples
            end = s * sequenceSamples + sequenceSamples
            smooth = int(np.ceil(0.002 * sampleRate))
            settling = int(np.ceil(0.002 * sampleRate))
            rampSignal[start: end] = rampValues[s]
            if s != fast_axis_positions - 1:
                if (end - smooth - settling) > 0:
                    rampSignal[end - smooth - settling: end - settling] = self.__smoothRamp(rampValues[s], rampValues[s + 1], smooth)
                    rampSignal[end - settling:end] = rampValues[s + 1]

        # return from the LAST visited pixel (not fast_axis_start+size: the last
        # pixel is now fast_axis_start + (N-1)*step, one step short of the ROI end)
        returnRamp = self.__smoothRamp(rampValues[-1], rampValues[0], returnSamples)
        fullLineSignal = np.concatenate((rampSignal, returnRamp))

        fastAxisSignal = np.tile(fullLineSignal, middle_axis_positions * n_linesteps * slow_axis_positions)

        # Make middle axis signal
        colValues = axis_pixel_positions(
            middle_axis_positions, middle_axis_step_size, start=middle_axis_start)

        colSamples = middle_axis_positions * n_linesteps * lineSamples
        fullSquareSignal = np.zeros(colSamples)

        for s in range(middle_axis_positions):
            for r in range(n_linesteps):
                block0 = (s * n_linesteps + r) * lineSamples
                block1 = block0 + lineSamples

                # hold during the pixel ramp portion
                fullSquareSignal[block0: block0 + rampSamples] = colValues[s]

                # return portion:
                if r < n_linesteps - 1:
                    # stay at same middle position for repeats
                    fullSquareSignal[block0 + rampSamples: block1] = colValues[s]
                else:
                    # only on the last repeat: ramp to next middle position (or wrap)
                    next_val = colValues[s + 1] if (s + 1) < middle_axis_positions else middle_axis_start
                    fullSquareSignal[block0 + rampSamples: block1] = self.__smoothRamp(colValues[s], next_val,
                                                                                       returnSamples)

        middleAxisSignal = np.tile(fullSquareSignal, slow_axis_positions)

        # Make slow axis signal
        sliceSamples = slow_axis_positions * colSamples
        sliceValues = axis_pixel_positions(
            slow_axis_positions, slow_axis_step_size, start=slow_axis_start)
        self._logger.debug(sliceValues)
        fullCubeSignal = np.zeros(sliceSamples)
        for s in range(slow_axis_positions):
            fullCubeSignal[s * colSamples:(s + 1) * colSamples - returnSamples] = sliceValues[s]

            try:
                fullCubeSignal[(s + 1) * colSamples - returnSamples:(s + 1) * colSamples] = \
                    self.__smoothRamp(sliceValues[s], sliceValues[s + 1], returnSamples)
            except IndexError:
                fullCubeSignal[(s + 1) * colSamples - returnSamples:(s + 1) * colSamples] = \
                    self.__smoothRamp(sliceValues[s], slow_axis_start, returnSamples)
        slowAxisSignal = fullCubeSignal

        if slow_axis_size > 0:
            sig_dict = {parameterDict['target_device'][0]: fastAxisSignal,
                        parameterDict['target_device'][1]: middleAxisSignal,
                        parameterDict['target_device'][2]: slowAxisSignal}
            positions = [fast_axis_positions, middle_axis_positions, slow_axis_positions]
        else:
            sig_dict = {parameterDict['target_device'][0]: fastAxisSignal,
                        parameterDict['target_device'][1]: middleAxisSignal}
            positions = [fast_axis_positions, middle_axis_positions]

        # Build complete scanInfoDict via ScanInfoContract
        img_dims = list(positions)
        img_axes_phys = ["x", "y", "z"][:len(img_dims)]
        pixel_sizes = [parameterDict['axis_step_size'][i] for i in range(len(img_dims))]

        # ScanInfoContract: scan_samples is LEVEL-based and one element longer
        # than the number of physical axes: [per_pixel, per_line, per_frame(,
        # per_stack)] (sequenceSamples=per_pixel, rampSamples=per_line,
        # colSamples=per_frame, sliceSamples=per_stack). Sample-stream consumers
        # (APDManager/PMTManager) and PointScanTTLCycleDesigner index it per
        # level, so the trailing per-frame/per-stack element MUST be present even
        # for a 2-axis scan -- omitting it (the old behavior) broke APD/PMT >=3D
        # scans (samples_d_scanstep[:-1] dropped a real axis).
        scan_samples = [sequenceSamples, rampSamples, colSamples]
        if slow_axis_size > 0:
            scan_samples.append(sliceSamples)

        contract = ScanInfoContract(
            img_dims=img_dims,
            img_axes_phys=img_axes_phys,
            pixel_sizes=pixel_sizes,
            scan_samples=scan_samples,
            scan_samples_total=len(fastAxisSignal),
            scan_samples_d2_period=lineSamples,
            n_pixels_fast=fast_axis_positions,
            samples_per_pixel=sequenceSamples,
            dwell_time=parameterDict['sequence_time'],
            scan_time_step=1.0 / sampleRate,
            n_linesteps=n_linesteps,
            positions=positions,
            return_time=parameterDict['return_time'],
            # One [min, max] per EMITTED signal, aligned with sig_dict /
            # target_device order, so checkSignalComp checks exactly what will
            # be written to the AO channels (parked axes included -- they are
            # driven to their center) and nothing that will not.
            minmaxes=[[float(np.min(s)), float(np.max(s))] for s in
                      ((fastAxisSignal, middleAxisSignal, slowAxisSignal)
                       if slow_axis_size > 0 else
                       (fastAxisSignal, middleAxisSignal))],
        )
        scanInfoDict = contract.to_dict()

        self.__plot_curves(plot=False, signals=[fastAxisSignal, middleAxisSignal, slowAxisSignal])

        return sig_dict, positions, scanInfoDict

    def __smoothRamp(self, start, end, samples):
        start = float(start)
        end = float(end)
        curve_half = 0.6
        n = int(np.floor(curve_half * samples))
        x = np.linspace(0, np.pi / 2, num=n, endpoint=True)
        signal = start + (end - start) * np.sin(x)
        signal = np.append(signal, end * np.ones(int(np.ceil((1 - curve_half) * samples))))
        return signal

    def __plot_curves(self, plot, signals):
        """ Plot all scan curves, for debugging. """
        if plot:
            import matplotlib.pyplot as plt
            plt.figure(1)
            plt.clf()
            for i, signal in enumerate(signals):
                plt.plot(signal - 0.01 * i)
            plt.show()

# Copyright (C) 2020, 2021 TestaLab
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
