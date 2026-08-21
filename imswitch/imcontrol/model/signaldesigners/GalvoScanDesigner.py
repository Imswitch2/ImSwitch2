import numpy as np
import matplotlib.pyplot as plt

from scipy.interpolate import BPoly

from .basesignaldesigners import ScanDesigner, ScanInfoContract
from ..scan_parameters import pixels_for_length_step, axis_pixel_positions
from imswitch.imcommon.model import initLogger


class GalvoScanDesigner(ScanDesigner):
    """ Scan designer for scan systems with galvanometric mirrors.

    Designer params: None
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)

        self._expectedParameters = ['target_device',
                                    'axis_length',
                                    'axis_step_size',
                                    'axis_centerpos',
                                    'axis_startpos',
                                    'sequence_time']

        self._debug_mode = False  # run mode for generating detected samples and plotting detected samples

    def checkSignalComp(self, scanParameters, setupInfo, scanInfo):
        """ Check analog scanning signals so that they are inside the range of
        the acceptable scanner voltages.

        Every emitted waveform is checked. ``scanInfo['axis_names']`` lists
        the active axes in signal order, aligned with
        ``scanInfo['minmaxes']``; the previous version indexed ``minmaxes``
        with the unfiltered ``target_device`` order, which misaligns as soon
        as a configured axis collapses to one step (inactive axes are
        filtered out of the signals but stayed in the indexing).
        """
        for name, (mn, mx) in zip(scanInfo.get('axis_names', []),
                                  scanInfo.get('minmaxes', [])):
            if name == 'None' or 'Mock' in name:
                continue
            props = setupInfo.positioners[name].managerProperties
            minv = props.get('minVolt')
            maxv = props.get('maxVolt')
            if minv is not None and mn < minv:
                return False
            if maxv is not None and mx > maxv:
                return False
        return True

    def checkSignalLength(self, scanParameters, setupInfo):
        """ Check that the signal would not be too large (to be stored in
        the RAM and to be generated and run inside a reasonable time). """
        # TODO: Think about changing the way >d3 steps are generated and sent to the nidaq -
        # could run scans as simple d2 scans and repeat them, with steps on the axes that needs steps between.
        # Keep track of that in ScanController for example, alternatively directly in NidaqController.runScan?
        device_count = len([positioner for positioner in setupInfo.positioners.values() if positioner.forScanning])
        active = self._active_axis_indices(
            scanParameters['axis_length'], scanParameters['axis_step_size'], device_count
        )
        axis_length = [scanParameters['axis_length'][i] for i in active]
        axis_count_scan = len(axis_length)
        axis_step_size = [scanParameters['axis_step_size'][i] for i in active]
        # get list of number of axis steps (round(len/step); see pixels_for_length_step)
        n_steps_dx = [pixels_for_length_step(axis_length[i], axis_step_size[i]) for i in range(axis_count_scan)]
        # TODO: Update these limits, arbitrarly 
        scan_steps = np.prod(n_steps_dx)
        min_scan_time = scan_steps * scanParameters['sequence_time'] * 2
        if hasattr(setupInfo.scan, "maxScanTimeMin"):
            if setupInfo.scan.maxScanTimeMin and min_scan_time > 60*setupInfo.scan.maxScanTimeMin:
                return False
        if scan_steps > 1e7:
            return False
        return True

    def make_signal(self, parameterDict, setupInfo):
        # --- Per-call state (reset each invocation, accumulated by private methods) ---
        self.__timestep = 1e6 / setupInfo.scan.sampleRate  # time step [µs]
        self.__paddingtime_d3step = int(parameterDict['d3step_delay'])  # inter-slice delay [µs]
        self.__paddingtime_full = 100  # safety padding [µs]
        self._samples_initpos = []
        self._samples_finalpos = []
        self._samples_settling = 0
        self._samples_startacc = 0

        n_linesteps = int(parameterDict.get("n_linesteps", 1))
        n_linesteps = max(1, n_linesteps)

        positioners = [positioner for positioner in setupInfo.positioners.values()
                       if positioner.forScanning]
        positionerNames = [positioner for positioner in setupInfo.positioners
                           if setupInfo.positioners[positioner].forScanning]
        positionersProps = [positioner.managerProperties for positioner in positioners]

        # Real (non-mock) galvo axes need vel_max/acc_max for the smooth-scan
        # spline. The old ``else 1e6`` fallback silently accepted a missing limit
        # and then produced degenerate spline knots + an opaque BPoly crash mid
        # build. Require them explicitly so a misconfigured setup fails early with
        # an actionable message. (Mock/alignment axes are non-smooth and exempt.)
        missing = [
            name for name, props in zip(positionerNames, positionersProps)
            if 'mock' not in name.lower()
            and ('vel_max' not in props or 'acc_max' not in props)
        ]
        if missing:
            raise ValueError(
                "GalvoScanDesigner requires 'vel_max' (µm/µs) and 'acc_max' "
                f"(µm/µs^2) in managerProperties for scanning positioner(s) {missing}. "
                "Add realistic values (example_sted uses vel_max=0.1, acc_max=0.0001); "
                "without them the smooth-scan spline degenerates."
            )

        device_count = len(positioners)
        # convert vel_max from µm/µs to V/µs
        vel_max = [positionerProps['vel_max'] / positionerProps['conversionFactor']
                   if 'vel_max' in positionerProps else 1e6
                   for positionerProps in positionersProps]
        # convert acc_max from µm/µs^2 to V/µs^2
        acc_max = [positionerProps['acc_max'] / positionerProps['conversionFactor']
                   if 'acc_max' in positionerProps else 1e6
                   for positionerProps in positionersProps]
        # convert jerk_max from µm/µs^3 to V/µs^3 (optional)
        jerk_max = [positionerProps['jerk_max'] / positionerProps['conversionFactor']
                    if 'jerk_max' in positionerProps else None
                    for positionerProps in positionersProps]

        # get conversion factors for scanning axes
        convFactors = [positionerProps['conversionFactor'] 
                       if 'conversionFactor' in positionerProps else 1
                       for positionerProps in positionersProps]

        # Determine which axes are active (more than 1 scan step)
        active = self._active_axis_indices(
            parameterDict['axis_length'], parameterDict['axis_step_size'], device_count
        )

        # Per-call scan axis properties (converted to voltage units via convFactors)
        self.axis_devs_order = [parameterDict['target_device'][i] for i in active]
        pos_idx = [positionerNames.index(parameterDict['target_device'][i]) for i in active]
        self.axis_length = [parameterDict['axis_length'][i] / convFactors[pos_idx[j]]
                            for j, i in enumerate(active)]
        self.axis_step_size = [parameterDict['axis_step_size'][i] / convFactors[pos_idx[j]]
                               for j, i in enumerate(active)]
        self.axis_centerpos = [parameterDict['axis_centerpos'][i] / convFactors[pos_idx[j]]
                               for j, i in enumerate(active)]
        self.axis_vel_max = [vel_max[pos_idx[j]] for j in range(len(active))]
        self.axis_acc_max = [acc_max[pos_idx[j]] for j in range(len(active))]
        self.axis_jerk_max = [jerk_max[pos_idx[j]] for j in range(len(active))]

        # Compute jerk-transition time (dt_fix): acc_max / jerk_max for each smooth axis with jerk_max
        dt_fix_candidates = []
        for j in range(len(active)):
            # Only consider smooth scanning axes (not mock) with jerk_max configured
            is_smooth = not ('mock' in self.axis_devs_order[j].lower())
            if is_smooth and self.axis_jerk_max[j] is not None:
                dt_fix_candidates.append(self.axis_acc_max[j] / self.axis_jerk_max[j])
        # Use max of computed values, or fall back to legacy 1e-2 if no jerk_max configured
        self.__dt_fix = max(dt_fix_candidates) if dt_fix_candidates else 1e-2

        axis_count_scan = len(self.axis_devs_order)

        # get list of number of axis steps (round(len/step); see pixels_for_length_step)
        n_steps_dx = [pixels_for_length_step(self.axis_length[i], self.axis_step_size[i]) for i in range(axis_count_scan)]
        # get list of number of axis scan samples, for first two axes initially
        n_scan_samples_dx = [int(round(parameterDict['sequence_time'] * 1e6 / self.__timestep))]
        n_scan_samples_dx.append(int(round(n_steps_dx[0] * parameterDict['sequence_time'] * 1e6 / self.__timestep)))
        # Per ACTIVE axis, in signal order -- indexing the unfiltered
        # parameterDict here reported the wrong step sizes whenever a
        # configured axis collapsed to one step (see sig_dict below).
        pixel_sizes = [parameterDict['axis_step_size'][i] for i in active]

        # get list of d1 positions for each active axis
        # (must match n_steps_dx / img_dims -- previously this used ceil while
        # img_dims used int(), so the returned positions disagreed with the
        # generated waveform for non-divisible ratios)
        axis_positions = list(n_steps_dx)

        # get parameter for which axes should be smooth
        self.__smooth_axis = [False if 'mock' in axis_name.lower() else True for axis_name in self.axis_devs_order]

        # generate axis signals for all d axes
        pos = []  # list with all axis positions lists
        # d1 axis signal
        axis = 0
        #smooth = False if 'mock' in self.axis_devs_order[axis].lower() else True
        if self.__smooth_axis[axis]:
            # calculate settling time to add to smooth axis
            self.__settlingtime = self.__calc_settling_time(self.axis_length, self.axis_centerpos, self.axis_vel_max, self.axis_acc_max)
            n_d2 = n_steps_dx[axis + 1] if axis_count_scan > 1 else 1
            n_d2_eff = n_d2 * n_linesteps
            pos_temp, samples_d2_period = self.__generate_smooth_scan(
                parameterDict, self.axis_vel_max[axis], self.axis_acc_max[axis], n_d2_eff
            )
            samples_d2_period_read = samples_d2_period - 1
        else:
            pos_temp, _ = self.__generate_step_scan(axis, n_scan_samples_dx[axis], n_steps_dx[axis], self.__smooth_axis, v_max=self.axis_vel_max[axis], a_max=self.axis_acc_max[axis])
            # single-active-axis scans have no d2 axis: one d2 step
            n_d2_steps = n_steps_dx[axis + 1] if axis_count_scan > 1 else 1
            pos_temp = self.__generate_tiledstep_multid2(pos_temp, n_d2_steps * n_linesteps)
            samples_d2_period = n_scan_samples_dx[axis+1]
            samples_d2_period_read = samples_d2_period
        pos.append(pos_temp)

        # initiate pad length list
        #pad_prev_axes = []

        # d2 axis signal
        if axis_count_scan > 1:
            axis = 1
            axis_reps = self.__get_axis_reps(pos[0], samples_d2_period, n_steps_dx[1], self.__smooth_axis[axis-1])
            pos_temp, pad_prev_axis = self.__generate_step_scan(axis, n_scan_samples_dx[axis], n_steps_dx[axis], self.__smooth_axis, v_max=self.axis_vel_max[axis], a_max=self.axis_acc_max[axis], axis_reps=axis_reps, n_linesteps=n_linesteps)
            if pad_prev_axis:
                pos, _ = self.__zero_padding(pos, padlen_base=pad_prev_axis)
                #pad_prev_axes.append(pad_prev_axis)
            pos.append(pos_temp)
            n_scan_samples_dx.append(len(pos[0]))

        # d>2 axes signals - all generated as pure step signals
        if axis_count_scan > 2:
            for axis in range(2, axis_count_scan):
                pos, pad_max = self.__zero_padding(pos, padlen_base=[0,0])
                pos = self.__repeat_dlower(pos, n_steps_dx[axis])
                n_scan_samples_dx[-1] = n_scan_samples_dx[-1] + pad_max
                pos_temp, pad_prev_axis = self.__generate_step_scan(axis, n_scan_samples_dx[axis], n_steps_dx[axis], self.__smooth_axis, v_max=self.axis_vel_max[axis], a_max=self.axis_acc_max[axis])
                if pad_prev_axis:
                    pos, _ = self.__zero_padding(pos, padlen_base=pad_prev_axis)
                    #pad_prev_axes.append(pad_prev_axis)
                pos.append(pos_temp)
                n_scan_samples_dx.append(len(pos[0]))

        pos, pad_max = self.__zero_padding(pos, padlen_base=[0,0])
        n_scan_samples_dx[-1] = n_scan_samples_dx[-1] + pad_max

        # pad all signals with zeros, for initial and final settling of galvos and safety start and end
        axis_signals, pad_max = self.__zero_padding(pos, padlen_base=[int(round(self.__paddingtime_full / self.__timestep)),int(round(self.__paddingtime_full / self.__timestep))])

        # add all signals to a signal dictionary, keyed by the ACTIVE axes in
        # signal order. Indexing the unfiltered target_device list routed
        # waveforms to the wrong devices whenever a configured axis collapsed
        # to one step (e.g. d1 with 1 step + active d2/d3: d2's waveform was
        # keyed under d1's device name and driven onto d1's AO channel).
        sig_dict = {self.axis_devs_order[i]: axis_signals[i] for i in range(axis_count_scan)}

        # create scan information dictionary via ScanInfoContract
        # Truthful total duration: the full emitted signal, padding and
        # positioning included. scan_samples stays NOMINAL (the detector's
        # line-read contract); the old n_scan_samples_dx[-1] * timestep
        # understated the real duration, and its only consumer is the Swabian
        # manager's diagnostics log.
        tot_scan_time = len(axis_signals[0]) * self.__timestep * 1e-6
        contract = ScanInfoContract(
            img_dims=list(n_steps_dx),
            img_axes_phys=["x", "y", "z"][:len(n_steps_dx)],
            pixel_sizes=pixel_sizes,
            scan_samples=n_scan_samples_dx,
            scan_samples_total=len(axis_signals[0]),
            scan_samples_d2_period=samples_d2_period_read,
            n_pixels_fast=n_steps_dx[0],
            samples_per_pixel=n_scan_samples_dx[0],
            dwell_time=parameterDict['sequence_time'],
            scan_time_step=round(self.__timestep * 1e-6, ndigits=10),
            n_linesteps=n_linesteps,
            scan_throw_startzero=int(round(self.__paddingtime_full / self.__timestep)),
            scan_throw_settling=self._samples_settling,
            scan_throw_startacc=self._samples_startacc,
            scan_pads_initpos=self._samples_initpos,
            phase_delay=parameterDict['phase_delay'],
            smooth_axes=self.__smooth_axis,
            axis_names=self.axis_devs_order,
            minmaxes=[[min(axis_signals[i]), max(axis_signals[i])] for i in range(axis_count_scan)],
            tot_scan_time_s=tot_scan_time,
        )
        scanInfoDict = contract.to_dict()

        if self._debug_mode:
            self._logger.debug(scanInfoDict)
            self.__plot_curves(plot=True, signals=axis_signals)  # for debugging

        #self._logger.info(f'Scanning curves generated, third dimension step time: {round(self.__timestep * 1e-6 * n_scan_samples_dx[2], ndigits=5)} s, total scan time: {tot_scan_time} s.')
        return sig_dict, axis_positions, scanInfoDict

    @staticmethod
    def _active_axis_indices(axis_lengths, axis_step_sizes, device_count):
        """Return indices of axes with more than 1 scan step."""
        return [i for i in range(device_count)
                if pixels_for_length_step(axis_lengths[i], axis_step_sizes[i]) > 1]

    def __calc_settling_time(self, axis_length, axis_centerpos, vel_max, acc_max):
        """ Calculate settling time based on all axis parameters. """
        t_initpos_vc = [abs(axis_centerpos[i] - axis_length[i] / 2) / vel_max[i] for i in range(len(axis_length))]
        t_acc = [vel_max[i] / acc_max[i] for i in range(len(axis_length))]
        t_initpos = [t_initpos_vc[i] + 2 * t_acc[i] for i in range(len(axis_length)) if self.__smooth_axis[i]]
        settlingtime = self.__paddingtime_d3step + (np.max(t_initpos) - np.min(t_initpos))
        return settlingtime

    def __generate_smooth_scan(self, parameterDict, v_max, a_max, n_d2):
        """ Generate a smooth scanning curve with spline interpolation """
        curve_poly, time_fix, pos_fix = self.__d2scan_poly(parameterDict, v_max, a_max)
        # calculate number of evaluation points for a d2 step for decided timestep
        n_eval = int(time_fix[-1] / self.__timestep)
        # evaluate ONE d2 period; the final sample would duplicate the start
        # of the next period, so the tiled form drops it
        x_eval = np.linspace(0, time_fix[-1], n_eval)
        period = curve_poly(x_eval)[:-1]
        if period.size == 0:
            raise ValueError(
                'GalvoScanDesigner: one fast-axis period evaluates to zero '
                f'samples (period {time_fix[-1]:.4g} µs < sample step '
                f'{self.__timestep:.4g} µs). Check sequence_time, the scan '
                'sampleRate and the fast axis\'s vel_max/acc_max.'
            )
        # middle of the scan: (n_d2 - 1) whole periods -- legitimately EMPTY
        # for a single-d2-step (1-active-axis) scan. __add_start_end slices
        # the missing start/end half-periods from ``period`` itself, so
        # n_d2 == 1 degenerates to exactly one full sweep instead of crashing
        # on np.min of an empty array (defect 1 in
        # docs/galvo-designer-single-axis-findings.md).
        pos = np.tile(period, n_d2 - 1)
        # add missing start and end piece
        pos_ret = self.__add_start_end(pos, period, pos_fix, v_max, a_max)
        return pos_ret, n_eval

    def __generate_step_scan(self, dim, len_axis, n_axis, smooth_axis,
                             v_max=0, a_max=0, axis_reps=None, n_linesteps=1):
        """Generate a step-function scanning curve, with optional smooth init/final positioning.

        Contract:
          - for dim==1 (Y): n_axis is the PHYSICAL number of Y positions (ny_phys)
          - axis_reps is computed for ny_phys (NOT expanded)
          - n_linesteps expands Y blocks internally
        """
        if axis_reps is None:
            axis_reps = [0, 0]
        l_scan = self.axis_length[dim]
        c_scan = self.axis_centerpos[dim]
        pad_prev_axis = False

        n_linesteps = max(1, int(n_linesteps))
        n_axis = int(n_axis)

        # --- physical positions (do NOT bake linesteps into spacing) ---
        # Pixels spaced exactly one step, centered on c_scan (span (N-1)*step), so
        # the realized pitch equals the reported step -- matching the fast axis
        # (constant velocity = step/dwell). Previously used linspace over the full
        # l_scan -> pitch l_scan/n_axis, which disagreed with the fast axis and
        # anisotropically distorted pixels. See scan-realized-step-spacing.
        step_axis = self.axis_step_size[dim]
        positions_phys = axis_pixel_positions(n_axis, step_axis, center=c_scan)

        # Expand Y positions into linestep blocks if dim==1
        if dim == 1 and n_linesteps > 1:
            positions = np.repeat(positions_phys, n_linesteps)
        else:
            positions = positions_phys

        if smooth_axis[dim]:
            # smooth init/final positioning
            pos_init = self.__init_positioning(positions[0], v_max, a_max)
            self._samples_initpos.append(len(pos_init))

            pos_final = self.__final_positioning(positions[-1], v_max, a_max)
            self._samples_finalpos.append(len(pos_final))

            if dim == 1:
                # axis_reps is provided for physical ny -> expand to match expanded positions
                axis_reps = np.asarray(axis_reps, dtype=int)

                if axis_reps.size != positions_phys.size:
                    raise ValueError(
                        f"axis_reps length ({axis_reps.size}) must match physical ny ({positions_phys.size})"
                    )

                if n_linesteps > 1:
                    axis_reps_exp = np.repeat(axis_reps, n_linesteps)
                else:
                    axis_reps_exp = axis_reps

                # IMPORTANT: apply the 'first-step' correction only once overall (first expanded block)
                if smooth_axis[0] and self._samples_initpos[-1] == np.max(self._samples_initpos):
                    if self._samples_initpos[:-1]:
                        corr = int(np.max(self._samples_initpos[:-1]))
                        axis_reps_exp[0] = int(axis_reps_exp[0] - corr)

                pos_ret = np.repeat(positions, axis_reps_exp)

            else:
                reps = np.ones(len(positions)) * len_axis
                if True in smooth_axis[:dim] and self._samples_initpos[-1] == np.max(self._samples_initpos):
                    if self._samples_initpos[:-1]:
                        reps[0] = reps[0] - np.max(self._samples_initpos[:-1])
                reps = [int(rep) for rep in reps]
                pos_ret = np.repeat(positions, reps)

            pos_ret = np.concatenate((pos_init, pos_ret, pos_final))

            padlen_init = (len(pos_init) - np.max(self._samples_initpos[:-1])) if self._samples_initpos[:-1] else len(
                pos_init)
            padlen_final = (len(pos_final) - np.max(self._samples_finalpos[:-1])) if self._samples_finalpos[
                                                                                     :-1] else len(pos_final)
            if padlen_init > 0 or padlen_final > 0:
                pad_prev_axis = [max(0, padlen_init), max(0, padlen_final)]

        else:
            # non-smooth (mock) axis: realign positions
            positions = positions - positions[0]
            if dim == 1:
                axis_reps = np.asarray(axis_reps, dtype=int)
                if axis_reps.size != positions_phys.size:
                    raise ValueError(
                        f"axis_reps length ({axis_reps.size}) must match physical ny ({positions_phys.size})"
                    )
                axis_reps_exp = np.repeat(axis_reps, n_linesteps) if n_linesteps > 1 else axis_reps
                pos_ret = np.repeat(positions, axis_reps_exp)
            else:
                pos_ret = np.repeat(positions, len_axis)

        return pos_ret, pad_prev_axis

    def __repeat_dlower(self, pos, n_steps_axis):
        """ Repeat the current positions, for all dimensions,
        for the number of steps on the new axis. """
        pos_ret = [np.tile(pos_dim, n_steps_axis) for pos_dim in pos]
        return pos_ret

    def __get_axis_reps(self, pos, samples_period, n_d2, smooth=True):
        """ Get reps for each step on d2 axis, by looking at the maximum and
        periods of the d1 axis """
        if smooth:
            # get length of first d2 step
            start_skip = np.max(self._samples_initpos) + self._samples_settling + self._samples_startacc
            end_skip = np.max(self._samples_finalpos)
            # NB len(pos) - end_skip, not -end_skip: a device whose final
            # positioning is shorter than one sample gives end_skip == 0, and
            # pos[start:-0] is EMPTY -- the old "argmax of an empty sequence"
            # crash for stiff (piezo-like) devices on d1.
            search = pos[start_skip:len(pos) - end_skip]
            if search.size == 0:
                raise ValueError(
                    'GalvoScanDesigner: cannot locate the first fast-axis '
                    f'period (positioning takes {start_skip}+{end_skip} of '
                    f'{len(pos)} samples). The d1 device is likely not '
                    'sweepable with its vel_max/acc_max; mark it '
                    "'smoothScan': false in managerProperties to scan it "
                    'stepwise.'
                )
            first_d2 = [np.argmax(search) + start_skip]
            # get length of all other d2 steps
            rest_d2s = np.repeat(samples_period - 1, n_d2 - 1)
        else:
            # get length of first d2 step
            first_d2 = [samples_period]
            # get length of all other d2 steps
            rest_d2s = np.repeat(samples_period, n_d2 - 1)
        # concatenate all repetition lengths
        return np.concatenate((first_d2, rest_d2s))

    @staticmethod
    def _smooth_scan_bpoly(time, yder):
        """``BPoly.from_derivatives`` with an actionable error on degenerate knots.

        The smooth-scan knot times are cumulative segment durations (accel,
        constant-velocity, flyback, jerk-transition). When ``vel_max``/``acc_max``
        are far too large for the scan velocity -- most importantly the ``1e6``
        fallback used when a scanning positioner omits ``vel_max``/``acc_max`` --
        the flyback/settling segments collapse below the jerk-transition time and
        the knot times stop being strictly increasing. ``BPoly`` then raises an
        opaque ``"x must be strictly increasing"``. Fail with a message that points
        at the real cause instead.
        """
        t = np.asarray(time, dtype=float)
        if t.size < 2 or not np.all(np.diff(t) > 0):
            raise ValueError(
                "GalvoScanDesigner smooth-scan spline has non-increasing time knots "
                f"({list(t)}). This happens when vel_max/acc_max are too large for the "
                "scan velocity -- notably the 1e6 fallback used when a scanning "
                "positioner omits vel_max/acc_max. Set realistic vel_max (µm/µs) and "
                "acc_max (µm/µs^2) in the scanning positioners' managerProperties."
            )
        return BPoly.from_derivatives(t, yder)

    def __d2scan_poly(self, parameterDict, v_max, a_max):
        """ Generate a Bernstein piecewise polynomial for a smooth one-d2-step
        scanning curve, from the acquisition parameter settings, using
        piecewise spline interpolation """
        sequence_time = parameterDict['sequence_time'] * 1e6  # s --> µs
        l_scan = self.axis_length[0]  # µm
        c_scan = self.axis_centerpos[0]  # µm
        v_scan = self.axis_step_size[0] / sequence_time  # µm/µs

        # jerk-transition time: acceleration change interval (finite jerk if jerk_max configured) - µs
        dt_fix = self.__dt_fix

        # positions at fixed points
        p1 = c_scan
        p2 = p2p = p1 + l_scan / 2
        t_deacc = (v_scan + v_max) / a_max
        d_deacc = v_scan * t_deacc + 0.5 * (-a_max) * t_deacc ** 2
        p3 = p3p = p2 + d_deacc
        p4 = p4p = c_scan - (p3 - c_scan)
        p5 = p5p = p1 - l_scan / 2
        p6 = p1
        pos = [p1, p2, p2p, p3, p3p, p4, p4p, p5, p5p, p6]

        # time at fixed points
        t1 = 0
        t_scand2 = l_scan / v_scan
        t2 = t1 + t_scand2 / 2
        t2p = t2 + dt_fix
        t3 = t2 + t_deacc
        t3p = t3 + dt_fix
        t4 = t3 + abs(p4 - p3) / v_max
        t4p = t4 + dt_fix
        t_acc = t_deacc
        t5 = t4 + t_acc
        t5p = t5 + dt_fix
        t6 = t5 + t_scand2 / 2
        time = [t1, t2, t2p, t3, t3p, t4, t4p, t5, t5p, t6]

        # velocity at fixed points
        v1 = v_scan
        v2 = v2p = v_scan
        v3 = v3p = v4 = v4p = -v_max
        v5 = v5p = v6 = v_scan
        vel = [v1, v2, v2p, v3, v3p, v4, v4p, v5, v5p, v6]

        # acceleration at fixed points
        a1 = a2 = 0
        a2p = a3 = -a_max
        a3p = a4 = 0
        a4p = a5 = a_max
        a5p = a6 = 0
        acc = [a1, a2, a2p, a3, a3p, a4, a4p, a5, a5p, a6]
        
        # if p3 is already past the center of the scan it means that the max_velocity was never
        # reached in this case, remove two fixed points, and change the values to the curr. vel and
        # time in the middle of the flyback
        if p3 <= c_scan:
            t_mid = np.roots([-a_max / 2, v_scan, p2 - c_scan])[0]
            v_mid = -a_max * t_mid + v_scan
            del pos[5:7]
            del vel[5:7]
            del acc[5:7]
            del time[5:7]
            pos[3:5] = [c_scan, c_scan]
            vel[3:5] = [v_mid, v_mid]
            acc[3:5] = [-a_max, a_max]
            time[3] = time[2] + t_mid
            time[4] = time[3] + dt_fix
            time[5] = time[3] + t_mid
            time[6] = time[5] + dt_fix
            time[7] = time[5] + t_scand2 / 2
        # generate Bernstein polynomial with piecewise spline interpolation with the fixed points
        # give positions, velocity, acceleration, and time of fixed points
        yder = np.array([pos, vel, acc]).T.tolist()

        bpoly = self._smooth_scan_bpoly(time, yder)  # bpoly time unit: µs
        # return polynomial, that can be evaluated at any timepoints you want
        # return fixed points position and time
        return bpoly, time, pos

    def __generate_tiledstep_multid2(self, pos, n_d2):
        pos_ret = np.tile(pos, n_d2)
        return pos_ret

    def __init_positioning(self, initpos, v_max, a_max):
        v_max = np.sign(initpos) * v_max
        a_max = np.sign(initpos) * a_max

        # jerk-transition time: acceleration change interval (finite jerk if jerk_max configured) - µs
        dt_fix = self.__dt_fix

        # positions at fixed points
        p1 = p1p = 0
        t_deacc = v_max / a_max
        d_deacc = 0.5 * a_max * t_deacc ** 2
        p2 = p2p = d_deacc
        p3 = p3p = initpos - d_deacc
        p4 = initpos
        pos = [p1, p1p, p2, p2p, p3, p3p, p4]

        # time at fixed points
        t1 = 0
        t1p = dt_fix
        t2 = t_deacc
        t2p = t2 + dt_fix
        t3 = t2 + abs(abs(p3 - p2) / v_max)
        t3p = t3 + dt_fix
        t4 = t3 + t_deacc
        time = [t1, t1p, t2, t2p, t3, t3p, t4]

        # velocity at fixed points
        v1 = v1p = 0
        v2 = v2p = v3 = v3p = v_max
        v4 = 0
        vel = [v1, v1p, v2, v2p, v3, v3p, v4]

        # acceleration at fixed points
        a1 = 0
        a1p = a2 = a_max
        a2p = a3 = 0
        a3p = a4 = -a_max
        acc = [a1, a1p, a2, a2p, a3, a3p, a4]

        # if p2 is already past the center of the scan it means that the max_velocity was never
        # reached in this case, remove two fixed points, and change the values to the curr. vel and
        # time in the middle of the flyback
        if abs(p2 - p1) >= abs(initpos / 2):
            t_mid = np.sqrt(abs(initpos / a_max))
            v_mid = a_max * t_mid
            del pos[4:6]
            del vel[4:6]
            del acc[4:6]
            del time[4:6]
            pos[2:4] = [initpos / 2, initpos / 2]
            vel[2:4] = [v_mid, v_mid]
            acc[2:4] = [a_max, -a_max]
            time[2] = t_mid
            time[3] = t_mid + dt_fix
            time[4] = 2 * t_mid

        # generate Bernstein polynomial with piecewise spline interpolation with the fixed points
        # give positions, velocity, acceleration, and time of fixed points
        yder = np.array([pos, vel, acc]).T.tolist()
        bpoly = self._smooth_scan_bpoly(time, yder)  # bpoly time unit: µs

        # get number of evaluation points
        n_eval = int(time[-1] / self.__timestep)
        # get evaluation times for one d2 step
        t_eval = np.linspace(0, time[-1], n_eval)
        # evaluate polynomial
        poly_eval = bpoly(t_eval)
        # return evaluated polynomial at the timestep I want
        return poly_eval

    def __final_positioning(self, initpos, v_max, a_max):
        """ Generate a polynomial for a smooth final positioning scanning
        curve, from the acquisition parameter settings """
        v_max = -np.sign(initpos) * v_max
        a_max = -np.sign(initpos) * a_max

        # jerk-transition time: acceleration change interval (finite jerk if jerk_max configured) - µs
        dt_fix = self.__dt_fix

        # positions at fixed points
        p1 = p1p = initpos
        t_deacc = (v_max) / a_max
        d_deacc = 0.5 * a_max * t_deacc ** 2
        p2 = p2p = initpos + d_deacc
        p3 = p3p = -d_deacc
        p4 = 0
        pos = [p1, p1p, p2, p2p, p3, p3p, p4]

        # time at fixed points
        t1 = 0
        t1p = dt_fix
        t2 = t_deacc
        t2p = t2 + dt_fix
        t3 = t2 + abs(abs(p3 - p2) / v_max)
        t3p = t3 + dt_fix
        t4 = t3 + t_deacc
        time = [t1, t1p, t2, t2p, t3, t3p, t4]

        # velocity at fixed points
        v1 = v1p = 0
        v2 = v2p = v3 = v3p = v_max
        v4 = 0
        vel = [v1, v1p, v2, v2p, v3, v3p, v4]

        # acceleration at fixed points
        a1 = 0
        a1p = a2 = a_max
        a2p = a3 = 0
        a3p = a4 = -a_max
        acc = [a1, a1p, a2, a2p, a3, a3p, a4]

        # if p2 is already past the center of the scan it means that the max_velocity was never
        # reached in this case, remove two fixed points, and change the values to the curr. vel and
        # time in the middle of the flyback
        if abs(p2 - p1) >= abs(initpos / 2):
            t_mid = np.sqrt(abs(initpos / a_max))
            v_mid = a_max * t_mid
            del pos[4:6]
            del vel[4:6]
            del acc[4:6]
            del time[4:6]
            pos[2:4] = [initpos / 2, initpos / 2]
            vel[2:4] = [v_mid, v_mid]
            acc[2:4] = [a_max, -a_max]
            time[2] = t_mid
            time[3] = t_mid + dt_fix
            time[4] = 2 * t_mid

        # generate Bernstein polynomial with piecewise spline interpolation with the fixed points
        # give positions, velocity, acceleration, and time of fixed points
        yder = np.array([pos, vel, acc]).T.tolist()
        bpoly = self._smooth_scan_bpoly(time, yder)  # bpoly time unit: µs

        # get number of evaluation points
        n_eval = int(time[-1] / self.__timestep)
        # get evaluation times for one d2 step
        t_eval = np.linspace(0, time[-1], n_eval)
        # evaluate polynomial
        poly_eval = bpoly(t_eval)
        # return evaluated polynomial at the timestep I want
        return poly_eval

    def __add_start_end(self, pos, period, pos_fix, v_max, a_max):
        """ Add start and end half-d2-steps to smooth scanning curve.

        ``pos`` is the tiled middle — (n_d2 - 1) whole periods, possibly
        empty — and ``period`` is ONE evaluated d2 period (trailing duplicate
        sample already trimmed). The start piece (last minimum → period end)
        and end piece (period start → first maximum) are sliced from
        ``period``: with a tiled middle these slices are byte-identical to
        slicing the middle itself (its tail/head ARE one period), and with an
        empty middle (single-d2-step scan) they compose the one full sweep —
        the period starts and ends at the scan center with v = v_scan, so
        pre3 + post1 is continuous.
        """
        # generate five pieces, three before and two after, to be concatenated to the given positions array
        min_idx = np.where(period == np.min(period))[0][-1]
        max_idx = np.where(period == np.max(period))[0][0]
        # initial smooth acceleration piece from 0
        pos_pre1 = self.__init_positioning(initpos=np.min(period), v_max=v_max, a_max=a_max)
        self._samples_initpos.append(len(pos_pre1))
        # initial settling time before first d2 step
        settlinglen = int(round(self.__settlingtime / self.__timestep))
        pos_pre2 = np.repeat(np.min(period), settlinglen)  # settling positions
        self._samples_settling = len(pos_pre2)
        pos_pre3 = period[min_idx:]  # first half scan curve
        # alt: last half scan curve to last peak after last d2 step
        pos_post1 = period[:max_idx]
        # final smooth acceleration piece from max to 0
        pos_post2 = self.__final_positioning(initpos=pos_post1[-1], v_max=v_max, a_max=a_max)
        pos_ret = np.concatenate((pos_pre1, pos_pre2, pos_pre3, pos, pos_post1, pos_post2))
        # half scan d2 step
        pos_halfscand2step = period[:np.argmin(abs(period[:max_idx] - pos_fix[2]))]
        self._samples_startacc = len(pos_pre3) - len(pos_halfscand2step)
        self._samples_finalpos.append(len(pos_post2))
        return pos_ret

    def __zero_padding(self, pos, padlen_base):
        """ Pad zeros to the beginning and end of all scanning curves. """
        pos_ret = []  # return array of padded axis signals
        n_axes = len(pos)  # number of axes
        padlens = [np.array(padlen_base) for i in range(n_axes)]  # basic padding length added to arrays for all axes
        # calculate padding lengths for all axes
        # get longest axis index
        pos_lens = [len(pos_i) for pos_i in pos]
        pos_max_len = np.argmax(pos_lens)
        # get difference in length between longest and all other axes
        pos_len_diffs = [pos_lens[pos_max_len] - pos_len for pos_len in pos_lens]
        # add length differences to padlens for each axis, if differences sum != 0
        if np.sum(pos_len_diffs) != 0:
            padlens = [padlen_i + np.array([0, pos_len_diffs[i]]) for i, padlen_i in enumerate(padlens)]
        # pad arrays
        for axis, pos_axis in enumerate(pos):
            pos_temp = np.pad(pos_axis, padlens[axis], 'constant', constant_values=0)
            pos_ret.append(pos_temp)
        return pos_ret, padlens[0][1]

    def __plot_curves(self, plot, signals):
        """ Plot all scan curves, for debugging. """
        if plot:
            plt.figure(1)
            plt.clf()
            for i, signal in enumerate(signals):
                while np.max(signal) > 1:
                    signal = np.divide(signal, 10)
                plt.plot(signal - 0.01 * i)
                target = self.axis_devs_order[i]
                #self._logger.debug(f'Signal length {target}: {len(signal)}')
            plt.show()

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
