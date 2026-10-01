"""napari endpoint adapters for ``napari-skimage``, as a drop-in plugin file.

Copy into the ImProcess plugins folder (Plugins -> Open plugins folder...)
and reload. A drop-in file may define processors, a ``NAPARI_ENDPOINTS``
list, or both. Entries are ``NapariEndpoint`` objects or the dict form the
setup file uses; both are shown.

``napari-skimage`` is a pure npe2 plugin with one dock widget per
scikit-image operation. Its widgets take an Image layer and add a new layer,
so the adapters also say how the added layer maps back into ImProcess
(``output_mappings``). ``preserves_grid`` is claimed only for operations that
are pixel-aligned with their input; the importer still checks that the
layer's transform is the identity before letting the result share a grid.
"""

from imswitch.improcess.model.napari_endpoints import NapariEndpoint, OutputMapping

NAPARI_ENDPOINTS = [
    NapariEndpoint(
        id="napari-skimage:median",
        label="napari-skimage: Median filter",
        lane="dock",
        plugin_name="napari-skimage",
        widget_name="Median filter",
        kinds=("image", "composite"),
        verified=True,
        output_mappings=(
            OutputMapping("*", "image", "image", preserves_grid=True, label="filtered image"),
        ),
    ),
    # The dict form is what the setup file accepts; keys may be camelCase.
    {
        "id": "napari-skimage:watershed",
        "label": "napari-skimage: Watershed",
        "lane": "dock",
        "plugin": "napari-skimage",
        "widget": "Watershed",
        "kinds": ["image", "labels"],
        "outputMappings": [
            {"pattern": "*", "layerType": "labels", "kind": "labels", "preservesGrid": True,
             "label": "watershed labels"}
        ],
    },
]
