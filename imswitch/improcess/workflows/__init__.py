"""ImProcess workflows: reconstruct and process without the GUI.

A workflow is an ordered list of steps (sources, reconstructions,
processing steps, saves) that runs through the same code paths the GUI
uses, so its results carry the same provenance. See
``docs/improcess-workflows.rst``.

Quick use::

    from imswitch.improcess.workflows import (
        Workflow, Source, Reconstruct, Process, Save, bootstrap_registry, run,
    )

    wf = Workflow("project", [
        Source("raw", path="scan.h5"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("proj", "projection", {"axis": "Z", "mode": "max"}, inputs=["rec"]),
        Save("out", input="proj", fmt="tiff"),
    ])
    with run(wf, registry=bootstrap_registry(), out_dir="out") as report:
        print(report.receipts)
"""

from imswitch.improcess.workflows.batch import (
    BatchReport,
    BatchRow,
    bindings_for_inputs,
    bindings_from_manifest,
    run_over,
)
from imswitch.improcess.workflows.runner import RunError, RunReport, run
from imswitch.improcess.workflows.runtime import bootstrap_registry, describe_registry
from imswitch.improcess.workflows.sources import SourceError, SourceSpec, open_source, parse_binding
from imswitch.improcess.workflows.steps import (
    Consolidate,
    Issue,
    Process,
    Reconstruct,
    Ref,
    Save,
    Source,
    Workflow,
    WorkflowError,
    validate,
)

__all__ = [
    "BatchReport",
    "BatchRow",
    "Consolidate",
    "Issue",
    "Process",
    "Reconstruct",
    "Ref",
    "RunError",
    "RunReport",
    "Save",
    "Source",
    "SourceError",
    "SourceSpec",
    "Workflow",
    "WorkflowError",
    "bindings_for_inputs",
    "bindings_from_manifest",
    "bootstrap_registry",
    "describe_registry",
    "open_source",
    "parse_binding",
    "run",
    "run_over",
    "validate",
]
