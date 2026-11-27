"""A timelapse of tilings reconstructed as one T stack on one canvas.

Writes a small synthetic timelapse (a 3 x 3 grid per timepoint over a
drifting, noisy scene) and runs ``tiling_timelapse_mosaic.yaml`` on it:

    python examples/improcess_workflows/tiling_timelapse_mosaic.py [TIMELAPSE.json] [OUT_DIR]
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

from imswitch.improcess.workflows import Workflow, bootstrap_registry, run, validate

HERE = Path(__file__).resolve().parent


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        manifest, out_dir = Path(argv[0]), Path(argv[1] if len(argv) > 1 else "results")
    else:
        root = Path(tempfile.mkdtemp(prefix="improcess_tiling_timelapse_"))
        from _synthetic import write_tiling_timelapse

        manifest = write_tiling_timelapse(root / "timelapse", timepoints=4)
        out_dir = root / "results"
    registry = bootstrap_registry()
    workflow = Workflow.load(HERE / "tiling_timelapse_mosaic.yaml")
    problems = validate(workflow, registry)
    if problems:
        for issue in problems:
            print(f"{issue.step}: {issue.message}")
        return 1
    with run(workflow, registry=registry, bindings={"raw": str(manifest)}, out_dir=out_dir) as report:
        result = report.result("mosaic")
        print(f"{result.axis_labels} {np.asarray(result.data).shape}, scales {result.axis_scales}")
        drift = np.round(result.drift_yx, 1).tolist() if result.drift_yx is not None else None
        print(f"drift correction per timepoint (row, col px): {drift}")
        for receipt in report.receipts:
            print(f"wrote {receipt.primary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
