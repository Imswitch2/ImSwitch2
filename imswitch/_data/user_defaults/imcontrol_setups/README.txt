This directory contains example setups for the hardware control module and
processing-only ImProcess presets.

Processing-only presets:
- fiji_processor.json
- monalisa_processor.json
- snouty_processor.json
- widefieldstarss_processor.json
- general_image_processing.json
- smlm_processor.json
- bead_psf_processor.json
- restoration_processor.json
- timelapse_processor.json
- tiling_mosaic_processor.json

A setup whose only key is "processing" (comment keys such as "// title" and
"// description" aside) is a preset: the config editor's "Start from preset..."
lists it by its "// title". The first reconstructor of its list is the one
ImProcess starts with. After adding or changing a setup file here, run
tools/update_user_defaults_history.py.
