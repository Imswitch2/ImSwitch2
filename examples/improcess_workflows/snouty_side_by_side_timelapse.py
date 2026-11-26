"""A SNOUTY time-lapse filed as one 3D array, reconstructed two ways.

The timepoints' skewed stacks sit next to each other along X. A Python step
reshapes them into ``(T, Z, Y, X)``; SNOUTY deskews and MoNaLISA reconstructs
every timepoint -- once by auto-indexing (each plugin loops over T by
itself), once with an explicit ``map`` over T. Both give the same pixels.

    python examples/improcess_workflows/snouty_side_by_side_timelapse.py [LAPSE.h5] [OUT_DIR]
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

from imswitch.improcess.model.provenance import output_node
from imswitch.improcess.workflows import Workflow, bootstrap_registry, run, validate

HERE = Path(__file__).resolve().parent


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        lapse, out_dir = Path(argv[0]), Path(argv[1] if len(argv) > 1 else "results")
    else:
        root = Path(tempfile.mkdtemp(prefix="improcess_snouty_"))
        from _synthetic import write_side_by_side_snouty

        lapse, _ = write_side_by_side_snouty(root / "lapse.h5", timepoints=2)
        out_dir = root / "results"

    registry = bootstrap_registry()
    made = {}
    for name, result_step in (("snouty_side_by_side_timelapse", "mona"),
                              ("snouty_side_by_side_timelapse_map", "per_t")):
        workflow = Workflow.load(HERE / f"{name}.yaml")
        problems = validate(workflow, registry)
        if problems:
            for issue in problems:
                print(f"{name}: {issue.step}: {issue.message}")
            return 1
        with run(workflow, registry=registry, bindings={"raw": str(lapse)}, out_dir=out_dir) as report:
            result = report.result(result_step)
            made[name] = np.asarray(result.data)
            node = output_node(result)
            print(f"{name}: {result.axis_labels} {made[name].shape}, "
                  f"iterated over {node.get('iterated_over')}")
            for receipt in report.receipts:
                print(f"  wrote {receipt.primary}")
    same = np.allclose(*made.values())
    print("auto-indexed and mapped results agree" if same else "the two results DIFFER")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
