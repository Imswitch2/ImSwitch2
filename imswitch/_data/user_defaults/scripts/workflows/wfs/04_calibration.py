"""Calibrate rotators (HWP/QWP) with full angular sweep and image capture.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Rotators: HWP, QWP available
  - Laser: 488 nm (EXC)
  - Teensy device available (if pulsed=True)

Output: <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/polcal_<HHMMSS>.csv
"""
# ruff: noqa: F821
from imswitch.imcontrol.model.workflows import CalibrationWorkflow, CalibrationParams

MEASUREMENTS_ROOT = "D:/Measurements"  # Adapt to your preferred measurement folder.

facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

params = CalibrationParams(
    n_steps_qwp=10,
    n_steps_hwp=10,
    laser_power_488_mw=50.0,
    exposure_us=50_000,
    laser_pin=8,
    camera_pin=11,
    pulsed=True,
    measurements_root=MEASUREMENTS_ROOT,
)

wf = CalibrationWorkflow(facade, params)
wf.run()

print(f"Calibration complete: {params.n_steps_qwp}×{params.n_steps_hwp} angles.")
