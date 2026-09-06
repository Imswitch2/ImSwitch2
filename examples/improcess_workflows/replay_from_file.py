"""Replay: from a file ImProcess wrote back to the workflow that made it.

    python examples/improcess_workflows/replay_from_file.py [RESULT_FILE] [OUT_DIR]

Without arguments it first runs the diamond example on a synthetic
recording, then replays the file it wrote, checks the pixels match, and
prints the recovered workflow as YAML. This is the Fiji-macro-recorder
idea without a recorder: the record is in the file.
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

from imswitch.improcess.workflows import bootstrap_registry, run, workflow_from_file


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    registry = bootstrap_registry()
    original = None
    if argv:
        result_file, out_dir = Path(argv[0]), Path(argv[1] if len(argv) > 1 else "replayed")
    else:
        from _synthetic import write_synthetic_recording
        from split_process_merge_diamond import build_workflow

        root = Path(tempfile.mkdtemp(prefix="improcess_replay_"))
        recording = write_synthetic_recording(root / "cells.h5", frames=2)
        with run(build_workflow(recording), registry=registry, out_dir=root / "first") as report:
            result_file = report.receipts[0].primary
            original = np.asarray(report.result("merge").data)
        out_dir = root / "replayed"
        print(f"made {result_file}")

    replay = workflow_from_file(result_file, registry=registry)
    for warning in replay.warnings:
        print("warning:", warning)
    print(replay.workflow.to_yaml())

    with run(replay.workflow, registry=registry, out_dir=out_dir, mode="replay") as report:
        for receipt in report.receipts:
            print("replayed into", ", ".join(str(f) for f in receipt.files))
        if original is not None:
            last = report.result(replay.workflow.steps[-1].input)
            same = np.array_equal(np.asarray(last.data), original)
            print("pixels identical to the original run:", same)
            return 0 if same else 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
