"""Record polarised CWSTARSS image stacks with Teensy TTL pulse generation.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Teensy device available for pulse generation
  - Lasers: 488 nm (EXC), 405 nm (ACT)
  - Rotators: HWP, QWP configured with presets

Output: ~/ImSwitchMeasurements/<timestamp>_horizontal.tiff
        ~/ImSwitchMeasurements/<timestamp>_vertical.tiff
"""
from imswitch.imcontrol.model.workflows import RecordingWorkflow, RecordingParams

facade = api.workflowFacade.build(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

params = RecordingParams(
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
    measurements_root=None,  # defaults to ~/ImSwitchMeasurements/
)

wf = RecordingWorkflow(facade, params)
wf.run()

print("Recording complete. Check ~/ImSwitchMeasurements/ for TIFF files.")
