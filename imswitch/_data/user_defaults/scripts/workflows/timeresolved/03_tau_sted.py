"""Compute tau-STED / FLIM-STED lifetime products from a scan.

The scan and STED excitation/depletion timing come from the active scan setup.
The workflow configures the time-resolved detector fit and saves intensity,
lifetime, decay, and metadata products.
"""
# ruff: noqa: F821

from imswitch.imcontrol.model.workflows import (
    LifetimeFitConfig,
    TauSTEDParams,
    TauSTEDWorkflow,
)


MEASUREMENTS_ROOT = "D:/Measurements"  # Adapt to your preferred measurement folder.
TIME_RESOLVED_DETECTOR = "FLIM"        # Adapt to your setup detector name.


facade = api.imcontrol.buildWorkflowFacade(
    time_resolved_detector_name=TIME_RESOLVED_DETECTOR,
)

params = TauSTEDParams(
    fit=LifetimeFitConfig(
        method="moment",
        min_counts_per_pixel=20,
        laser_rep_rate_mhz=80.0,
    ),
    capture_cube=False,
    timeout_s=120.0,
    measurements_root=MEASUREMENTS_ROOT,
    save_h5=True,
    save_npz=False,
    save_tiff=True,
)

workflow = TauSTEDWorkflow(facade, params)
result = workflow.run()

print("Saved tau-STED products:")
for label, path in result.output_paths.items():
    print(f"  {label}: {path}")
