"""Acquire defocus scan with optional Z-order scrambling for PSF calibration.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Z positioner available
  - Camera configured for image acquisition

Output: One recording per Z plane under ~/ImSwitchMeasurements/.
"""
# ruff: noqa: F821
from imswitch.imcontrol.model.workflows import (
    DefocusScanParams,
    DefocusScanWorkflow,
    RecordingParams,
    RecordingWorkflow,
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

params = DefocusScanParams(
    n_z_planes=21,
    z_step_um=0.5,
    z_center_um=None,  # use current Z position as center
    scramble=False,
)

recording_params = RecordingParams(
    pin488=8,
    pin405=6,
    camerapin=11,
    start488=0,
    start405=25_000,
    start_camera=0,
    width488=20_000,
    width405=20_000,
    width_camera=50_000,
    dwelltime=50_000,
    delay_time=0,
    frame_number=20,
    move_waveplate=True,
    record_h=True,
    record_v=True,
    measurements_root=MEASUREMENTS_ROOT,
)

recording = RecordingWorkflow(facade, recording_params)
wf = DefocusScanWorkflow(facade, recording, params)
z_positions = wf.run()

print(f"Defocus scan complete: {len(z_positions)} planes acquired.")
print(f"Z positions: {z_positions}")
