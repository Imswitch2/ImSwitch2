# Example ImProcess drop-in plugins

These are ready-to-use examples of ImProcess **drop-in plugins**. Each is a
single `.py` file that defines one or more
`imswitch.improcess.processors.base.Processor` subclasses (analysis tools that
transform a result) or `imswitch.improcess.reconstructors.base.Reconstructor`
subclasses (the step that turns a raw file into the first result).

## How to use them

1. In any ImProcess window, choose **Plugins → Add plugin file…** and pick one
   of these `.py` files: it is copied into the plugins folder
   (`~/.imswitch/improcess_plugins/`) and the plugins are reloaded in one step.
   Or choose **Plugins → Open plugins folder…**, copy the file in yourself and
   choose **Plugins → Reload plugins** (or restart ImProcess).
2. A processor now appears in the **Load plugin** dropdown of the Plugins
   toolbar. Load it, select a compatible result, and run it — it gets a
   parameter panel, result-kind gating and results-table/graph integration
   automatically.
3. A reconstructor appears in the reconstructor picker at the top of the
   Parameters dock. Pick it and press *Reconstruct current* — it gets its
   parameter widget, the multidata actions, the file watcher and workflow
   support like any built-in.

No packaging, no `pip install`. A dropped-in plugin runs arbitrary Python at
startup, so only add files you trust.

## What each example shows

| File | Shows |
|---|---|
| `invert.py` | The minimal plugin: an image→image processor with no parameters, declared explicitly (`default_params` returns `{}`). |
| `gaussian_blur.py` | A real parameter widget (`get_values`), the matching `default_params` declaration, and a lazily-imported optional dependency (`scipy`). |
| `photophysics_suite.py` | A multi-mode analysis with a custom `curve` result: every effective parameter declared (hidden fallbacks included), and the result saved through the staged protocol (`supported_formats`, `plan_save`, `write_files`) as CSV plus a provenance companion. |
| `frame_average.py` | A **reconstructor**: raw `DataObj` in, one averaged image out, with a spin-box parameter and the pixel size carried through from the file. |

## Writing your own

**Processor.** Give it a unique, dotted `id` (e.g. `"me.myfilter"`), set
`kinds` to the result kinds it accepts (`"image"`, `"labels"`, `"table"`,
`"curve"`, `"localization"`, `"rgb"`, `"composite"`), implement `applies_to` (a
shape/axis gate), `make_param_widget` (return a widget exposing
`get_values() -> dict`) and `apply(result, params)` (pure: return a new
`ProcessingResult`), and **declare `default_params()`**: a class method
returning exactly what a fresh widget hands `apply` — same keys, same defaults,
`{}` if there are none. Without that declaration the plugin works in the GUI
but is GUI-only: workflows refuse it and its results are recorded as not
replayable. Every fallback `apply` reads is a parameter and belongs in the
defaults.

**Reconstructor.** Same file shape around a `Reconstructor` subclass: `name`,
a unique dotted `id`, `file_extensions` (which files the watcher hands it),
`default_params()`, `make_param_widget`, `make_metadata_dialog` (`None` when
there is no acquisition metadata to ask for) and
`process(data_obj, params, context=None)`: call `data_obj.checkAndLoadData()`,
compute on `data_obj.data`, and return a result whose last two axes are
`Y, X`. One file may define both kinds. Built-in ids always win a collision, so
a plugin cannot shadow a core plugin by reusing its id.

ImProcess compares the widget with the declaration when the panel opens; for
your own tests use
`imswitch.improcess.model.plugin_contract.check_plugin_contract(MyPlugin)`,
which returns the list of problems (empty when the contract holds).

Materialize lazy data (`np.asarray(result.data)`) before computing on it; a
workflow's view-only reconstruction is a lazy view over the file. If you return
a result class of your own, do not override `save()`: implement `plan_save` and
`write_files` as `photophysics_suite.py` does, so staging, the provenance
companion and the receipt come for free.

See the `_example_plugin.py` template that ImProcess writes into the plugins
folder, and `docs/improcess.rst` → *Drop-in analysis plugins* and *What a
plugin gets for free, and what it must declare*.
