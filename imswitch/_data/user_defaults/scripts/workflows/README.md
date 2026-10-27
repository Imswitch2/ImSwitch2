# Workflows

Complete acquisition workflows for real microscopes. Unlike the tutorials,
these need the hardware they name -- see the README and the header of each
script, and edit the device names and output folders before running.

* `wfs/` -- WidefieldSTARSS and related widefield acquisitions
  (Kiralux camera, Teensy pulses, rotators).
* `timeresolved/` -- photon-arrival, gated-STED and tau-STED products from a
  time-resolved detector (e.g. Swabian TimeTagger).
* `monalisa/` -- MoNaLISA timelapses recorded into memory and reconstructed
  as they come in;
  `01_bleach_compensated_timelapse.py` raises a laser's power when the
  reconstructed intensity drops from one block of timepoints to the next.

New to scripting? Start with `../tutorial/basic`.
