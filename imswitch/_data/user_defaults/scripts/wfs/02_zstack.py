"""Acquire a Z-stack using hardware-triggered or software acquisition.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Z positioner available (Z axis)
  - Teensy device available (if pulsed=True)
  - Laser: 488 nm (EXC)

Output: ~/ImSwitchMeasurements/<timestamp>_zstack.tiff
"""
from imswitch.imcontrol.model.workflows import ZStackWorkflow, ZStackParams

facade = api.workflowFacade.build(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

params = ZStackParams(
    n_planes=10,
    step_um=1.0,
    laser_pin=8,
    camera_pin=11,
    pulsed=True,
    laser_power_488_mw=50.0,
    exposure_us=50_000,
    measurements_root=None,  # defaults to ~/ImSwitchMeasurements/
)

wf = ZStackWorkflow(facade, params)
wf.run()

print(f"Z-stack complete: {params.n_planes} planes, {params.step_um} µm steps.")
