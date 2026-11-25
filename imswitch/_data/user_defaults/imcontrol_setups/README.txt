Example hardware setups for the hardware control module, and processing-only
ImProcess presets. ImSwitch copies them into the default setup folder,
<Documents>/ImSwitchConfig/imcontrol_setups (~/ImSwitchConfig/imcontrol_setups
outside Windows). Preferences > Setup folder… (in ImSwitch and in the Config
Studio) points ImSwitch at another folder of setups; these examples stay in the
default one.

Setups may be sorted into subfolders of a setup folder. The setup in use is
named by its path relative to the folder, e.g.
mock/point_scanning/simple_mock_setup.json.

example_no_hardware.json   The default: preselected on a first start, and the
                           only setup at the top. Runs without hardware.

mock/<modality>/           Hardware-free setups: simulated cameras, stages,
                           lasers and NI-DAQ, for trying ImSwitch and for tests.
  widefield/               example_mock.json
  point_scanning/          simple_mock_setup.json, galvo_apd_mock_scan_setup.json,
                           galvo_apd_simple_mock_scan_setup.json,
                           mixed_hamamatsu_apd_mock_scan_setup.json,
                           expert_mock_scan_setup.json (the Expert scan panel)
  monalisa/                mock_scan_setup.json, mock_scan_monalisa_live.json,
                           simple_monalisa_mock_scan_setup.json,
                           hamamatsu_mock_scan_setup.json
  lightsheet/              example_snouty_smart_modes.json
  time_tagger/             galvo_flim_mock_scan_setup.json (a simulated Swabian
                           Time Tagger with a FLIM detector),
                           galvo_two_apd_counting_mock_setup.json,
                           galvo_two_colour_flim_mock_setup.json (several
                           detectors on one card)
  step_boards/             expert_step_boards_mock_setup.json (the Expert panel
                           on simulated step boards, a leader and a follower)

real/<modality>/           Setups of real microscopes, to adapt to your own.
  widefield/               example_coolLED.json
  widefieldstarss/         example_kiralux_teensy.json
  monalisa/                example_monalisa.json
  sted/                    example_sted.json

processing/                ImProcess on its own, no hardware (processing-only
                           presets): fiji_processor.json,
                           general_image_processing.json, monalisa_processor.json,
                           snouty_processor.json, widefieldstarss_processor.json,
                           smlm_processor.json, bead_psf_processor.json,
                           restoration_processor.json, timelapse_processor.json,
                           tiling_mosaic_processor.json

A setup whose only key is "processing" (comment keys such as "// title" and
"// description" aside) is a preset: the config editor's "Start from preset..."
lists it by its "// title". The first reconstructor of its list is the one
ImProcess starts with.

Every file name is unique across the folders: a setup can be found by its file
name alone (the scripting tutorials name their mock setup that way), and an
options file that still names one of these by its old top-level name follows it
into its subfolder. After adding, changing or moving a setup file here, run
tools/update_user_defaults_history.py.
