****************************************
ImProcess Workflows (Cookbook)
****************************************

Overview
========

An ImProcess **workflow** reconstructs and processes data without the GUI:
a batch of recordings through the same reconstructor and the same
processing steps, from a script, a terminal, or a scheduled job. It is the
processing counterpart of :doc:`scripting-wfs-workflows`.

A workflow is an ordered list of **steps**:

* ``Source`` — a recording (a file and the dataset inside it);
* ``Reconstruct`` — one reconstructor over one source;
* ``Consolidate`` — several reconstructions merged into one;
* ``Process`` — a processor over one or more earlier results;
* ``Save`` — write one result.

Steps refer to earlier steps by id, or by ``id.port`` when a step has
several outputs (a channel split has one port per channel, a background
subtraction can have ``signal`` and ``background``). That is what lets a
workflow be a graph rather than a chain: a split, two differently-treated
branches, and a merge.

Every step runs through the code path the GUI uses — the same run
envelope for reconstructions, the same processor runner, the same staged
save protocol — so a result made by a workflow carries the same
**provenance graph**, on the same ports, with the same save receipts, as
one made by clicking. That is also what makes replay (:ref:`workflows-replay`)
possible.

Quick start
===========

.. code-block:: python

   from imswitch.improcess.workflows import (
       Workflow, Source, Reconstruct, Process, Save, bootstrap_registry, run,
   )

   wf = Workflow("project-and-blur", [
       Source("raw", path="scan.h5"),
       Reconstruct("rec", "view-only", inputs=["raw"]),
       Process("proj", "projection", {"axis": "C", "mode": "max"}, inputs=["rec"]),
       Process("blur", "filter", {"method": "gaussian", "radius": 1.5}, inputs=["proj"]),
       Save("out", input="blur", fmt="tiff"),
   ])

   with run(wf, registry=bootstrap_registry(), out_dir="results") as report:
       print(report.receipts[0].files)      # every file the save produced
       blurred = report.result("blur")      # the in-memory result, if you want it

The same workflow as a file, run from the terminal:

.. code-block:: yaml

   name: project-and-blur
   schema: 1
   steps:
     - {step: source, id: raw}
     - {step: reconstruct, id: rec, reconstructor: view-only, inputs: [raw]}
     - {step: process, id: proj, processor: projection, params: {axis: C, mode: max}, inputs: [rec]}
     - {step: process, id: blur, processor: filter, params: {method: gaussian, radius: 1.5}, inputs: [proj]}
     - {step: save, id: out, input: blur, fmt: tiff}

.. code-block:: bash

   python -m imswitch.improcess.workflows validate project.yaml
   python -m imswitch.improcess.workflows run project.yaml --input recordings/*.h5 --out results/

Worked examples live in ``examples/improcess_workflows/``; two of them run
on synthetic data with no arguments.

Anatomy of a workflow
=====================

Steps and references
--------------------

.. list-table::
   :header-rows: 1
   :widths: 16 44 40

   * - Step
     - Fields
     - Ports it produces
   * - ``Source``
     - ``id``; ``path`` (optional until bound), ``dataset`` (needed when the container holds several), ``source_kind`` (``auto`` / ``image`` / ``tiling-manifest``), ``fingerprint`` (recorded by replay)
     - ``data``
   * - ``Reconstruct``
     - ``id``, ``reconstructor`` (plugin id), ``params``, ``inputs`` (exactly one source)
     - ``out``
   * - ``Consolidate``
     - ``id``, ``reconstructor``, ``inputs`` (reconstructions by that reconstructor); no ``params`` — consolidation merges what the reconstructions already are
     - ``out``
   * - ``Process``
     - ``id``, ``processor`` (plugin id), ``params``, ``inputs`` (ordered)
     - what the processor declares (see below)
   * - ``Save``
     - ``id``, ``input`` (one reference), ``fmt``, ``path_template``
     - none

A reference is ``"step"`` (the step's only, or first, port) or
``"step.port"``. Step ids use letters, digits, ``_`` and ``-``.

Parameters and defaults
-----------------------

``params`` holds only what you change. Everything else takes the plugin's
**widget default** — the value a freshly opened parameter panel would have
handed the plugin — from ``default_params()``, which is pinned to the widget
by a test. ``python -m imswitch.improcess.workflows list`` prints every
plugin's id, version, ports and defaults.

The keys a step may set are the plugin's ``param_keys()``: its defaults plus
any ``extra_param_keys`` it declares for settings no widget default names
(MoNaLISA's ``scan_params``). An unknown key is a validation error for
every step kind — reconstructions included — and a plugin with no
parameters accepts none; a typo cannot become a silent no-op. A
``consolidate`` step takes no parameters at all, because ``consolidate()``
takes none, and the provenance never records settings a step did not use.

A reconstructor may complete its parameters from the data before running
(``prepare_params``): MoNaLISA fills ``scan_params`` from the recording's
acquisition attributes exactly as the GUI does when the file is opened, so a
workflow need not spell out the scan geometry unless it wants to override
it.

Ports
-----

A processor declares its output ports through ``output_spec(params)``:

* one port, ``out``, for the ordinary one-in-one-out processor;
* named ports when there are several (``subtract-background`` with
  ``output_background: true`` yields ``signal`` and ``background``);
* a **pattern** when the ports depend on the data (``stack-split`` and
  ``channel-split`` yield ``C0, C1, …`` — one per slice, as many as the
  input has).

``validate`` checks every reference against the declared ports or pattern
before anything runs, and the runner checks the ports actually produced
against the declaration after each step. A single-input processor given
several inputs runs once per input; the second result onwards is the port
with the input's position appended (``out``, ``out1``, ``out2`` …).

Multi-input processors (``channel-merge``, ``image-calculator``,
``colocalization``, ``frc`` …) take their inputs in the order listed.

Saves
-----

``Save`` writes through the staged save protocol: every file is planned,
written to a staging directory beside the target, and published by atomic
rename with companions before the primary; the receipt on the report lists
exactly what was published. Outputs are **never overwritten** unless the run
is given ``overwrite=True`` (``--overwrite``).

``path_template`` placeholders: ``{out_dir}``, ``{source_stem}`` (the first
source's file name without suffixes), ``{step}`` (the save step's id),
``{input_step}``, ``{name}`` (the result's display name), ``{fmt}``,
``{ext}`` (the format's default suffix: ``.ome.tif``, ``.h5``, ``.ome.zarr``,
``.csv``, ``.hdf5``). The default is
``{out_dir}/{source_stem}_{step}{ext}``.

Formats are what the result type supports (``tiff``, ``hdf5``, ``zarr``;
``csv`` / ``picasso`` for localizations; ``imagej`` for MoNaLISA's classic
hyperstack). A format the type cannot write fails validation of the save
step at run time, before any file is touched.

Sources and binding
===================

A ``Source`` with a ``path`` is self-contained. A ``Source`` without one is
**bound** at run time, which is how one workflow serves a whole folder:

.. code-block:: bash

   # one source: bind each input in turn
   python -m imswitch.improcess.workflows run wf.yaml --input a.h5 b.h5 c.h5 --out results/

   # a container with several datasets: name the dataset
   python -m imswitch.improcess.workflows run wf.yaml --input a.h5::camera2 --out results/

   # several sources: one manifest column per source id
   #   left,right,right.dataset
   #   scan_0001.h5,scan_0002.h5,camera2
   python -m imswitch.improcess.workflows run wf.yaml --manifest pairs.csv --out results/

   # or bind explicitly
   python -m imswitch.improcess.workflows run wf.yaml --bind left=a.h5 --bind right=b.h5::camera2 --out results/

Relative source paths resolve against the workflow file's directory, or
against ``--source-root``. A multi-dataset container without a dataset name
is an error that lists the datasets, never a guess. Any file inside a
tiling run (a tile, the mosaic, the manifest) binds as a manifest-backed
source that the ``tiling-mosaic`` reconstructor understands.

From Python, ``bindings_for_inputs(workflow, paths)`` and
``bindings_from_manifest(csv, source_ids)`` produce the per-run bindings,
and ``run_over`` runs them:

.. code-block:: python

   from imswitch.improcess.workflows import bindings_for_inputs, run_over

   batch = run_over(wf, bindings_for_inputs(wf, paths), registry=registry, out_dir="results")
   batch.write_summary("results/summary.csv")     # one row per input: ok, error, failed step, files
   for row in batch.failures:
       print(row.bindings, row.failed_step, row.error)

One failing input is one row in the summary; the others still run.

Runs and reports
================

``run(workflow, registry=…, bindings=…, out_dir=…)`` executes the steps in
order and returns a ``RunReport``: results keyed by ``step.port``
(``report.result("blur")``, ``report.ports_of("split")``), the save
receipts, warnings, and — on failure — ``failed_step`` and ``error``
(raised as ``RunError`` with the report attached as ``exc.report``). Use it
as a context manager, or call ``report.close()``, so the sources it opened
are released.

``bootstrap_registry()`` builds a **fresh** plugin registry for the run —
every built-in unless a setup's ``processing`` block (or explicit id lists)
narrows it, plus the drop-in plugins from the user plugins folder — and
stamps each plugin's version (the ImSwitch distribution version for
built-ins, a digest of the file for drop-ins). Every call returns a new
registry; two runs share plugin instances only if you pass the same
registry object to both. The command line never does: each batch row gets
its own registry.

Two run modes are kept apart on purpose:

* ``run`` (the default) binds whatever sources it is given: apply this
  recipe to new data.
* ``replay`` verifies each source against what was recorded: a binding may
  relocate the file but not point at a different dataset or kind of
  source, and every recorded fingerprint field (shape, dtype, size,
  modification time, attribute digest, tiling manifest) must be present
  now and equal — a field that cannot be determined today is a mismatch,
  not a pass. Those fields cannot tell two files of the same shape and size
  apart by content; for that, record a hash when running
  (``hash_sources=True`` / ``--hash-sources``, one read pass over each
  source) and replay with ``verify_hash=True`` / ``--verify-hash``, which
  requires the recorded hash and compares it. ``allow_drift`` downgrades a
  mismatch to a warning — and then it is a run, not a replay: the report's
  ``mode`` becomes ``"run"``.

``run_over`` accepts either a registry or a zero-argument factory
(``bootstrap_registry`` itself); with a factory every row gets fresh plugin
instances, so a plugin that caches state cannot carry it from one input to
the next.

A row that fails after one of its ``Save`` steps has already published a
file is reported with ``ok = False`` **and** the files written before the
failure in ``files``, with ``partial = True``. A save that fails rolls back
what it published; if the rollback itself cannot remove or restore a file,
the ``SaveError`` names every path left behind rather than reporting only
the original error. A save that succeeds but cannot remove its own
staging or backup directory still returns its receipt, with those
directories listed in ``SaveReceipt.leftovers`` and a warning logged. Partial files are left in place (they
are complete, receipted files of an earlier step), and the summary CSV
carries the ``partial`` column so nobody has to guess whether a failed row
left anything behind.

Testing a workflow
==================

Workflows are testable with synthetic data and no hardware:

.. code-block:: python

   def test_my_workflow(tmp_path):
       raw = write_synthetic_recording(tmp_path / "scan.h5")     # examples/improcess_workflows/_synthetic.py
       registry = bootstrap_registry(user_plugins=False)
       with run(build_workflow(), registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path) as report:
           assert report.receipts[0].primary.exists()
           assert output_node(report.result("blur"))["params"]["radius"] == 1.5

``validate(workflow, registry)`` returns every issue that can be known
before running (unknown plugins, dangling references, undeclared ports,
wrong arity, unknown parameters, unknown formats); it is what the CLI's
``validate`` prints.

Drop-in plugins in workflows
============================

A processor from the user plugins folder (see :doc:`improcess`, "Drop-in
analysis plugins") is a plugin like any other: ``bootstrap_registry()``
discovers it, its id goes in a ``Process`` step, and its version in the
provenance is a digest of its file. Give it ``default_params()`` so
``params`` can stay short.

.. _workflows-replay:

Replay: from a saved file back to a workflow
============================================

Every file ImProcess writes carries a **provenance document**: the graph
of steps that made it (plugin ids and versions, parameters, ordered input
ports, ROI restrictions, the source's path and fingerprint) and an
artifact record naming the file itself. That is a workflow in all but
shape, and ``replay`` reshapes it — like Fiji's macro recorder, except
nothing had to be recording: the record is in the file.

.. code-block:: bash

   # what the file knows about itself
   python -m imswitch.improcess.workflows show-provenance results/cell_3_maxproj_blur.ome.tif

   # the workflow that made it, as YAML (print, or write with --out-workflow)
   python -m imswitch.improcess.workflows replay results/cell_3_maxproj_blur.ome.tif --out-workflow cell_3.yaml

   # make it again, verifying the source has not changed
   python -m imswitch.improcess.workflows replay results/cell_3_maxproj_blur.ome.tif --run --out replayed/

From Python:

.. code-block:: python

   from imswitch.improcess.workflows import workflow_from_file, bootstrap_registry, run

   registry = bootstrap_registry()
   replay = workflow_from_file("results/cell_3_maxproj_blur.ome.tif", registry=registry)
   for warning in replay.warnings:          # version drift, migrated params, streaming origin
       print(warning)
   with run(replay.workflow, registry=registry, out_dir="replayed", mode="replay") as report:
       print(report.receipts[0].files)

In the GUI, **File → Export workflow of current result…** writes the same
workflow from the result in memory (no file needed), and **File → Run
workflow…** runs a workflow file and adds its results to the reconstruction
list.

What replay reproduces, and what it refuses
-------------------------------------------

Replay walks the graph backwards from the file's own node and emits one
step per node: sources with their recorded path, dataset and fingerprint;
reconstructions and consolidations with their parameters; processing steps
with their parameters, **ordered input ports** (so a channel split feeding
two branches and a merge comes back as exactly that) and their ROI
restriction; and finally a ``Save`` with a **parameterised** destination —
never the original path. Parameters recorded under an older
``params_version`` are migrated by the plugin; a newer one is refused.
Version drift (ImSwitch, a plugin) and a streaming origin are warnings.

Replay refuses, listing every reason, when the graph contains a step that
cannot be run again:

* a node recorded **non-replayable**: a parameter that had no lossless
  form (an object the plugin's codec could not write down), an ROI
  restriction too large to keep and recorded by reference, a source that
  was never persisted (a RAM recording), a stream that did not complete,
  or a layer imported from a napari plugin;
* a plugin that is not installed;
* parameters recorded with a newer ``params_version`` than the installed
  plugin has.

In ``replay`` mode the run also verifies every source against the recorded
fingerprint (dataset, shape, dtype, size, modification time, attribute
digest, tiling manifest) and stops on a mismatch; ``--allow-drift`` turns
that into a warning, and the report then says it was a run, not a replay.

Files written before the graph existed carry only the linear
``processing_history`` (schema 0). Replay builds the best workflow it can —
an unbound source, a view-only reconstruction if none was recorded, the
processing steps with their (possibly summarised) parameters — and warns
about every gap. That is where the next section comes in.

Reconstructing a pipeline with an LLM
-------------------------------------

When ``replay`` refuses, or the file predates the graph, or the file was
written by another program, the record is still worth reading: it names
the steps and most of the settings, and a language model can turn it into a
workflow draft for you to check. Give it three things:

1. **the provenance** — ``python -m imswitch.improcess.workflows
   show-provenance FILE --json`` (or the Metadata panel's copy action);
2. **the plugin catalogue** — ``python -m imswitch.improcess.workflows list
   --json``: every plugin id, its default parameters and its ports;
3. **this page**, which is the schema.

A prompt that works:

.. code-block:: text

   You are writing an ImSwitch ImProcess workflow file (YAML, schema 1) as
   described in the attached documentation page. Attached are (a) the
   provenance document of a result file and (b) the catalogue of installed
   plugins with their default parameters and output ports.

   Produce a workflow that reproduces the result:
   - one `source` step per recorded source (leave `path` unset if the
     recorded path is a temporary or in-memory location, and say so);
   - `reconstruct` / `consolidate` / `process` steps with the recorded
     parameters; only include parameters that differ from the catalogue
     defaults;
   - refer to multi-output steps by their ports (e.g. `split.C1`,
     `bg.background`) exactly as the provenance's input references do;
   - a final `save` step with fmt equal to the file's own format and the
     default path template.

   Where the provenance is incomplete (a node marked not replayable, a
   summarised parameter such as "<array shape=(512, 512)>", an unknown
   plugin), do not guess silently: add a `# TODO` comment on that step
   explaining what is missing and what a human must supply.

   Output only the YAML.

Validate the draft before trusting it:

.. code-block:: bash

   python -m imswitch.improcess.workflows validate draft.yaml

Validation reports unknown plugins, undeclared ports, unknown parameters
and wrong arity; what it cannot check is whether a guessed parameter value
is the one that was used, which is why summarised parameters must stay
marked.

Related documentation
=====================

* :doc:`improcess` — the ImProcess module, its plugins and result flow
* :doc:`improcess-napari-plugins` — sending results to napari plugins
* :doc:`scripting-wfs-workflows` — the acquisition-side cookbook this one mirrors
