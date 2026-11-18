# Polarization rig workflows

Scripts for the polarization rig that ran Microscope_GUI (its
`polarization_scan` branch): a Nucleo step board times the two Cobolts (405,
488), the camera and the two galvos; two Elliptec mounts each hold a
waveplate; a BPC303 drives the piezo. The mock setup
`nucleo_polarization_mock_setup.json` simulates all of it.

The rig's scans -- live Wide Field, live Galvo Scan, Wide Field 2D, Line
Scan 2D and the dark frames -- are Expert scan files written by

    python tools/polarization_rig_scans.py --setup <the rig's setup>

into `<ImSwitchConfig>/imcontrol_scans/polarization_rig`. The Expert panel
opens them (Load); the scripts here read them from there.

* `01_polarization_stacks.py` -- one recorded stack per pair of waveplate
  angles; each stack's file name and recorded attributes
  (`Rotator:<mount>:Position`) say the angles.
