# ImSwitch scripting tutorials

Short scripts that build on each other. Each one runs on a *mock setup*, a
hardware configuration made of simulated devices that ships with ImSwitch, so
you can try them without a microscope. The header of every script says what
it teaches, which mock setup to load, and what a real setup needs for it.

To run one: in the Hardware Control tab choose **Tools > Pick hardware
setup…** and pick the mock setup named in the script (ImSwitch restarts).
Then open the script in the Scripting tab and press **Run**. **Stop** ends a
script at any time.

## basic

| Step | Script | Mock setup | You learn |
|---|---|---|---|
| 01 | `01_hello_imswitch.py` | `example_mock.json` | what a script can reach; `sleep()` vs `time.sleep()` |
| 02 | `02_snap_an_image.py` | `example_mock.json` | snap into numpy or to a file (HDF5, TIFF) |
| 03 | `03_camera_settings.py` | `example_mock.json` | exposure, ROI, live view; restoring settings |
| 04 | `04_record_frames.py` | `example_mock.json` | record N frames, wait for the end, read the file |
| 05 | `05_two_cameras.py` | `example_no_hardware.json` | one camera after the other, then both together |
| 06 | `06_move_the_stage.py` | `example_no_hardware.json` | move stages, wait for them, snap a grid |
| 07 | `07_record_until_stop_safely.py` | `example_mock.json` | until-stop recording; clean-up that survives Stop |
| 08 | `08_share_code_between_scripts.py` | `example_mock.json` | `importScript()` (uses `tutorial_helpers.py`) |

## scanning

| Step | Script | Mock setup | You learn |
|---|---|---|---|
| 01 | `01_run_a_scan.py` | `hamamatsu_mock_scan_setup.json` | a scan that triggers a camera; scan files |
| 02 | `02_record_a_scan.py` | `hamamatsu_mock_scan_setup.json` | record one frame per scan position |
| 03 | `03_camera_and_apd.py` | `mixed_hamamatsu_apd_mock_scan_setup.json` | a camera and an APD in one scan |
| 04 | `04_scan_timelapse.py` | `hamamatsu_mock_scan_setup.json` | repeat a scan at intervals |
| 05 | `05_laser_power_series.py` | `galvo_apd_mock_scan_setup.json` | lasers; several scans in one recording |

The scanning tutorials load their scan settings from `scanning/scan_params/`.
Make your own in the Scan widget and save them with
`api.imcontrol.saveScanParamsToFile(path)`.
