# Workflows

Complete acquisition workflows for real microscopes. Unlike the tutorials,
these need the hardware they name -- see the README and the header of each
script, and edit the device names and output folders before running.

* `wfs/` -- WidefieldSTARSS and related widefield acquisitions
  (Kiralux camera, Teensy pulses, rotators).
* `smart/` -- calibrations for smart microscopy:
  `01_bead_calibration_camera_to_scan.py` maps camera pixels to the
  Advanced scan's centre coordinates from one camera snap and one APD scan
  of the same bead field (the `cam_to_scan` transform the ROI dF/F
  triggered-scan pipeline uses).
* `monalisa/` -- MoNaLISA timelapses recorded into memory and reconstructed
  as they come in;
  `01_bleach_compensated_timelapse.py` raises a laser's power when the
  reconstructed intensity drops from one block of timepoints to the next.

The time-resolved acquisitions (binned photon arrivals, gated STED, tau
STED) are tutorials now: `../tutorial/timetagger/11_*` to `13_*`, which run
on the simulated card and write into the Recording folder.

New to scripting? Start with `../tutorial/basic`.
