# Workflows

Complete acquisition workflows for real microscopes. Unlike the tutorials,
these need the hardware they name -- see the README and the header of each
script, and edit the device names and output folders before running.

* `wfs/` -- WidefieldSTARSS and related widefield acquisitions
  (Kiralux camera, Teensy pulses, rotators).
* `monalisa/` -- MoNaLISA timelapses recorded into memory and reconstructed
  as they come in;
  `01_bleach_compensated_timelapse.py` raises a laser's power when the
  reconstructed intensity drops from one block of timepoints to the next.

* `event_trigger/` -- smart acquisitions that look first and then decide:
  `01_spot_tiles_confocal.py` counts diffraction-limited spots in every
  tile of a widefield tiling overview and runs a confocal scan on the
  tiles that have spots, most spots first.

The time-resolved acquisitions (binned photon arrivals, gated STED, tau
STED) are tutorials now: `../tutorial/timetagger/11_*` to `13_*`, which run
on the simulated card and write into the Recording folder.

New to scripting? Start with `../tutorial/basic`.
