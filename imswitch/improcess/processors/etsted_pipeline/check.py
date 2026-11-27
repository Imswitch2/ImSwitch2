"""Check an etSTED analysis pipeline file for use in a smart pipeline, in one line::

    python -m imswitch.improcess.processors.etsted_pipeline my_pipeline            # a name in the etSTED folder
    python -m imswitch.improcess.processors.etsted_pipeline C:/path/my_pipeline.py
    python -m imswitch.improcess.processors.etsted_pipeline --folder etsnouty name

It reads the file's signature (without running it), loads it the way the ``etsted-pipeline`` step does (the
runner etSTED's panel uses: the same checks), prints the parameters the step and its controls will show, and
runs it on a few synthetic frames to catch an error with its file and line (``--no-run`` skips that; ``--shape
512x512`` sets the frame). Exit code 0 when everything held, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np


def check(pipeline: str, folder: str = "etsted", *, frames: int = 14, shape=(256, 320), run: bool = True,
          out=print) -> bool:
    from imswitch.improcess.processors.etsted_pipeline.files import (
        PipelineFileError,
        error_text,
        inspect_pipeline_file,
        resolve_pipeline,
    )
    from imswitch.improcess.processors.etsted_pipeline.processor import (
        EtstedPipelineProcessor,
        parameter_docs,
        pipeline_fields,
    )

    ok = True
    try:
        path = resolve_pipeline(pipeline, folder)
        info = inspect_pipeline_file(path)
    except PipelineFileError as exc:
        out(f"FAILED  {exc}")
        return False
    out(f"{info.name}  ({path})")
    for problem in info.problems:
        out(f"  PROBLEM  {problem}")
        ok = False
    docs = parameter_docs(info.doc)
    out(f"  parameters ({len(info.exposed)}; the step and the controls show one numeric field each):")
    fields = {f.key: f for f in pipeline_fields(info)}
    for param in info.params:
        if param.name in fields:
            default = fields[param.name].default
            out(f"    {param.name:<22} {default!r:<10} {docs.get(param.name) or param.note}")
        else:
            reason = "named like a setting of the step: give it in 'params'" if param.exposed else param.note
            out(f"    {param.name:<22} (not a field: {reason})")
    try:
        from imswitch.imcommon.model.event_pipeline_runner import EtSTEDPipelineRunner

        runner = EtSTEDPipelineRunner()
        parameters = runner.load_file(path)
    except Exception as exc:  # noqa: BLE001 - the user's file
        out(f"  FAILED to load: {error_text(path, exc)}")
        return False
    names = runner.get_user_parameter_names()
    if names != [p.name for p in info.params if p.name in names]:
        out("  PROBLEM  the loaded signature differs from the file's text (a parameter added at run time?)")
        ok = False
    out(f"  loads: OK ({len(parameters)} arguments; coordinates are read as rows of (column, row) of the full frame)")
    if run:
        ok = _dry_run(path, frames, shape, out) and ok
    from imswitch.improcess.processors.etsted_pipeline.files import analysis_folder

    in_folder = path.resolve().parent == analysis_folder(folder).resolve()
    out("  as a smart-pipeline step: processor 'etsted-pipeline', params "
        f"{{'pipeline': {(info.name if in_folder else str(path))!r}, 'folder': {folder!r}" + "".join(
            f", {key!r}: {fields[key].default!r}" for key in fields) + "}")
    out("OK" if ok else "NOT OK")
    return ok


def _dry_run(path, frames: int, shape, out) -> bool:
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.processors.etsted_pipeline.processor import EtstedPipelineProcessor

    processor = EtstedPipelineProcessor()
    rng = np.random.default_rng(0)
    state: dict = {}
    seen = 0
    try:
        for index in range(frames):
            image = rng.poisson(200, shape).astype(np.uint16)
            result = ArrayProcessingResult(f"synthetic #{index}", image, ["Y", "X"], metadata={"frame_index": index})
            for testmode in (False, True) if index == frames - 1 else (False,):
                output = processor.apply(result, {**processor.default_params(), "pipeline": str(path),
                                                  "ignore_frames": 0, "testmode": testmode, "_state": state})
                points = output.results[0]
                seen += points.count
                if points.count and not (np.all(points.coordinates[:, 0] >= -1) and np.all(points.coordinates[:, 1] >= -1)
                                         and np.all(points.coordinates[:, 0] <= shape[0] + 1)
                                         and np.all(points.coordinates[:, 1] <= shape[1] + 1)):
                    out(f"  PROBLEM  detections outside the {shape[0]}x{shape[1]} frame: {points.coordinates[:3].tolist()} "
                        "(rows of (column, row) of the full frame are expected)")
                    return False
        out(f"  dry run: {frames} synthetic {shape[0]}x{shape[1]} frames in experiment mode and in test mode: OK "
            f"({seen} detections; pure noise rarely has any)")
        return True
    except Exception as exc:  # noqa: BLE001 - the pipeline's error names file and line
        out(f"  FAILED on a synthetic frame: {type(exc).__name__}: {exc}")
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m imswitch.improcess.processors.etsted_pipeline",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("pipeline", help="a pipeline name (in its panel's analysis_pipelines folder) or a .py path")
    parser.add_argument("--folder", default="etsted", help="etsted (default), etsnouty, etmonalisa, or a folder")
    parser.add_argument("--frames", type=int, default=14, help="synthetic frames for the dry run")
    parser.add_argument("--shape", default="256x320", help="rows x columns of the synthetic frames")
    parser.add_argument("--no-run", action="store_true", help="only load, do not run on synthetic frames")
    args = parser.parse_args(argv)
    try:
        rows, columns = (int(v) for v in args.shape.lower().split("x"))
    except ValueError:
        parser.error("--shape is rows x columns, e.g. 512x512")
    return 0 if check(args.pipeline, args.folder, frames=args.frames, shape=(rows, columns),
                      run=not args.no_run) else 1


if __name__ == "__main__":
    sys.exit(main())
