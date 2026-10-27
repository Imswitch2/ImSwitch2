"""MoNaLISA timelapse with live reconstruction and bleaching compensation.

What it does
  Records a MoNaLISA timelapse, one Scan-once HDF5 recording per timepoint,
  and reconstructs each timepoint with the ImProcess MoNaLISA reconstructor
  as soon as its file is written. The timepoints are grouped in blocks of
  BLOCK_SIZE; when a block's mean reconstructed intensity is significantly
  lower than the previous block's, the power of LASER is raised by the
  factor that restores the previous block's level, through the calibrated
  power response (see "Calibration"). The power never exceeds MAX_POWER.

  The illumination grid is localized once, on the first timepoint, and
  every later timepoint is reconstructed on that grid, so the intensities
  of all timepoints are measured on the same foci.

Calibration
  Reconstructed intensity is modelled as I = a * P**RESPONSE_EXPONENT. To
  restore I_previous from I_now the power is multiplied by
      (I_previous / I_now) ** (1 / RESPONSE_EXPONENT).
  RESPONSE_EXPONENT = 1 is fluorescence excited below saturation (emission
  proportional to excitation). For an activation laser (405 nm) or near
  saturation it is not 1: list powers in CALIBRATION_POWERS and the script
  first scans once at each, reconstructs, and fits the exponent from the
  log-log slope. Those scans bleach the sample too -- calibrate on a spot
  next to the one you image, or on a sacrificial sample, and copy the fitted
  exponent printed here into RESPONSE_EXPONENT for the real run.

  "Significantly lower" is a one-sided test on the two block means: the
  drop must exceed SIGNIFICANCE_Z standard errors (from the scatter of the
  timepoints within each block) AND MIN_RELATIVE_DROP of the previous
  block's mean, so a drop that is real but too small to matter does not
  move the laser.

Output, in the recording folder
  <NAME>_t000_rec_<CAMERA>.hdf5, ..._t001_...   one raw recording per timepoint
  <NAME>_recon/                                 the reconstructions (SAVE_RECONSTRUCTIONS)
  <NAME>_log.csv                                timepoint, time, power, intensity, block decision

Setup
  Your own microscope: a MoNaLISA Scan widget, a camera, the Laser and
  Recording widgets. Set up the scan in the Scan widget first -- the script
  records with whatever it shows. Change CAMERA, LASER and the powers to
  yours (in the units the Laser widget shows).
  Mock setup: mock_scan_monalisa_live.json has no laser; on a mock the
  reconstructed intensity does not respond to the laser, so it shows the
  wiring, not the compensation.
"""

import csv
import glob
import math
import os
import time

import h5py
import numpy as np

from imswitch.imcommon.framework import FrameworkUtils
from imswitch.improcess.reconstructors.monalisa.localizer import localizer
from imswitch.improcess.workflows import Reconstruct, Save, Source, Workflow, bootstrap_registry, run

# --- Your microscope ---------------------------------------------------------
CAMERA = 'Camera'
LASER = '488 (EXC)'
START_POWER = 2.0            # laser units; the power of the first block
MAX_POWER = 8.0              # laser units; never set higher than this

# --- The timelapse -----------------------------------------------------------
NAME = 'monalisa_bleach_comp'
TIMEPOINTS = 30
INTERVAL_S = 10.0            # from the start of one timepoint to the next
BLOCK_SIZE = 5               # timepoints compared as one block

# --- Compensation ------------------------------------------------------------
RESPONSE_EXPONENT = 1.0      # I ~ P**exponent; 1 = linear excitation (see Calibration)
CALIBRATION_POWERS = []      # e.g. [1.0, 2.0, 4.0] to fit the exponent first
SIGNIFICANCE_Z = 2.0         # one-sided, ~2.3 % false-alarm rate per block
MIN_RELATIVE_DROP = 0.05     # ignore drops below 5 % of the previous block

# --- Reconstruction ----------------------------------------------------------
RECON_PARAMS = {
    'reconstruction_method': 'Fast Gauss MoNaLISA',
    'device': 'CPU',
    'pixel_size_nm': 77,
    'psf_fwhm_nm': 220,
    'bg_modelling': 'Constant',   # background fitted out: intensity = signal only
}
PATTERN = None               # {'row_offset':..,'col_offset':..,'row_period':..,'col_period':..}
                             # or None: localize on the first timepoint
SAVE_RECONSTRUCTIONS = True

log = getLogger()
signals = api.imcontrol.signals()
recFolder = api.imcontrol.getRecFolder()
registry = bootstrap_registry()


def recordTimepoint(fileName):
    """One Scan-once recording; returns the path of the file it wrote."""
    api.imcontrol.setRecFilename(fileName)
    # A recording that fails emits recordingFailed and never recordingEnded;
    # one refused before it starts (a camera the scan does not trigger, say)
    # emits neither and is simply not recording. Watch for all three, so a
    # failure stops the script at once instead of at the timeout. The
    # handlers run on this script's thread, so the wait must deliver its
    # events (getWaitForSignal does the same).
    outcome = {}
    onEnded = lambda *a: outcome.setdefault('ended', True)
    onFailed = lambda *a: outcome.setdefault('failed', a)
    signals.recordingEnded.connect(onEnded)
    signals.recordingFailed.connect(onFailed)
    try:
        api.imcontrol.startRecording()
        if not outcome and not api.imcontrol.isRecording():
            outcome['failed'] = ('refused before it started',)
        waitUntil(lambda: FrameworkUtils.processPendingEventsCurrThread() or outcome,
                  timeout=600)
    finally:
        signals.recordingEnded.disconnect(onEnded)
        signals.recordingFailed.disconnect(onFailed)
    if 'failed' in outcome:
        raise RuntimeError(f'recording {fileName} failed: {outcome["failed"]} '
                           '(see the log; is the Scan widget set up to trigger the camera?)')
    paths = glob.glob(os.path.join(recFolder, f'{fileName}_rec_{CAMERA}*'))
    if not paths:
        raise RuntimeError(f'no recording of {CAMERA} named {fileName} in {recFolder}')
    return max(paths, key=os.path.getmtime)


def localizePattern(path):
    """The illumination grid on the summed stack, as the reconstructor's params."""
    with h5py.File(path, 'r') as f:
        frames = np.asarray(f[f'{CAMERA}/data'], dtype=np.float32)
    loc = localizer(frames)
    return {'row_offset': float(loc.yo), 'col_offset': float(loc.xo),
            'row_period': float(loc.yp), 'col_period': float(loc.xp)}


def reconstruct(path, pattern, save):
    """Reconstruct one recording; returns the mean of the reconstructed image."""
    steps = [Source('raw', path=path),
             Reconstruct('rec', 'monalisa', {**RECON_PARAMS, **pattern}, inputs=['raw'])]
    if save:
        steps.append(Save('save', input='rec', fmt='hdf5'))
    outDir = os.path.join(recFolder, f'{NAME}_recon') if save else None
    with run(Workflow(NAME, steps), registry=registry, out_dir=outDir) as report:
        layers = report.result('rec').display_layer_data()
        return float(np.mean(layers[0]))   # layer 0 = the reconstructed signal


def fitExponent(powers, intensities):
    """Slope of log I against log P."""
    slope, _ = np.polyfit(np.log(powers), np.log(intensities), 1)
    return float(slope)


def compareBlocks(previous, current):
    """(significant, relative change, previous/current) of two blocks' means.

    A block whose mean is not positive has no level to restore: nothing
    reconstructed (no sample, or noise around the fitted background).
    """
    mp, mc = float(np.mean(previous)), float(np.mean(current))
    if mp <= 0 or mc <= 0:
        return False, None, None
    se = math.sqrt(np.var(previous, ddof=1) / len(previous)
                   + np.var(current, ddof=1) / len(current))
    drop = mp - mc
    significant = drop > SIGNIFICANCE_Z * se and drop > MIN_RELATIVE_DROP * mp
    return significant, -drop / mp, mp / mc


previousFormat = api.imcontrol.getRecFileFormat()
api.imcontrol.setRecFileFormat('HDF5')
api.imcontrol.setDetectorToRecord(CAMERA)
api.imcontrol.setRecModeScanOnce()
power = START_POWER
pattern = dict(PATTERN) if PATTERN else None
rows = []
try:
    api.imcontrol.setLaserValue(LASER, power)
    api.imcontrol.setLaserActive(LASER, True)

    exponent = RESPONSE_EXPONENT
    if CALIBRATION_POWERS:
        calIntensities = []
        for i, calPower in enumerate(CALIBRATION_POWERS):
            api.imcontrol.setLaserValue(LASER, calPower)
            path = recordTimepoint(f'{NAME}_cal{i:02d}')
            if pattern is None:
                pattern = localizePattern(path)
            calIntensities.append(reconstruct(path, pattern, save=False))
            print(f'calibration: power {calPower} -> intensity {calIntensities[-1]:.4g}')
        exponent = fitExponent(CALIBRATION_POWERS, calIntensities)
        print(f'fitted RESPONSE_EXPONENT = {exponent:.3f}')
        api.imcontrol.setLaserValue(LASER, power)

    intensities = []
    t0 = time.monotonic()
    for t in range(TIMEPOINTS):
        # Wait for this timepoint's slot; a reconstruction that ran past it
        # delays the timepoint instead of skipping it.
        lag = time.monotonic() - (t0 + t * INTERVAL_S)
        if lag < 0:
            sleep(-lag)
        elif t > 0 and lag > 1:
            log.warning(f'timepoint {t} starts {lag:.1f} s late: '
                        f'recording + reconstruction take longer than INTERVAL_S')
        tStart = time.monotonic() - t0
        path = recordTimepoint(f'{NAME}_t{t:03d}')
        if pattern is None:
            pattern = localizePattern(path)
            print('grid:', {k: round(v, 2) for k, v in pattern.items()})
        intensity = reconstruct(path, pattern, save=SAVE_RECONSTRUCTIONS)
        intensities.append(intensity)
        row = {'timepoint': t, 'time_s': round(tStart, 2), 'power': power,
               'intensity': intensity, 'decision': ''}
        print(f't={t:3d}  {tStart:7.1f} s  power {power:.3g}  intensity {intensity:.4g}')

        # A block is complete: compare it with the one before.
        if (t + 1) % BLOCK_SIZE == 0 and t + 1 >= 2 * BLOCK_SIZE:
            current = intensities[-BLOCK_SIZE:]
            previous = intensities[-2 * BLOCK_SIZE:-BLOCK_SIZE]
            significant, change, ratio = compareBlocks(previous, current)
            if change is None:
                row['decision'] = 'no positive intensity to compare'
            elif significant:
                wanted = power * ratio ** (1.0 / exponent)
                newPower = min(wanted, MAX_POWER)
                row['decision'] = f'{change:+.1%}: power {power:.3g} -> {newPower:.3g}'
                if newPower < wanted:
                    row['decision'] += f' (capped, wanted {wanted:.3g})'
                    log.warning(f'power capped at MAX_POWER = {MAX_POWER}')
                power = newPower
                api.imcontrol.setLaserValue(LASER, power)
            else:
                row['decision'] = f'{change:+.1%}: not a significant drop'
            print('   block:', row['decision'])
        rows.append(row)
finally:
    # Lasers first: whatever went wrong, the sample should not stay lit.
    api.imcontrol.setLaserValue(LASER, 0)
    api.imcontrol.setLaserActive(LASER, False)
    api.imcontrol.setRecFilename(None)
    api.imcontrol.setDetectorToRecord(-1)
    api.imcontrol.setRecFileFormat(previousFormat)
    if rows:
        logPath = os.path.join(recFolder, f'{NAME}_log.csv')
        with open(logPath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print('log:', logPath)
