<!--
ImSwitch-specific addendum to ScopeAId prompts. This file has no upstream
counterpart — it captures the small set of conventions and references that
only apply when the microscope is controlled by ImSwitch. Paste this prompt
into your LLM session *after* the upstream `system-prompt.md`.

If you maintain a different control software, fork this file rather than
editing the upstream prompts — the upstream prompts are deliberately
software-agnostic.
-->

# ImSwitch addendum

This addendum sits on top of `system-prompt.md` (and is referenced by
`generation-prompt.md`'s Wave 2 instructions for the optional
`<control_software>_defaults.yaml` file).

## ImSwitch terminology

When answering questions about an ImSwitch-controlled system, use these
terms with the meanings ImSwitch itself uses — don't substitute generic
equivalents:

- **manager** = ImSwitch driver class (e.g. `HamamatsuManager`,
  `APDManager`, `SwabianTimeTaggerManager`). Not a person; not a generic
  abstraction. The `managerName` field in `imswitch_defaults.yaml` is
  the authoritative identifier.
- **setpoint** = the value entered in the ImSwitch UI / written to the
  hardware via the manager. May differ from the value at the sample
  (e.g. laser power after AOM/AOTF/objective losses). When the user asks
  about "laser power", clarify whether they mean setpoint or sample-level
  unless `calibrations.yaml` resolves it.
- **valueRange** (`valueRangeMin` / `valueRangeMax` / `valueRangeStep`) =
  the UI control's allowed domain. **Not** the hardware's physical limit
  — the hardware may accept more (driver-clamped) or less (hardware
  fault). Treat as a soft setpoint domain, not a safety boundary.
- **forAcquisition** / **forFocusLock** / **forScanning** = boolean flags
  in the config selecting which pipelines a device participates in by
  default. A device with `forAcquisition: false` won't show up in the
  detector dropdown even though its manager is loaded.
- **managerProperties** = driver-specific config block (COM ports, DAQ
  channel lines, calibration CSV paths, conversion factors). Changing
  these usually implies a physical change too — see the upstream rule
  about flagging both halves.

## Where `imswitch_defaults.yaml` comes from

This file is **auto-generated** from the ImSwitch setup JSON by
`utility_scripts/scopeaid_seed_from_config.py` (in the ImSwitch repo).
Re-run the extractor whenever the setup config changes; never edit
`imswitch_defaults.yaml` by hand — your changes will be overwritten on
the next extraction.

The canonical refresh workflow:

1. Re-run the extractor against the new setup JSON to produce a new
   `imswitch_defaults.yaml`.
2. Paste the new YAML into the LLM session that already has the KB
   loaded.
3. Ask: *"Merge changes into the existing KB; flag removed devices, new
   devices, and any references in other files that need updating."*

Because the file is deterministically extracted, treat it as the
**highest-fidelity** source for any factual question about device names,
manager classes, channel wiring, value ranges, and presets — above
`software_config.yaml` which is human-curated narrative.

## Common ImSwitch gotchas

If the loaded KB is silent on these, default answers:

- *"Can I change `valueRangeMax` to make the field of view bigger?"*
  No — `valueRangeMax` only changes the UI control's allowed range. The
  galvo's actual physical range depends on `conversionFactor`, mirror
  geometry, and the optical relay; changing `valueRangeMax` alone makes
  the UI lie. The user needs to either accept the existing range or
  modify hardware + recompute `conversionFactor`.
- *"My calibration CSV path doesn't work after moving the config to a
  new machine."* Calibration paths in the ImSwitch config are absolute
  (typically Windows paths). Either edit the path in the JSON or copy
  the CSVs to the same absolute location on the new machine. Don't make
  the path relative without confirming ImSwitch can resolve it.
- *"Where do I see which manager a device uses?"* The `managerName`
  field under that device's entry in `imswitch_defaults.yaml`. Cross-
  reference to `hardware.yaml` for physical specs and to
  `software_config.yaml` for narrative about safe edits.
