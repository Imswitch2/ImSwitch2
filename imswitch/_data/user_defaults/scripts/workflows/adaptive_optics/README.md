# Adaptive optics workflows

Adaptive optics with a deformable mirror and a Shack-Hartmann sensor, ported
from the polarization rig's Microscope_GUI (its AO panel). The mirror is a
`deformableMirrors` entry of the setup (an ALPAO through `AlpaoDMManager`);
the sensor is an ordinary camera (the rig's TIS camera) behind a lenslet
array. The mock setup `adaptive_optics_mock_setup.json` simulates both: a
mock DM69 and a mock TIS camera showing the spots of a simulated sensor
behind it, with a fixed aberration to correct.

Run them in order the first time; later, 03 alone when the calibration
still holds. Everything they write goes to
`<ImSwitchConfig>/adaptive_optics/<mirror>/`.

* `01_wavefront_sensor.py` -- finds the lenslet grid in a frame (or takes
  the old GUI's typed-in grid), takes the reference, saves the sensor, and
  prints the wavefront in Zernike µm RMS (Noll, the bead-PSF fit's basis).
* `02_mirror_calibration.py` -- pushes every actuator up and down and saves
  the influence matrix, and a mode matrix (the command for 1 µm RMS of each
  Zernike mode) that the mirror's `modesFile` can take, so the Deformable
  Mirror panel and `setMirrorModes` speak µm RMS.
* `03_closed_loop.py` -- measure, correct, repeat, until the sensor reads
  the reference; optionally keeps the result as the mirror's flat.
* `04_sensorless_modes.py` -- the old GUI's "Run AO": scans each mode's
  amplitude on the imaging camera, keeps the one with the sharpest image.

The mirror alone: the **Deformable Mirror** panel, and `flattenMirror`,
`setMirrorModes`, `pokeMirrorActuator`, `setMirrorCommand` and friends in
any script (see the scripting API reference).
