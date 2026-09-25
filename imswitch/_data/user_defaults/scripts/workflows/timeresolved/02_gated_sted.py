"""Compute gated-STED images from photon arrival windows.

The STED laser timing and scan geometry come from the currently configured
scan. This workflow only controls detection-side software gates.
"""
# ruff: noqa: F821

from imswitch.imcontrol.model.workflows import (
    GateSpec,
    GatedSTEDParams,
    GatedSTEDWorkflow,
)


MEASUREMENTS_ROOT = "D:/Measurements"  # Adapt to your preferred measurement folder.
TIME_RESOLVED_DETECTOR = "FLIM"        # Adapt to your setup detector name.


facade = api.imcontrol.buildWorkflowFacade(
    time_resolved_detector_name=TIME_RESOLVED_DETECTOR,
)

params = GatedSTEDParams(
    gates=(
        GateSpec("early", 0.5, 2.5),
        GateSpec("late", 2.5, 8.0),
    ),
    capture_cube=False,
    timeout_s=120.0,
    measurements_root=MEASUREMENTS_ROOT,
    save_h5=True,
    save_npz=False,
    save_tiff=True,
)

workflow = GatedSTEDWorkflow(facade, params)
result = workflow.run()

print("Saved gated-STED products:")
for label, path in result.output_paths.items():
    print(f"  {label}: {path}")
