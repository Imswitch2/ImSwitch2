"""``python -m imswitch.improcess.workflows`` — run, validate or list.

    python -m imswitch.improcess.workflows validate WORKFLOW.yaml
    python -m imswitch.improcess.workflows run WORKFLOW.yaml --input a.h5 b.h5 --out results/
    python -m imswitch.improcess.workflows run WORKFLOW.yaml --manifest inputs.csv --out results/
    python -m imswitch.improcess.workflows run WORKFLOW.yaml --bind raw=scan.h5::data --out results/
    python -m imswitch.improcess.workflows list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _registry(args):
    from imswitch.improcess.workflows.runtime import bootstrap_registry

    return bootstrap_registry(user_plugins=not args.no_user_plugins)


def _load(path: str):
    from imswitch.improcess.workflows.steps import Workflow

    return Workflow.load(path)


def cmd_validate(args) -> int:
    from imswitch.improcess.workflows.steps import validate

    workflow = _load(args.workflow)
    issues = validate(workflow, _registry(args))
    if issues:
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        print(f"{workflow.name}: {len(issues)} issue(s)", file=sys.stderr)
        return 1
    print(f"{workflow.name}: OK ({len(workflow.steps)} steps)")
    return 0


def cmd_list(args) -> int:
    from imswitch.improcess.workflows.runtime import describe_registry

    description = describe_registry(_registry(args))
    if args.json:
        print(json.dumps(description, indent=2, default=str))
        return 0
    for section in ("reconstructors", "processors"):
        print(f"{section}:")
        for pid, info in description[section].items():
            extra = f"  ports={info['ports']}" if "ports" in info else ""
            print(f"  {pid:28s} {info['name']}  [{info['version']}]{extra}")
    return 0


def cmd_run(args) -> int:
    from imswitch.improcess.workflows.batch import (
        bindings_for_inputs,
        bindings_from_manifest,
        run_over,
    )
    from imswitch.improcess.workflows.sources import parse_binding

    workflow = _load(args.workflow)
    registry = _registry(args)
    source_root = args.source_root or str(Path(args.workflow).resolve().parent)

    if args.manifest:
        bindings_list = bindings_from_manifest(args.manifest, [s.id for s in workflow.sources()])
    elif args.input:
        bindings_list = bindings_for_inputs(workflow, args.input, source_id=args.source)
    else:
        fixed = {}
        for item in args.bind or []:
            if "=" not in item:
                print(f"--bind expects source=path[::dataset], got {item!r}", file=sys.stderr)
                return 2
            sid, spec = item.split("=", 1)
            fixed[sid.strip()] = parse_binding(spec.strip())
        bindings_list = [fixed]

    batch = run_over(
        workflow, bindings_list, registry=registry, out_dir=args.out,
        overwrite=args.overwrite, source_root=source_root,
        mode=args.mode, allow_drift=args.allow_drift,
        verify_hash=args.verify_hash, hash_sources=args.hash_sources,
        on_row=lambda row: print(
            f"[{row.index}] {'ok ' if row.ok else 'FAIL'} "
            + (", ".join(row.files) if row.ok else f"{row.failed_step}: {row.error}")
        ),
    )
    summary = Path(args.out) / (args.summary or f"{workflow.name}_summary.csv")
    batch.write_summary(summary)
    print(f"{len(batch.rows)} run(s), {len(batch.failures)} failed; summary: {summary}")
    return 0 if batch.ok else 1


def cmd_replay(args) -> int:
    from imswitch.improcess.workflows.replay import ReplayError, workflow_from_file

    registry = _registry(args)
    try:
        result = workflow_from_file(args.file, registry=registry, save_fmt=args.fmt)
    except ReplayError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    workflow = result.workflow
    if args.out_workflow:
        path = workflow.save(args.out_workflow)
        print(f"wrote {path} ({len(workflow.steps)} steps)")
    else:
        print(workflow.to_yaml() if args.out_workflow is None and not args.run else "")
    if args.run:
        from imswitch.improcess.workflows.batch import run_over

        if not args.out:
            print("--run needs --out", file=sys.stderr)
            return 2
        batch = run_over(
            workflow, [{}], registry=registry, out_dir=args.out, overwrite=args.overwrite,
            mode="replay", allow_drift=args.allow_drift, verify_hash=args.verify_hash,
            on_row=lambda row: print(
                "replayed: " + ", ".join(row.files) if row.ok else f"FAILED at {row.failed_step}: {row.error}"
            ),
        )
        return 0 if batch.ok else 1
    return 0


def cmd_show(args) -> int:
    from imswitch.improcess.model.provenance_io import read_provenance

    document = read_provenance(args.file, validate=not args.no_validate)
    if args.json:
        print(json.dumps(document.to_dict(), indent=2, default=str))
        return 0
    print(f"schema {document.schema}")
    if document.artifact:
        print(f"artifact: {document.artifact.get('primary')} node={document.node} port={document.port} "
              f"files={document.artifact.get('files')}")
    if document.graph:
        nodes = document.graph["nodes"]
        print(f"graph: {len(nodes)} nodes, ImSwitch {document.graph.get('imswitch_version', '?')}")
        for node_id, node in nodes.items():
            inputs = ", ".join(f"{r['node'][-8:]}.{r['port']}" for r in node.get("inputs") or [])
            flag = "" if node.get("replayable", True) else "  [NOT REPLAYABLE: " + "; ".join(node.get("reasons") or []) + "]"
            print(f"  {node_id[-8:]}  {node.get('op'):12s} {node.get('plugin_id') or '':24s} <- {inputs or '-'}{flag}")
            if node.get("params"):
                print(f"           params: {json.dumps(node['params'], default=str)[:200]}")
    elif document.history:
        print("linear history only (schema 0):")
        for index, step in enumerate(document.history, start=1):
            print(f"  {index}. {step.get('operation')}  {json.dumps(step.get('params') or {}, default=str)[:200]}")
    else:
        print("no provenance in this file")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m imswitch.improcess.workflows",
                                     description="Run ImProcess workflows without the GUI")
    parser.add_argument("--no-user-plugins", action="store_true",
                        help="do not load drop-in plugins from the user plugins folder")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="check a workflow file against the installed plugins")
    p_validate.add_argument("workflow")
    p_validate.set_defaults(func=cmd_validate)

    p_list = sub.add_parser("list", help="list plugins, their versions, ports and default params")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="run a workflow over one or more inputs")
    p_run.add_argument("workflow")
    p_run.add_argument("--out", required=True, help="output directory")
    p_run.add_argument("--input", nargs="*", help="input paths (path or path::dataset) for a one-source workflow")
    p_run.add_argument("--source", help="which source step --input binds (when there are several)")
    p_run.add_argument("--manifest", help="CSV with one column per source id")
    p_run.add_argument("--bind", action="append", help="source=path[::dataset] (repeatable)")
    p_run.add_argument("--source-root", help="directory relative source paths resolve against")
    p_run.add_argument("--overwrite", action="store_true", help="allow overwriting existing outputs")
    p_run.add_argument("--mode", choices=("run", "replay"), default="run")
    p_run.add_argument("--allow-drift", action="store_true", help="replay even if sources differ from the recorded ones")
    p_run.add_argument("--hash-sources", action="store_true",
                       help="record a sha256 of every file source in the provenance (slower; enables --verify-hash on replay)")
    p_run.add_argument("--verify-hash", action="store_true",
                       help="in replay mode, require and check the recorded sha256 of every source")
    p_run.add_argument("--summary", help="summary CSV name inside --out")
    p_run.set_defaults(func=cmd_run)

    p_replay = sub.add_parser("replay", help="turn a saved result's provenance into a workflow, and optionally run it")
    p_replay.add_argument("file", help="a file ImProcess wrote (OME-TIFF, HDF5, Zarr, CSV with companion)")
    p_replay.add_argument("--out-workflow", help="write the workflow here (.yaml or .json); default: print YAML")
    p_replay.add_argument("--fmt", help="format for the replayed save (default: the file's own)")
    p_replay.add_argument("--run", action="store_true", help="also run it, in replay mode")
    p_replay.add_argument("--out", help="output directory for --run")
    p_replay.add_argument("--overwrite", action="store_true")
    p_replay.add_argument("--allow-drift", action="store_true", help="run even if the sources changed (then it is a run, not a replay)")
    p_replay.add_argument("--verify-hash", action="store_true",
                          help="require and check the sha256 recorded for every source (needs a run made with --hash-sources)")
    p_replay.set_defaults(func=cmd_replay)

    p_show = sub.add_parser("show-provenance", help="print the provenance a file carries (what to hand an LLM)")
    p_show.add_argument("file")
    p_show.add_argument("--json", action="store_true", help="the full document as JSON")
    p_show.add_argument("--no-validate", action="store_true")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
