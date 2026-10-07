"""Record binned photon arrival times per scan pixel.

Prerequisites:
  - A configured ScanWidget scan.
  - A time-resolved detector manager implementing the generic contract.
    For the Swabian manager, this is usually the FLIM detector entry.

Output:
  <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/photon_arrivals_<HHMMSS>.h5
  plus preview TIFFs when save_tiff=True.
"""
# ruff: noqa: F821

from imswitch.imcontrol.model.workflows import (
    BinnedPhotonArrivalParams,
    BinnedPhotonArrivalWorkflow,
    GateSpec,
)


MEASUREMENTS_ROOT = "D:/Measurements"  # Adapt to your preferred measurement folder.
TIME_RESOLVED_DETECTOR = "FLIM"        # Adapt to your setup detector name.


facade = api.imcontrol.buildWorkflowFacade(
    time_resolved_detector_name=TIME_RESOLVED_DETECTOR,
)

params = BinnedPhotonArrivalParams(
    gates=(
        GateSpec("early", 0.5, 2.5),
        GateSpec("late", 2.5, 8.0),
    ),
    timeout_s=120.0,
    measurements_root=MEASUREMENTS_ROOT,
    save_h5=True,
    save_npz=False,
    save_tiff=True,
)

workflow = BinnedPhotonArrivalWorkflow(facade, params)
result = workflow.run()

print("Saved binned photon-arrival products:")
for label, path in result.output_paths.items():
    print(f"  {label}: {path}")
