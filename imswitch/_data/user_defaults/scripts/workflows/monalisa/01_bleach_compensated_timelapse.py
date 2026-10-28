"""MoNaLISA timelapse with live reconstruction and bleaching compensation.

What it does
  Runs the scan the Scan widget holds once per timepoint, records it into
  memory -- no file is written -- and reconstructs that timepoint with the
  ImProcess MoNaLISA reconstructor straight from memory, showing the
  reconstruction in the image view as it comes in. The timepoints are
  grouped in blocks of BLOCK_SIZE; when a block's mean reconstructed
  intensity is significantly lower than the previous block's, the power of
  the activation laser ACT_LASER is raised by the factor that restores the
  previous block's level, through the calibrated power response (see
  "Calibration"). It never exceeds MAX_POWER_ACT. The excitation and
  off-switching lasers stay at EXC_POWER and OFF_POWER throughout.

  The illumination grid is localized once, on the first timepoint, and
  every later timepoint is reconstructed on that grid, so the intensities
  of all timepoints are measured on the same foci.

Calibration
  Reconstructed intensity is modelled as I = a * P_act**RESPONSE_EXPONENT.
  To restore I_previous from I_now the activation power is multiplied by
      (I_previous / I_now) ** (1 / RESPONSE_EXPONENT).
  Measure the exponent separately (reconstructed intensity at a few 405
  powers, slope of log I against log P) and set RESPONSE_EXPONENT. Near
  saturation of the activation it is well below 1, and more 405 adds little
  signal: once the switchable protein is used up, the power climbs to
  MAX_POWER_ACT without restoring the level -- the log then shows a run of
  "capped" decisions.

  "Significantly lower" is a one-sided test on the two block means: the
  drop must exceed SIGNIFICANCE_Z standard errors (from the scatter of the
  timepoints within each block) AND MIN_RELATIVE_DROP of the previous
  block's mean, so a drop that is real but too small to matter does not
  move the laser.

Output
  The image view: the latest reconstruction ("MoNaLISA live"), and at the
  end the whole timelapse as a stack ("MoNaLISA timelapse").
  In the recording folder:
    <NAME>_log.csv                timepoint, time, act_power, intensity, block decision
    <NAME>_raw/ (SAVE_RAW)        each timepoint's raw recording, HDF5
    <NAME>_timelapse.tif (SAVE_RECONSTRUCTIONS)   the reconstructed stack

Setup
  Your own microscope: a MoNaLISA Scan widget whose TTL cycle triggers the
  camera, the camera, the Laser, Recording and Image widgets. Set up the
  scan in the Scan widget first -- the script runs whatever it shows.
  Change CAMERA, the laser names and the powers to yours (in the units the
  Laser widget shows).

  The script assumes two things of the setup:
  * The scan's TTL cycle switches the three lasers on and off: the script
    sets their powers but never switches them on.
  * The scan runs the lasers at their Live power (setLaserValue). With
    separate Live and Scan powers and a Scan power saved, or a laser whose
    power the scan's analog output drives, a power the script sets does
    not reach the acquisition. getLaserAcquisitionState(name) says which
    power a scan would use.

  On a mock setup the reconstructed intensity does not respond to the
  lasers, so it shows the wiring, not the compensation.
"""

import csv
import math
import os
import time

import numpy as np
import tifffile

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.reconstructors.monalisa.localizer import localizer
from imswitch.improcess.reconstructors.run import run_reconstruction
from imswitch.improcess.workflows import bootstrap_registry

# --- Your microscope ---------------------------------------------------------
CAMERA = 'Green'
EXC_LASER = '488 (EXC)'
EXC_POWER = 2.0              # laser units; fixed
OFF_LASER = '488 (OFF)'
OFF_POWER = 150              # laser units; fixed
ACT_LASER = '405 (ON)'
START_POWER_ACT = 2.0        # laser units; the activation power of the first block
MAX_POWER_ACT = 50.0         # laser units; never set higher than this

# --- The timelapse -----------------------------------------------------------
NAME = 'monalisa_bleach_comp'
TIMEPOINTS = 30
INTERVAL_S = 10.0            # from the start of one timepoint to the next
BLOCK_SIZE = 5               # timepoints compared as one block

# --- Compensation ------------------------------------------------------------
RESPONSE_EXPONENT = 1.0      # I ~ P_act**exponent; measured separately (see Calibration)
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
SAVE_RAW = False
SAVE_RECONSTRUCTIONS = False

log = getLogger()
recFolder = api.imcontrol.getRecFolder()
reconstructor = bootstrap_registry().get_reconstructor('monalisa')


def recordTimepoint():
    """One scan into memory; returns the camera's part of the result."""
    result = api.imcontrol.recordScanToMemory(CAMERA, timeout=600, structured=True)
    camera = result.detector(CAMERA)
    if not camera.complete:
        log.warning(f'{camera.framesRecorded} of {camera.framesExpected} frames '
                    f'({camera.outcome}): reconstructing what arrived')
    if SAVE_RAW:
        result.save(os.path.join(recFolder, f'{NAME}_raw'))
    return camera


def localizePattern(camera):
    """The illumination grid on the summed stack, as the reconstructor's params."""
    loc = localizer(np.asarray(camera.data, dtype=np.float32))
    return {'row_offset': float(loc.yo), 'col_offset': float(loc.xo),
            'row_period': float(loc.yp), 'col_period': float(loc.xp)}


def reconstruct(camera, pattern, name):
    """Reconstruct one timepoint from memory; returns the reconstructed image.

    The reconstructor reads the in-memory recording exactly as it reads the
    file a recording to disk writes: the frames and the scan geometry in its
    attributes.
    """
    with camera.openRecording() as recording:
        dataObj = DataObj(name, CAMERA, file=recording)
        params = {**type(reconstructor).default_params(), **RECON_PARAMS, **pattern}
        params = reconstructor.prepare_params(dataObj, params)
        result = run_reconstruction(reconstructor, dataObj, params).result
    return np.squeeze(result.display_layer_data()[0])   # layer 0 = the reconstructed signal


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


power = START_POWER_ACT
pattern = dict(PATTERN) if PATTERN else None
rows, images = [], []
try:
    api.imcontrol.setLaserValue(ACT_LASER, power)
    api.imcontrol.setLaserValue(EXC_LASER, EXC_POWER)
    api.imcontrol.setLaserValue(OFF_LASER, OFF_POWER)

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
                        f'scan + reconstruction take longer than INTERVAL_S')
        tStart = time.monotonic() - t0
        camera = recordTimepoint()
        if pattern is None:
            pattern = localizePattern(camera)
            print('grid:', {k: round(v, 2) for k, v in pattern.items()})
        image = reconstruct(camera, pattern, f'{NAME}_t{t:03d}')
        images.append(image)
        show(image, 'MoNaLISA live')
        intensity = float(np.mean(image))
        intensities.append(intensity)
        row = {'timepoint': t, 'time_s': round(tStart, 2), 'act_power': power,
               'intensity': intensity, 'decision': ''}
        print(f't={t:3d}  {tStart:7.1f} s  405 power {power:.3g}  intensity {intensity:.4g}')

        # A block is complete: compare it with the one before.
        if (t + 1) % BLOCK_SIZE == 0 and t + 1 >= 2 * BLOCK_SIZE:
            current = intensities[-BLOCK_SIZE:]
            previous = intensities[-2 * BLOCK_SIZE:-BLOCK_SIZE]
            significant, change, ratio = compareBlocks(previous, current)
            if change is None:
                row['decision'] = 'no positive intensity to compare'
            elif significant:
                wanted = power * ratio ** (1.0 / RESPONSE_EXPONENT)
                newPower = min(wanted, MAX_POWER_ACT)
                row['decision'] = f'{change:+.1%}: power {power:.3g} -> {newPower:.3g}'
                if newPower < wanted:
                    row['decision'] += f' (capped, wanted {wanted:.3g})'
                    log.warning(f'power capped at MAX_POWER_ACT = {MAX_POWER_ACT}')
                power = newPower
                api.imcontrol.setLaserValue(ACT_LASER, power)
            else:
                row['decision'] = f'{change:+.1%}: not a significant drop'
            print('   block:', row['decision'])
        rows.append(row)
finally:
    # Lasers first: whatever went wrong, the sample should not stay lit. Each
    # laser on its own, so one that fails does not leave the others on.
    for laser in (ACT_LASER, EXC_LASER, OFF_LASER):
        try:
            api.imcontrol.setLaserValue(laser, 0)
            api.imcontrol.setLaserActive(laser, False)
        except Exception as error:
            log.error(f'could not switch {laser} off: {error}')

    if images and len({image.shape for image in images}) == 1:
        stack = np.stack(images)
        show(stack, 'MoNaLISA timelapse')
        if SAVE_RECONSTRUCTIONS:
            path = os.path.join(recFolder, f'{NAME}_timelapse.tif')
            tifffile.imwrite(path, stack.astype(np.float32), imagej=True,
                             metadata={'axes': 'TYX'})
            print('reconstructions:', path)
    if rows:
        logPath = os.path.join(recFolder, f'{NAME}_log.csv')
        os.makedirs(recFolder, exist_ok=True)
        with open(logPath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print('log:', logPath)
