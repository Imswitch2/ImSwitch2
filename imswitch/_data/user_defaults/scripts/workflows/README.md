# Workflows

Complete acquisition workflows for real microscopes. Unlike the tutorials,
these need the hardware they name -- see the README and the header of each
script, and edit the device names and output folders before running.

* `wfs/` -- WidefieldSTARSS and related widefield acquisitions
  (Kiralux camera, Teensy pulses, rotators).

The time-resolved acquisitions (binned photon arrivals, gated STED, tau
STED) are tutorials now: `../tutorial/timetagger/11_*` to `13_*`, which run
on the simulated card and write into the Recording folder.

New to scripting? Start with `../tutorial/basic`.
