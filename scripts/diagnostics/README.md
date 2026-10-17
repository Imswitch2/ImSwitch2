# Hardware diagnostic scripts

One-off probes that need physical hardware. They are not part of the automated
test suite, have no assertions, and are run by hand on the rig, in the
ImSwitch2 environment, from the repository root.

| Script | Purpose |
|---|---|
| `ai_nidaq_tests.py` | NI-DAQ analog input check: continuously reads voltage from `Dev1/ai1`. |
| `bsc203_diag.py` | Talks to a Thorlabs BSC203 directly (no ImSwitch) to dump status, home, move, and measure true steps/mm and direction (`status`, `monitor`, `home`, `calibrate`, `moveabs`). |
| `diagnose_tis_camera.py` | Checks The Imaging Source camera at system level (driver, USB, process lock) to tell hardware problems from ImSwitch problems. |
| `ic4_cam.py` | Standalone IC4 probe: verifies one distinct frame per software or hardware trigger via a `QueueSink`. |
| `measure_laser_rep_rate.py` | Measures the laser repetition rate on a Swabian Time Tagger channel; the result feeds `laser_rep_rate_mhz` for the FLIM phasor fit. |
| `repro-single-axis-scan.py` | Demonstrator that single-axis and degenerate galvo/piezo scan designs generate (historical crash repro). |
| `test_standa_motrot.py` | Standa motor/rotation stage serial test: queries the device serial number via `ASRL8::INSTR`. |

For automated tests that run in CI see `imswitch/imcontrol/_test/`. Operator
tools live in `scripts/utilities/`; developer generators in `tools/`.
