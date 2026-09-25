"""A diamond: split a stack, process each slice differently, merge them back.

This is the shape a linear "history" could never replay: two branches from
one split, each with its own settings, joined by a merge. The provenance
graph of the merged result carries both branches, and the workflow refers
to the split's slices by their ports (``split.C0``, ``split.C1``).

    python examples/improcess_workflows/split_process_merge_diamond.py [RECORDING.h5] [OUT_DIR]
"""

import sys
import tempfile
from pathlib import Path

from imswitch.improcess.model.provenance import graph_of
from imswitch.improcess.workflows import (
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
    run,
)


def build_workflow(recording) -> Workflow:
    return Workflow(
        "split-process-merge",
        [
            Source("raw", path=str(recording)),
            Reconstruct("rec", "view-only", inputs=["raw"]),
            Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
            Process("smooth", "filter", {"method": "gaussian", "radius": 2.0}, inputs=["split.C0"]),
            Process("sharp", "filter", {"method": "median", "radius": 1.0}, inputs=["split.C1"]),
            Process("merge", "channel-merge", inputs=["smooth", "sharp"]),
            Save("out", input="merge", fmt="hdf5", path_template="{out_dir}/{source_stem}_diamond{ext}"),
        ],
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        recording, out_dir = Path(argv[0]), Path(argv[1] if len(argv) > 1 else "results")
    else:
        root = Path(tempfile.mkdtemp(prefix="improcess_workflow_"))
        from _synthetic import write_synthetic_recording

        recording = write_synthetic_recording(root / "cells.h5", frames=2)
        out_dir = root / "results"

    workflow = build_workflow(recording)
    with run(workflow, registry=bootstrap_registry(), out_dir=out_dir) as report:
        merged = report.result("merge")
        graph = graph_of(merged)
        branches = [n for n in graph["nodes"].values() if n.get("plugin_id") == "filter"]
        print(f"merged shape {merged.data.shape}; graph has {len(graph['nodes'])} nodes, "
              f"{len(branches)} filter branches with params {[b['params'] for b in branches]}")
        for receipt in report.receipts:
            print("wrote", ", ".join(str(f) for f in receipt.files))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
