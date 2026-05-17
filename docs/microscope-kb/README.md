# Build Your Own Microscope Knowledge Base

ImSwitch2 ships with **templates and schemas** for building a structured, AI-readable knowledge base (KB) about your microscope system. The KB captures hardware specs, calibrations, procedures, safety protocols, and troubleshooting guides in a standardized YAML format that AI agents (like the ImSwitch `imcontrol` agent) can query to provide context-aware assistance.

**No KB content ships with ImSwitch.** You build your own KB locally after your microscope is set up.

---

## What Is a Microscope KB?

A microscope KB is a collection of YAML files that describe:

- **Hardware**: Lasers, detectors, cameras, stages, filters, objectives
- **Modalities**: Confocal, STED, SIM, SMLM, multiphoton, widefield, TIRF, light-sheet
- **Calibrations**: Laser power curves, pixel sizes, PSF measurements
- **Procedures**: Startup, shutdown, alignment, maintenance
- **Safety**: Laser classifications, PPE requirements, emergency procedures
- **Troubleshooting**: Symptom → cause → fix decision trees
- **Limits**: Resolution, field of view, frame rates, imaging depth
- **Recipes**: Complete experiment configurations (sample prep + acquisition settings)
- **FAQ**: Common questions with canonical answers

The KB follows the **ScopeAId schema** (MIT license, © ScopeAId contributors). ImSwitch2 includes the schema, prompts, and templates verbatim for interoperability with ScopeAId-compatible tools.

---

## Quick Start

### 1. Copy the Template Directory

```bash
# Create a local KB directory (outside the ImSwitch repo)
mkdir -p ~/microscope-kb/my-system

# Copy all templates
cp /path/to/imswitch2/docs/microscope-kb/templates/*.yaml ~/microscope-kb/my-system/
```

### 2. Choose Your Generation Strategy

**Option A: AI-assisted generation (recommended)**
- Use the **generation prompt** in `prompts/generation-prompt.md`
- LLMs interview you and fill the KB incrementally (8 waves)
- Works with Claude, GPT-4, or any long-context model

**Option B: Manual authoring**
- Fill templates directly using `schemas.md` as reference
- Good for simple single-modality systems

**Option C: Seed from ImSwitch config (future)**
- Utility script `utility_scripts/scopeaid_seed_from_config.py` (planned)
- Auto-populates hardware.yaml, software_config.yaml, imswitch_defaults.yaml from your ImSwitch setup JSON

### 3. Fill the KB (AI-assisted)

Open the **generation prompt** (`prompts/generation-prompt.md`) and paste it into your LLM of choice (Claude, GPT-4, etc.). The prompt will guide you through **8 waves**:

1. **System Overview** → `_index.yaml`
2. **Hardware Inventory** → `hardware.yaml`
3. **Modality Concepts** → `concepts.yaml`
4. **Calibrations** → `calibrations.yaml`
5. **Procedures & Safety** → `procedures.yaml`, `safety.yaml`
6. **Troubleshooting** → `troubleshooting.yaml`
7. **Limits & Performance** → `limits.yaml`
8. **Recipes & FAQ** → `recipes.yaml`, `faq.yaml`

The LLM will ask clarifying questions, then generate YAML content for each file. Copy-paste the output into your local templates.

### 4. Validate Your KB

```bash
# Check YAML syntax
yamllint ~/microscope-kb/my-system/*.yaml

# Verify schema compliance (if you have the ScopeAId validator)
python3 /path/to/scopeaid/validate_kb.py ~/microscope-kb/my-system
```

### 5. Point ImSwitch to Your KB

In your ImSwitch setup JSON, add:

```json
{
  "microscope_kb_path": "~/microscope-kb/my-system"
}
```

The `imcontrol` agent will now use your KB to answer hardware questions, suggest troubleshooting steps, and validate acquisition parameters.

---

## File Reference

### Core Schema Files (Read These First)

- **`schemas.md`**: Complete YAML schema reference with conditional sections for all modalities
- **`microscope-families.md`**: Taxonomy of microscopy modalities (confocal, STED, SIM, SMLM, etc.)
- **`prompts/generation-prompt.md`**: AI-assisted KB generation workflow (8 waves)
- **`prompts/system-prompt.md`**: System-level instructions for LLMs using the KB

### Templates (Copy These to Your Local KB)

All templates are in `templates/`:

| File | Purpose | When to Fill |
|------|---------|--------------|
| `_index.yaml` | KB manifest + usage instructions | Wave 1 (System Overview) |
| `hardware.yaml` | Physical components + optical layout | Wave 2 (Hardware Inventory) |
| `concepts.yaml` | Modality explanations | Wave 3 (Modality Concepts) |
| `calibrations.yaml` | Power curves, position calibrations | Wave 4 (Calibrations) |
| `software_config.yaml` | ImSwitch/MicroManager config | Wave 4 (auto-seed planned) |
| `imswitch_defaults.yaml` | ImSwitch-specific defaults | Wave 4 (auto-seed planned) |
| `procedures.yaml` | Startup, shutdown, alignment | Wave 5 (Procedures & Safety) |
| `safety.yaml` | Laser safety, PPE, emergency procedures | Wave 5 (Procedures & Safety) |
| `troubleshooting.yaml` | Symptom → cause → fix trees | Wave 6 (Troubleshooting) |
| `limits.yaml` | Performance envelope (resolution, FOV, speed) | Wave 7 (Limits & Performance) |
| `recipes.yaml` | Complete experiment configurations | Wave 8 (Recipes & FAQ) |
| `faq.yaml` | Common questions + answers | Wave 8 (Recipes & FAQ) |
| `_changelog.yaml` | KB version history | Update every time you edit |

---

## Modality-Specific Guidance

The schema uses **conditional sections** marked with comments like `# IF STED`, `# IF camera-based`, etc. Include only the sections relevant to your system.

### Example 1: Point-Scanning Confocal + STED

Include:
- `point_detectors` (not `cameras`)
- `scanning` (galvos)
- `beam_shaping` (phase plate for STED donut)
- STED-specific procedures (alignment, donut check)
- STED troubleshooting (no resolution improvement, poor donut)

Omit:
- `cameras`, `spinning_disk_unit`, `light_sheet_optics`, `smlm_specific`

### Example 2: Widefield + TIRF + SMLM

Include:
- `cameras` (not `point_detectors`)
- `tirf_optics`
- `smlm_specific` (buffer system, activation laser)
- SMLM procedures (buffer prep)
- SMLM troubleshooting (blinking issues, drift)

Omit:
- `point_detectors`, `scanning`, `beam_shaping`, `pulsed_laser_system`

### Example 3: Hybrid System (Confocal + Widefield)

Include **both** `point_detectors` and `cameras`, plus separate limit/recipe sections for each modality.

---

## Best Practices

### 1. Start Small, Iterate

- Fill `_index.yaml`, `hardware.yaml`, `safety.yaml` first (minimum viable KB)
- Add modality-specific files as you use each mode
- Update `_changelog.yaml` every time you edit

### 2. Use Actual Measurements

- **Don't guess calibration values.** Use `null` if you don't have data.
- Store raw calibration CSVs in a `calibrations/` subfolder:
  ```
  ~/microscope-kb/my-system/
  ├── calibrations/
  │   ├── 488nm_power_curve.csv
  │   ├── pixel_size_63x.csv
  └── calibrations.yaml  (references the CSVs)
  ```

### 3. Version Control Your KB

```bash
cd ~/microscope-kb/my-system
git init
git add .
git commit -m "Initial KB for Zeiss LSM 980 confocal + STED"
```

### 4. Keep It Up to Date

- Log hardware changes (new laser, filter swap) in `_changelog.yaml`
- Re-run calibrations after optical alignment or component replacement
- Review safety procedures annually

### 5. Share Within Your Lab (Optional)

- Push your KB to a private GitHub repo
- Lab members can clone + customize for their experiments
- ImSwitch agents can diff two KBs to explain differences between systems

---

## Integration with ImSwitch Agents

The ImSwitch `imcontrol` agent (planned) will use your KB to:

- **Answer hardware questions**: "What's the max power of the 488 nm laser?"
- **Validate acquisition parameters**: "Is 100 µs dwell time within spec?"
- **Suggest troubleshooting steps**: "Low signal in STED → check donut alignment"
- **Generate acquisition presets**: "Set up live-cell confocal for GFP"
- **Safety checks**: "Laser power exceeds recommended limit for this sample"

The agent queries the KB via structured lookups (e.g., `hardware.yaml#illumination_sources.laser_488`) and never modifies the files.

---

## Upstream Sync with ScopeAId

This schema is maintained upstream at [ScopeAId](https://github.com/LREIN663/ScopeAId) (MIT license). ImSwitch2 ships a **snapshot** of the schema for offline use. To sync with the latest version:

```bash
# From the ImSwitch2 repo root
cd /path/to/imswitch2
git remote add scopeaid https://github.com/LREIN663/ScopeAId.git
git fetch scopeaid

# Cherry-pick schema updates (replace COMMIT_HASH with the latest commit from ScopeAId)
git cherry-pick COMMIT_HASH --no-commit
```

If you make ImSwitch-specific changes to the schema, mark them with HTML comments:

```yaml
# <!-- ImSwitch-specific: added support for REST API triggering -->
rest_api_port: 8001
```

---

## Roadmap

- [ ] **Auto-seed from ImSwitch config** (`scopeaid_seed_from_config.py`)
- [ ] **KB validator** (`validate_kb.py` wrapper around ScopeAId validator)
- [ ] **Example KBs** (confocal, STED, light-sheet) in `examples/microscope-kb/`
- [ ] **KB diff tool** (compare two KBs, explain differences)
- [ ] **ImSwitch agent integration** (query KB from `imcontrol` agent context)

---

## License

The ScopeAId schema (`schemas.md`, `microscope-families.md`, prompts) is **MIT licensed** by ScopeAId contributors.

**Your KB content** (the YAML files you create) is yours. No license restriction.

ImSwitch2 code is **GPL-3.0** (unchanged).

---

## Support

- **Schema questions**: See `schemas.md` or open an issue in [ScopeAId](https://github.com/LREIN663/ScopeAId)
- **ImSwitch integration**: Open an issue in [ImSwitch2](https://github.com/openUC2/ImSwitch)
- **LLM generation issues**: Check `prompts/generation-prompt.md` or try a different model (Claude Sonnet 4 works well)

---

## Example: Minimal KB (Confocal-Only)

```yaml
# _index.yaml
knowledge_base:
  system: "Basic Confocal System"
  family: "point-scanning confocal"
  architecture:
    image_formation: "point-scanning"
    resolution_regime: "diffraction-limited"
  version: "1.0"
  last_updated: "2026-05-17"
  modalities_available:
    - name: "Confocal"
      type: "point-scanning confocal"
      description: "Diffraction-limited confocal imaging"
  usage_instructions: >
    This KB covers a basic point-scanning confocal microscope.
    Start with this index, then consult hardware.yaml for components.
```

See `templates/` for full file examples.

---

**Ready to build?** Copy the templates and run the generation prompt! 🎉
