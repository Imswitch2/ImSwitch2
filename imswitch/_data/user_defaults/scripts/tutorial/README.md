# ImSwitch scripting tutorials

Short scripts that build on each other, from snapping a single image to a
laser power series during a scan. You don't need a microscope for them:
each one runs on a simulated setup.

## Simulated setups and how to choose one

ImSwitch learns what hardware it controls from a **setup file**: a JSON
file that lists the cameras, stages, lasers and so on, and which panels
(widgets) to show for them. Besides the setup files for real microscopes,
ImSwitch ships some whose devices are all *simulated* ("mock" devices).
They behave like the real ones -- a camera returns images, a stage reports
where it moved to, a scan sends trigger pulses -- but nothing is connected.

The tutorials use several of these simulated setups, because they need
different hardware: the first ones one camera, later ones two cameras and
stages, the scanning ones a scanner that triggers a camera, a point
detector and lasers. The header of every script names its setup file on
the line `Mock setup:` and says what that setup simulates on the line
`It simulates:`.

To choose a setup file:

* **The first time ImSwitch starts**, it asks which setup to use. Pick the
  one named in the tutorial you want to run.
* **Afterwards**, go to the Hardware Control tab and choose
  **Hardware > Pick hardware setup…**, pick the file and confirm "The software
  will restart". ImSwitch restarts with the new setup.

The tutorials are ordered so you only switch when a new block begins:

| Tutorials | Setup file | It simulates |
|---|---|---|
| basic 01-06 | `example_mock.json` | one camera |
| basic 07-08 | `example_no_hardware.json` | two cameras and three stages |
| scanning 01-03 | `hamamatsu_mock_scan_setup.json` | a camera triggered by a scan |
| scanning 04 | `mixed_hamamatsu_apd_mock_scan_setup.json` | the same, plus a point detector (APD) |
| scanning 05 | `galvo_apd_mock_scan_setup.json` | a point-scanning microscope with two lasers |
| timetagger 01-10 | `galvo_flim_mock_scan_setup.json` | the same, plus a simulated Swabian Time Tagger with a FLIM detector |

## Running a tutorial

1. Load the setup file named in the script's header (see above).
2. Open the Scripting tab and double-click the script in the **Files**
   panel on the left. It opens in the editor.
3. **Read it before you run it.** The header says what you will learn; the
   comments explain every step. Try to predict what the script will do and
   what it will print.
4. Press **Run all** above the editor. What the script prints appears in
   the **Output** panel below the editor -- compare it with your guess.
5. **Stop** ends a script at any time. The tutorials clean up after
   themselves (settings, lasers, recordings), so the microscope is left as
   it was.

Then change something -- the number of frames, an exposure time, a scan
size -- and run it again. These files are your own copies: edit them
freely. When you update ImSwitch, the tutorials you have *not* edited are
updated too; the ones you edited are kept as they are. To get an original
back, delete your copy and restart ImSwitch.

## basic

| Step | Script | Setup file | You learn |
|---|---|---|---|
| 01 | `01_hello_imswitch.py` | `example_mock.json` | what a script can reach; `print()` and the logger; `sleep()` |
| 02 | `02_snap_an_image.py` | `example_mock.json` | snap into numpy or to a file (HDF5, TIFF) |
| 03 | `03_camera_settings.py` | `example_mock.json` | exposure, ROI, live view; restoring settings |
| 04 | `04_record_frames.py` | `example_mock.json` | record N frames, wait for the end, read the file |
| 05 | `05_record_until_stop_safely.py` | `example_mock.json` | until-stop recording; clean-up that survives Stop |
| 06 | `06_share_code_between_scripts.py` | `example_mock.json` | `importScript()` (uses `tutorial_helpers.py`) |
| 07 | `07_two_cameras.py` | `example_no_hardware.json` | one camera after the other, then both together |
| 08 | `08_move_the_stage.py` | `example_no_hardware.json` | move stages, wait for them, snap a grid |

## scanning

| Step | Script | Setup file | You learn |
|---|---|---|---|
| 01 | `01_run_a_scan.py` | `hamamatsu_mock_scan_setup.json` | a scan that triggers a camera; scan settings files |
| 02 | `02_record_a_scan.py` | `hamamatsu_mock_scan_setup.json` | record one frame per scan position |
| 03 | `03_scan_timelapse.py` | `hamamatsu_mock_scan_setup.json` | repeat a scan at intervals |
| 04 | `04_camera_and_apd.py` | `mixed_hamamatsu_apd_mock_scan_setup.json` | a camera and an APD in one scan |
| 05 | `05_laser_power_series.py` | `galvo_apd_mock_scan_setup.json` | lasers; several scans in one recording |

## timetagger

| Step | Script | Setup file | You learn |
|---|---|---|---|
| 01 | `01_meet_the_card.py` | `galvo_flim_mock_scan_setup.json` | the card's roles and channels; `count_rates()`; the built-in test signal |
| 02 | `02_trigger_levels_and_dead_time.py` | `galvo_flim_mock_scan_setup.json` | `trigger_sweep()` and the plateau; 50 Ω inputs; dead time; the APPLY pattern |
| 03 | `03_dark_counts_and_afterpulsing.py` | `galvo_flim_mock_scan_setup.json` | `dark_rates()` with the excitation blocked; the detector's `background_rate_hz` |
| 04 | `04_laser_sync.py` | `galvo_flim_mock_scan_setup.json` | `rep_rate()`: the sync rate measured safely, period jitter and the card's floor |
| 05 | `05_bandwidth_and_the_filter.py` | `galvo_flim_mock_scan_setup.json` | the tag budget, `overflows()`, the conditional filter and the TCSPC direction |
| 06 | `06_irf_and_t0.py` | `galvo_flim_mock_scan_setup.json` | `histogram()`: the IRF, its peak and width; the detector's `t0_ps` |
| 10 | `10_flim_preflight.py` | `galvo_flim_mock_scan_setup.json` | `preflight()`: the green/red checklist before a FLIM session |

Steps 07 to 09 (the line clock, the frame clock and pixel markers, and the
line delay, all measured during a scan) come with the next release.
`timetagger/timetagger_helpers.py` finds the card and the FLIM detector and
prints tables for 02-10. Every measurement that could change a setting
prints a suggestion and writes it only when the script's `APPLY` is True;
a written value holds until restart, the printed JSON line goes into the
`timeTagger` block of the setup file to keep it.

The scanning tutorials load their scan settings from `scanning/scan_params/`
(double-click one in the Files panel to read it). The camera and stage names
are set at the top of each script and put into the settings when they are
loaded, so to try a tutorial on another setup you change those names, not
the file. To make your own settings, set up a scan in the Scan widget and
save it with `api.imcontrol.saveScanParamsToFile(path)`.
`scanning/scan_helpers.py` holds the steps of scanning 01 as functions for
02-04.

## On your own microscope

Each header also has a line `Your own microscope:` that says what a real
setup must have for the script to work -- for example "a camera and the
Recording widget". Device names differ from setup to setup (the camera may
not be called "Camera"), so check the names the script uses against
`api.imcontrol.getDetectorNames()`, `getPositionerNames()` and
`getLaserNames()` on your setup.
