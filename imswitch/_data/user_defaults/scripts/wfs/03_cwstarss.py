"""Run CW-STARSS photoselection experiment (488-only, 488+405, background).

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Lasers: 488 nm (EXC), 405 nm (ACT)
  - Camera configured for continuous streaming

Output: ~/ImSwitchMeasurements/<timestamp>/signal_bwd.csv
        ~/ImSwitchMeasurements/<timestamp>/combined_bwd.csv
        ~/ImSwitchMeasurements/<timestamp>/background_bwd.csv
"""
from imswitch.imcontrol.model.workflows import CWSTARSSWorkflow, CWSTARSSParams

facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

params = CWSTARSSParams(
    fps=20.0,
    duration_s=2.0,
    power_488_mw=50.0,
    power_405_mw=10.0,
    measurements_root=None,  # defaults to ~/ImSwitchMeasurements/
)

wf = CWSTARSSWorkflow(facade, params)
wf.run()

print(f"CWSTARSS complete: {params.duration_s}s phases, CSV files saved.")
