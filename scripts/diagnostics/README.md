# Hardware Diagnostic Scripts

This directory contains one-off hardware diagnostic and exploration scripts that require physical hardware to run and are not part of the automated test suite.

## Scripts

- **`ai_nidaq_tests.py`** — NI-DAQ analog input diagnostic. Continuously reads voltage from Dev1/ai1. Requires NI-DAQ hardware.

- **`test-galvoscandesigner.py`** — Galvo scan signal generation test. Visualizes scan patterns and TTL sequences for various axis configurations. Used for manual verification of scan designer output.

- **`test_standa_motrot.py`** — Standa motor/rotation stage serial communication test. Queries device serial number via ASRL8::INSTR. Requires Standa hardware.

- **`measure_laser_rep_rate.py`** — Measures laser repetition rate on a Swabian Time Tagger channel using `TimeTagger.Countrate`. Output feeds the `laser_rep_rate_mhz` parameter used by the FLIM phasor fit.

## Why Not in `_test/`?

These scripts are not proper unit/integration tests because:
1. They require specific physical hardware
2. They have no assertions or test framework integration
3. They run indefinitely or require manual inspection
4. They were written for one-off debugging/exploration

For automated tests that can run in CI, see `imswitch/imcontrol/_test/`.
