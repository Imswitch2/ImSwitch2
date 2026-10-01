# Example napari endpoint adapters for ImProcess

An **endpoint** is an installed napari plugin that an ImProcess result can be
sent to: a plugin dock widget opened beside the result's layers in ImProcess's
own (napari) viewer, or a plugin reader that opens a file ImProcess exports.
The full description, the three lanes, the result→layer mapping and the
import rules are in `docs/improcess-napari-plugins.rst`.

These files show the two ways to declare an adapter.

| File | Shows |
|---|---|
| `setup_snippet.json` | Adapters in the setup file's `processing.napariEndpoints` list. Config entries override built-in adapters with the same id. |
| `napari_skimage_endpoints.py` | The same adapters as a **drop-in plugin file**: a module-level `NAPARI_ENDPOINTS` list, picked up from `~/.imswitch/improcess_plugins/` like any other drop-in plugin. |

## Try it

1. `pip install napari-skimage` into the ImSwitch environment.
2. Copy `napari_skimage_endpoints.py` into the ImProcess plugins folder
   (**Plugins → Open plugins folder…**), then **Plugins → Reload plugins**.
   (The three `napari-skimage` adapters in this file also ship as built-ins;
   the file exists to show the shape.)
3. Load or reconstruct an image, then **Plugins → napari plugins → Send
   current result to → napari-skimage: Automated threshold**.
4. Run the threshold in the plugin's dock; it adds a Labels layer.
5. **Plugins → napari plugins → Import layer from napari as result…**: the
   dialog pre-selects the plugin's layer and the source result because the
   adapter declares the output mapping. The imported labels result inherits
   the source grid only because the mapping says `preservesGrid` *and* the
   layer's transform is the identity.

## Writing an adapter for another plugin

Follow the recipe in `docs/improcess-napari-plugins.rst` ("Adding an adapter
for another plugin"). In short: find the plugin's manifest name and widget
display names (`npe2 list`), choose the lane, declare the result kinds, and
for a reader name the export format the reader really parses. Test with
npe2's `DynamicPlugin` before relying on the real plugin.
