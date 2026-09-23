# Example ImProcess workflows

A workflow is an ordered list of steps (sources, reconstructions, processing
steps, saves) that runs **without the GUI** through the same code paths the
GUI uses, so its results carry the same provenance and the same save
receipts. The guide is `docs/improcess-workflows.rst`.

| File | Shows | Runs on synthetic data? |
|---|---|---|
| `batch_view_only_project_save.py` / `.yaml` | A folder of recordings → max projection → blur → OME-TIFF, one row per file in a summary CSV. | yes (no arguments) |
| `split_process_merge_diamond.py` / `.yaml` | Split a stack, process each slice with different settings, merge; refers to the split's slices by port (`split.C0`). | yes (no arguments) |
| `monalisa_reconstruct_chain.yaml` | MoNaLISA reconstruction → background subtraction → Z projection, two saves. Scan geometry comes from the recording's attributes. | needs a MoNaLISA recording |
| `consolidate_two_sources.yaml` | Two reconstructions consolidated into one, bound from a two-column manifest. | needs MoNaLISA recordings |
| `_synthetic.py` | Writes the synthetic recording the runnable examples use. | — |

## Run

```bash
# a script
python examples/improcess_workflows/batch_view_only_project_save.py

# a workflow file, over a folder
python -m imswitch.improcess.workflows run \
    examples/improcess_workflows/batch_view_only_project_save.yaml \
    --input recordings/*.h5 --out results/

# check a file against the installed plugins, list plugins and their defaults
python -m imswitch.improcess.workflows validate my_workflow.yaml
python -m imswitch.improcess.workflows list
```

Parameters not given in a step keep the plugin's widget defaults; `list`
prints them. Outputs are never overwritten unless `--overwrite` is passed.

## From the GUI

**File → Export workflow of current result…** writes the steps behind the
selected result as a workflow file. **Run workflow…** runs a file on the
recording it names; **Run workflow on selected results…** applies a file's
processing steps to every result selected in the list (its reconstruction
step stands in for each result); **Run workflow over files…** is the GUI
form of `run --input`. Each run reports on its own, so one bad input does
not stop the rest.
