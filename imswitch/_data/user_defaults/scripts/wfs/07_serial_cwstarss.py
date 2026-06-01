"""Run serial CWSTARSS power sweep on spiral grid positions.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - XY positioner available
  - Lasers: 488 nm (EXC), 405 nm (ACT)
  - Camera configured for continuous streaming

Output: CWSTARSS TIFF stacks under <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/
        for each power combination.
"""
# ruff: noqa: F821
from imswitch.imcontrol.model.workflows import (
    CWSTARSSWorkflow,
    CWSTARSSParams,
    SerialCWSTARSSWorkflow,
    SerialCWSTARSSParams,
)

MEASUREMENTS_ROOT = "D:/Measurements"  # Adapt to your preferred measurement folder.

facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

cwstarss_params = CWSTARSSParams(
    fps=20.0,
    duration_s=1.0,
    power_488_mw=0.0,  # will be overridden by serial sweep
    power_405_mw=0.0,  # will be overridden by serial sweep
)
cwstarss_wf = CWSTARSSWorkflow(facade, cwstarss_params)

serial_params = SerialCWSTARSSParams(
    powers_488_mw=[30.0, 50.0, 70.0],
    powers_405_mw=[5.0, 10.0],
    fps=20.0,
    duration_s=1.0,
    step_units=1560.0,
    measurements_root=MEASUREMENTS_ROOT,
)

serial_wf = SerialCWSTARSSWorkflow(facade, cwstarss_wf, serial_params)
serial_wf.run()

print(f"Serial CWSTARSS complete: {len(serial_params.powers_488_mw) * len(serial_params.powers_405_mw)} positions.")
