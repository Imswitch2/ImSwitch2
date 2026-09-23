# Example ImProcess drop-in analysis plugins

These are ready-to-use examples of ImProcess **drop-in analysis plugins**. Each
is a single `.py` file that defines one or more
`imswitch.improcess.processors.base.Processor` subclasses.

## How to use them

1. In any ImProcess window, choose **Analyze → Drop-in plugins → Open plugins
   folder…**. (The folder is `~/.imswitch/improcess_plugins/`.)
2. Copy one of these `.py` files into that folder.
3. Choose **Analyze → Drop-in plugins → Reload plugins** (or restart ImProcess).
4. The processor now appears in the **Load tool** dropdown in the analysis
   toolbar. Load it, select a compatible result, and run it — it gets a
   parameter panel, result-kind gating and results-table/graph integration
   automatically.

No packaging, no `pip install`. A dropped-in plugin runs arbitrary Python at
startup, so only add files you trust.

## What each example shows

| File | Shows |
|---|---|
| `invert.py` | The minimal plugin: an image→image processor with no parameters, declared explicitly (`default_params` returns `{}`). |
| `gaussian_blur.py` | A real parameter widget (`get_values`), the matching `default_params` declaration, and a lazily-imported optional dependency (`scipy`). |
| `photophysics_suite.py` | A multi-mode analysis with a custom `curve` result: every effective parameter declared (hidden fallbacks included), and the result saved through the staged protocol (`supported_formats`, `plan_save`, `write_files`) as CSV plus a provenance companion. |

## Writing your own

Give your processor a unique, dotted `id` (e.g. `"me.myfilter"`), set `kinds` to
the result kinds it accepts (`"image"`, `"labels"`, `"table"`, `"curve"`,
`"localization"`, `"rgb"`, `"composite"`), implement `applies_to` (a shape/axis
gate), `make_param_widget` (return a widget exposing `get_values() -> dict`) and
`apply(result, params)` (pure: return a new `ProcessingResult`), and **declare
`default_params()`**: a class method returning exactly what a fresh widget hands
`apply` — same keys, same defaults, `{}` if there are none. Without that
declaration the plugin works in the GUI but is GUI-only: workflows refuse it and
its results are recorded as not replayable. Every fallback `apply` reads is a
parameter and belongs in the defaults.

ImProcess compares the widget with the declaration when the panel opens; for
your own tests use
`imswitch.improcess.model.plugin_contract.check_plugin_contract(MyProcessor)`,
which returns the list of problems (empty when the contract holds).

Materialize lazy data (`np.asarray(result.data)`) before computing on it; a
workflow's view-only reconstruction is a lazy view over the file. If you return
a result class of your own, do not override `save()`: implement `plan_save` and
`write_files` as `photophysics_suite.py` does, so staging, the provenance
companion and the receipt come for free.

See the `_example_plugin.py` template that ImProcess writes into the plugins
folder, and `docs/improcess.rst` → *Drop-in analysis plugins* and *What a
plugin gets for free, and what it must declare*.
