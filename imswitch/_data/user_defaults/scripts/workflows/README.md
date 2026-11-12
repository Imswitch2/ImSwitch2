# Workflows

Complete acquisition workflows for real microscopes. Unlike the tutorials,
these need the hardware they name -- see the README and the header of each
script, and edit the device names and output folders before running.

* `wfs/` -- WidefieldSTARSS and related widefield acquisitions
  (Kiralux camera, Teensy pulses, rotators).
* `timeresolved/` -- photon-arrival, gated-STED and tau-STED products from a
  time-resolved detector (e.g. Swabian TimeTagger).
* `event_trigger/` -- smart acquisitions that look first and then decide:
  `01_spot_tiles_confocal.py` counts diffraction-limited spots in every
  tile of a widefield tiling overview and runs a confocal scan on the
  tiles that have spots, most spots first.

New to scripting? Start with `../tutorial/basic`.
