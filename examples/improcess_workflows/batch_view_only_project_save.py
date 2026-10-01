"""Batch: every recording in a folder -> max projection -> Gaussian blur -> OME-TIFF.

    python examples/improcess_workflows/batch_view_only_project_save.py [INPUT_DIR] [OUT_DIR]

Without arguments it writes three synthetic recordings and processes them.
The same workflow as ``batch_view_only_project_save.yaml`` runs from the
command line with::

    python -m imswitch.improcess.workflows run \\
        examples/improcess_workflows/batch_view_only_project_save.yaml \\
        --input recordings/*.h5 --out results/
"""

import sys
import tempfile
from pathlib import Path

from imswitch.improcess.workflows import (
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
    bindings_for_inputs,
    bootstrap_registry,
    run_over,
)


def build_workflow() -> Workflow:
    return Workflow(
        "batch-project-blur",
        [
            Source("raw"),                                          # bound per input below
            Reconstruct("rec", "view-only", inputs=["raw"]),
            Process("proj", "projection", {"axis": "C", "mode": "max"}, inputs=["rec"]),
            Process("blur", "filter", {"method": "gaussian", "radius": 1.5}, inputs=["proj"]),
            Save("out", input="blur", fmt="tiff",
                 path_template="{out_dir}/{source_stem}_maxproj_blur{ext}"),
        ],
        description="max projection over the stack axis, then a Gaussian blur",
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        input_dir, out_dir = Path(argv[0]), Path(argv[1] if len(argv) > 1 else "results")
    else:
        root = Path(tempfile.mkdtemp(prefix="improcess_workflow_"))
        from _synthetic import write_synthetic_recording

        input_dir, out_dir = root / "recordings", root / "results"
        for index in range(3):
            write_synthetic_recording(input_dir / f"cell_{index}.h5", seed=index)
        print(f"synthetic recordings in {input_dir}")

    workflow = build_workflow()
    registry = bootstrap_registry()
    inputs = sorted(input_dir.glob("*.h5"))
    batch = run_over(workflow, bindings_for_inputs(workflow, inputs), registry=registry, out_dir=out_dir)
    for row in batch.rows:
        status = "ok  " if row.ok else "FAIL"
        print(f"[{status}] {row.bindings['raw']} -> {', '.join(row.files) or row.error}")
    summary = batch.write_summary(out_dir / "summary.csv")
    print(f"{len(batch.rows)} runs, {len(batch.failures)} failed; summary in {summary}")
    return 0 if batch.ok else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
