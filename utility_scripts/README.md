# Utility Scripts

This directory contains standalone utility scripts for ImSwitch maintenance and development.

## aa_aotf_calibration.py

**Purpose:** Find and save the RF frequency and power calibration for every AA
Opto-Electronic AOTF declared in an ImSwitch setup.

Close ImSwitch first so the utility can own the serial ports, then run:

```bash
python utility_scripts/aa_aotf_calibration.py /path/to/setup.json
```

The window can connect all distinct AA AOTF serial devices from the setup,
manually test frequency/power, find the peak frequency with a Thorlabs PM100D,
and acquire the power LUT. Saving updates only the selected laser with:

- `protocolProfile: "aa.frequency-startup"`
- `frequencyMHz`
- `valueRangeMin` and `valueRangeMax`
- `calibCsvPath`

The setup is backed up to a timestamped `.bak` before it is atomically updated.
Connecting alone never enables a channel; test/sweep actions are explicit and
the utility switches the channel off after a sweep or failure.

## scopeaid_seed_from_config.py

**Purpose:** Convert ImSwitch setup JSON configs into ScopeAId microscope knowledge base seed files.

**Usage:**
```bash
python scopeaid_seed_from_config.py <input_json> -o <output_dir>
```

**What it does:**
- Reads an ImSwitch setup JSON (e.g., `example_sted.json`)
- Generates 4 YAML seed files matching the ScopeAId KB schema:
  - `hardware.yaml` — Physical component specs (partially seeded, nulls marked for manual completion)
  - `imswitch_defaults.yaml` — Full machine-readable extraction of JSON config
  - `software_config.yaml` — Device wiring narrative and safety guidance
  - `_index.yaml` — KB metadata and file index

**Key features:**
- Deterministic output (no LLM calls, no randomness)
- Preserves all JSON fields (snake_case normalized)
- Adds `# not in config — fill from datasheet` comments to null fields
- `--print-wave1-prompt` generates LLM prompt for completing `hardware.yaml`
- `--force` allows overwriting existing output directory

**Example:**
```bash
python utility_scripts/scopeaid_seed_from_config.py \
    imswitch/_data/user_defaults/imcontrol_setups/example_sted.json \
    -o ~/microscope_kb/sted_system
```

See `docs/microscope-kb/` for full ScopeAId KB schema documentation.
