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
| `invert.py` | The minimal plugin: an image→image processor with no parameters. |
| `gaussian_blur.py` | A real parameter widget (`get_values`) and a lazily-imported optional dependency (`scipy`). |

## Writing your own

Give your processor a unique, dotted `id` (e.g. `"me.myfilter"`), set `kinds` to
the result kinds it accepts (`"image"`, `"labels"`, `"table"`, `"curve"`,
`"localization"`, `"rgb"`, `"composite"`), implement `applies_to` (a shape/axis
gate), `make_param_widget` (return a widget exposing `get_values() -> dict`) and
`apply(result, params)` (pure: return a new `ProcessingResult`). See the
`_example_plugin.py` template that ImProcess writes into the plugins folder, and
`docs/improcess.rst` → *Drop-in analysis plugins*.
