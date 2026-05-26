"""Acquire defocus scan with optional Z-order scrambling for PSF calibration.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - Z positioner available
  - Camera configured for image acquisition

Output: Returns array of (z_position_um, image_data) tuples.
        No automatic file saving — call workflow methods to access data.
"""
from imswitch.imcontrol.model.workflows import DefocusScanWorkflow, DefocusScanParams

facade = api.workflowFacade.build(
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

wf = DefocusScanWorkflow(facade, params)
results = wf.run()

print(f"Defocus scan complete: {len(results)} planes acquired.")
print(f"Z positions: {[z for z, _ in results]}")
