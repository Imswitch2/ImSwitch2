"""Camera -> scanner calibration from two bead recordings.

Finds the map from camera pixels (row, column) to the Advanced scan's
centre coordinates (um) that a smart pipeline needs to scan, with the APD,
the region a camera image points at (``roi_dff_triggered_apd_scan.json``:
its ``transform`` step names the calibration id, ``cam_to_scan``).

You need two recordings of the SAME sparse bead field, taken at the same
stage position and focus, saved by the Recording panel as HDF5 (default) or
Zarr -- the files as they are, no conversion:

  1. Camera: Recording panel -> File format HDF5 -> select the camera ->
     Snap (or record a few frames; they are averaged). Use the camera ROI
     and binning of the experiment: the calibration is only valid for them.
  2. APD: set the Advanced scan exactly as for the experiment (same scan
     axes, step, dwell and, above all, the same phase delay) over the beads;
     Recording panel -> select the APD -> "Scan once" -> Record.

Bead sample: sparse (beads a few um apart), random (not a grid), visible
in both channels, not saturated. The APD field may be a part of the camera
field. The result is valid for that camera ROI/binning and that scan
configuration only; the stage position is not modelled.

Edit the settings below and run this script (Scripting module), or run the
same from a terminal:
  python -m imswitch.imcontrol.model.bead_calibration CAMERA.h5 APD.hdf5 --id cam_to_scan

It prints a report, saves the QC figure and the report next to the record in
<ImSwitchConfig>/calibrations/transforms/ and the pipeline entry
<ImSwitchConfig>/calibrations/cam_to_scan.json. It REFUSES (and says why)
when there are too few beads, the bead pattern is regular (ambiguous), or
the residuals are larger than MAX_RESIDUAL_UM.

Check it before trusting it:
  * VERIFY_CAMERA_FILE / VERIFY_SCAN_FILE: a second bead pair (another
    field): reports how far camera beads land from the APD beads, no fit.
  * PREDICT_PIXEL: the scan centre (um) for one camera pixel -- put it in the
    Advanced scan panel's centre and scan by hand.
  * Then the pipeline in Validate mode: its "would scan ROI ..." lines.
"""
import os

from imswitch.imcommon.algorithms.transforms.bead_registration import BeadRegistrationOptions
from imswitch.imcontrol.model import bead_calibration as bc

# --- settings ---------------------------------------------------------------
CAMERA_FILE = os.path.expanduser('~/ImSwitchRecordings/beads_snap_Camera.h5')
SCAN_FILE = os.path.expanduser('~/ImSwitchRecordings/beads_rec_APD.hdf5')
CALIBRATION_ID = 'cam_to_scan'     # the id the pipeline's transform step names
CAMERA_DETECTOR = None             # name it when a file holds several detectors
SCAN_DETECTOR = None
MODEL = 'auto'                     # 'auto', 'similarity_reflect' or 'affine'
MAX_RESIDUAL_UM = 0.25
OVERWRITE = True

VERIFY_CAMERA_FILE = None          # a second bead pair, to check the saved calibration
VERIFY_SCAN_FILE = None
PREDICT_PIXEL = None               # e.g. (512, 640): camera (row, col)
# ----------------------------------------------------------------------------

result = bc.calibrate_from_files(
    CAMERA_FILE, SCAN_FILE, CALIBRATION_ID,
    camera_detector=CAMERA_DETECTOR, scan_detector=SCAN_DETECTOR,
    options=BeadRegistrationOptions(model=MODEL, max_residual=MAX_RESIDUAL_UM),
    overwrite=OVERWRITE,
)
print(result.report())

if VERIFY_CAMERA_FILE and VERIFY_SCAN_FILE:
    check = bc.verify_from_files(CALIBRATION_ID, VERIFY_CAMERA_FILE, VERIFY_SCAN_FILE)
    print(f"verification: {check['matched']} of {check['scan_beads']} APD beads have a camera bead "
          f"within 0.5 um; RMS {check['rms_um'] * 1000:.0f} nm, max {check['max_um'] * 1000:.0f} nm, "
          f"mean offset {check['mean_offset_um']} um; grid problems: {check['grid_problems'] or 'none'}")

if PREDICT_PIXEL is not None:
    centre = bc.predict_scan_centre(result.transform, *PREDICT_PIXEL)
    print(f"camera pixel {PREDICT_PIXEL} -> scan centre {centre} um")
