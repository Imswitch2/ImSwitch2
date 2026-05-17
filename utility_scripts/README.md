# Utility Scripts

This directory contains standalone utility scripts for ImSwitch maintenance and development.

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
