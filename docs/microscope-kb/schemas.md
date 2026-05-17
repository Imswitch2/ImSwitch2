<!--
Adapted from ScopeAId (https://github.com/LREIN663/ScopeAId).
Schema and original prose © ScopeAId contributors; preserved
here under the upstream license.  ImSwitch2 ships this schema
as a reference for the build-your-own microscope-KB workflow
documented in README.md alongside this file.  Any edits
diverging from ScopeAId upstream should be marked with an
inline HTML comment "<!-- ImSwitch-specific: ... -->" so they
can be re-synced cleanly when ScopeAId updates.
-->

# KB File Structure & YAML Schemas

## File Structure

Every microscope KB should contain these files. Omit optional fields with `null` rather than inventing values.

```
<microscope_name>_knowledge_base/
├── _index.yaml              # Manifest: what's in this KB, how to navigate it
├── hardware.yaml            # Physical components and optical layout
├── concepts.yaml            # Modality explanations relevant to this system
├── calibrations.yaml        # Power, position, and other calibrations
├── software_config.yaml     # Control software defaults and device wiring
├── procedures.yaml          # Startup, shutdown, alignment, maintenance workflows
├── safety.yaml              # All safety information (laser, electrical, chemical, bio)
├── troubleshooting.yaml     # Symptom → cause → fix decision trees
├── limits.yaml              # Performance envelope and operational boundaries
├── recipes.yaml             # Complete experiment configurations
├── faq.yaml                 # Common questions with canonical answers
└── _changelog.yaml          # Version history
```

---

## YAML Schemas (Modality-Aware)

All schemas use **conditional sections** marked with comments like `# IF scanning`, `# IF camera-based`, `# IF STED`, etc. Include only the sections that apply to your system. A system that does both confocal and TIRF would include both sets of conditional sections.

### _index.yaml
```yaml
knowledge_base:
  system: "<Full microscope name>"
  family: "<primary family, e.g., 'point-scanning confocal + STED'>"
  architecture:
    image_formation: "<widefield / point-scanning / spinning-disk / light-sheet / hybrid>"
    resolution_regime: "<diffraction-limited / super-resolution-deterministic / super-resolution-stochastic>"
  institution: "<Lab / institution>"
  version: "<KB version>"
  last_updated: "<YYYY-MM-DD>"

  modalities_available:
    - name: "<modality name>"
      type: "<family from cheat sheet>"
      description: "<one sentence>"
    # repeat for each modality

  usage_instructions: >
    <2-3 sentences telling an AI how to use this KB.
    Specify what this system is, what questions it can answer,
    and that it should start with this index to find the right file.>

  files:
    - path: <filename>
      covers: "<one-line description>"
      query_types: ["<example question 1>", "<example question 2>"]
    # repeat for each file
```

### hardware.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

microscope:
  name: "<system name>"
  type: "<modality description>"
  stand: "<microscope body make/model or 'custom'>"
  commercial_system: "<e.g., Zeiss LSM 980, Leica STELLARIS 8, Nikon AX R, or 'custom-built'>"
  objectives:                        # some systems have multiple objectives
    - id: "<obj_id>"
      vendor: "<vendor>"
      model: "<model string>"
      magnification: <number>
      na: <number>
      immersion: "<oil/water/air/silicone/multi>"
      notes: "<e.g., TIRF-compatible, STED White optimized>"
  control_software:
    name: "<ImSwitch/MicroManager/NIS-Elements/ZEN/LAS X/SlideBook/MetaMorph/custom/...>"
    version: "<if known>"
  core_modalities: []

functional_blocks:

  illumination_sources:
    <source_id>:
      wavelength_nm: <number or range>     # range for tunable lasers or broadband LEDs
      type: "<CW/pulsed/LED/arc_lamp/supercontinuum/tunable/femtosecond/...>"
      vendor: "<vendor>"
      model: "<model>"
      max_power_mW: <number or null>
      pulse_width: <string or null>        # e.g., "100 fs", "530 ps", "CW"
      repetition_rate: <string or null>    # e.g., "80 MHz", "40 MHz"
      purpose: "<what this source is used for>"

  # IF camera-based (widefield, TIRF, spinning-disk, light-sheet, SIM, SMLM)
  cameras:
    <camera_id>:
      type: "<sCMOS/EMCCD/CCD/...>"
      vendor: "<vendor>"
      model: "<model>"
      pixel_size_um: <number>
      chip_size_px: [<x>, <y>]
      quantum_efficiency_peak: <number or null>
      read_noise_e: <number or null>
      max_frame_rate_fps: <number or null>
      cooling: "<thermoelectric / liquid / air / none>"
      notes: []

  # IF point-scanning (confocal, STED, multiphoton, FLIM, FCS)
  point_detectors:
    <detector_id>:
      type: "<PMT/APD/SPAD/HyD/GaAsP/...>"
      vendor: "<vendor>"
      model: "<model>"
      spectral_filters:
        bandpass: "<filter spec or null>"
        notch: "<filter spec or null>"
        dichroic: "<dichroic spec or null>"
      gating_capable: <true/false>
      photon_counting_capable: <true/false>
      notes: []

  # IF point-scanning or spinning-disk
  scanning:
    <scanner_id>:
      type: "<galvo/resonant/piezo/MEMS/Yokogawa_CSU/...>"
      vendor: "<vendor>"
      model: "<model>"
      max_scan_field_um: [<x>, <y>]
      max_line_rate_hz: <number or null>
      bidirectional: <true/false or null>

  # IF spinning-disk
  spinning_disk_unit:
    vendor: "<Yokogawa / CrestOptics / other>"
    model: "<CSU-W1 / CSU-X1 / X-Light / ...>"
    pinhole_size_um: <number>
    disk_speed_rpm: <number or range>
    dichroic_options: []

  # IF light-sheet
  light_sheet_optics:
    excitation_geometry: "<Gaussian / Bessel / lattice / digitally_scanned / ...>"
    sheet_thickness_um: <number or range>
    excitation_objective:
      vendor: "<vendor>"
      model: "<model>"
      na: <number>
    detection_objective:
      vendor: "<vendor>"
      model: "<model>"
      na: <number>
    sample_mounting: "<agarose / chamber / inverted / ...>"
    notes: []

  # IF STED / RESOLFT
  beam_shaping:
    <component_id>:
      device: "<SLM/DMD/phase_plate/vortex_plate/...>"
      vendor: "<vendor>"
      model: "<model>"
      patterns: []               # e.g., [vortex, top_hat, bottle_beam]
      notes: []

  # IF SIM
  pattern_generation:
    method: "<grating / SLM / DMD / fiber_array / ...>"
    vendor: "<vendor>"
    model: "<model>"
    pattern_orientations: <number>       # typically 3 or 5
    pattern_phases: <number>             # typically 3 or 5
    notes: []

  # IF TIRF
  tirf_optics:
    method: "<objective-type / prism-type>"
    angle_control: "<motorized / manual / through-lens>"
    critical_angle_control: "<software / hardware>"
    penetration_depth_nm: <number or range>      # typically 70-200
    notes: []

  # IF multiphoton (2P, 3P)
  pulsed_laser_system:
    <laser_id>:
      type: "<Ti:Sapph / OPO / fiber / ...>"
      vendor: "<vendor>"
      model: "<model>"
      tuning_range_nm: [<min>, <max>]
      pulse_width_fs: <number>
      repetition_rate_MHz: <number>
      max_average_power_mW: <number>
      dispersion_compensation: "<prism_pair / GDD_control / none>"
      notes: []

  # IF FLIM
  lifetime_electronics:
    method: "<TCSPC / frequency_domain / gated_detection>"
    vendor: "<vendor>"
    model: "<model>"
    timing_resolution_ps: <number or null>
    channels: <number>
    notes: []

  # IF SMLM (PALM/STORM/PAINT)
  smlm_specific:
    activation_wavelength_nm: <number or null>       # e.g., 405 for STORM
    buffer_system: "<e.g., GLOX + MEA for dSTORM>"
    feedback_autofocus: <true/false>
    notes: []

  # IF FRAP / photoactivation
  targeted_illumination:
    method: "<galvo_bleach_module / DMD / SLM / scanner_ROI>"
    vendor: "<vendor or null>"
    model: "<model or null>"
    capabilities: []       # e.g., [FRAP, photoactivation, photoconversion, ablation]

  # COMMON TO ALL
  filter_configuration:
    type: "<filter_cubes / filter_wheel / spectral_detector / acousto-optic / ...>"
    elements:
      - id: "<filter_id>"
        type: "<excitation / emission / dichroic / notch>"
        spec: "<manufacturer part number or transmission spec>"
        position: "<cube position / wheel slot / ...>"

  sample_positioning:
    xy_stage:
      vendor: "<vendor>"
      model: "<model>"
      travel_mm: [<x>, <y>]
      motorized: <true/false>
    z_control:
      type: "<piezo/stepper/objective_scanner/galvo_ETL/...>"
      vendor: "<vendor>"
      model: "<model>"
      range_um: <number>

  focus_stabilization:     # if applicable
    method: "<IR_reflection / hardware_autofocus / image_correlation / none>"
    details: {}

  environmental_control:   # if applicable
    incubation: "<stage_top / enclosure / none>"
    temperature_control: <true/false>
    co2_control: <true/false>
    humidity_control: <true/false>
    perfusion: <true/false or null>

  timing_and_control:
    daq: "<make/model or null>"
    compute: "<CPU/GPU specs>"
    synchronization_notes: []
```

### concepts.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

concepts:
  # Include ONLY modalities available on this specific system.
  # For each, explain the principle AND how it is implemented on THIS system.

  <modality_name>:
    family: "<from the cheat sheet: widefield / confocal / spinning-disk / STED / SMLM / SIM / etc.>"
    architecture: "<camera-based / point-scanning / spinning-disk / light-sheet>"
    description: >
      <3-6 sentence explanation. Mention the physical principle, what makes it
      different from other modalities on this system, and its main trade-offs
      (resolution vs speed vs phototoxicity vs depth).>
    key_trade_offs:
      resolution: "<qualitative: diffraction-limited / ~2x super-resolution / nanoscale>"
      speed: "<qualitative: real-time / seconds / minutes>"
      phototoxicity: "<qualitative: low / moderate / high>"
      depth_penetration: "<qualitative: surface only / moderate / deep tissue>"
    when_to_use: >
      <1-2 sentences: under what experimental conditions should a user choose
      this modality over the others available on this system?>
```

### calibrations.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

calibrations:
  meta:
    measurement_reference: "<where measurements were taken>"
    recommended_storage: []
    notes: []

  laser_power_calibrations:
    - id: "<unique_id>"              # MUST be unique across ALL entries
      wavelength_nm: <number>
      modulator: "<AOTF/AOM/direct/neutral_density/analog_voltage/...>"
      measurement_location: "<before_body/at_sample/after_objective/...>"
      input:
        name: "<setpoint parameter name>"
        unit: "<unit>"
        min: <number>
        max: <number>
      output:
        name: "<measured parameter name>"
        unit: "<W/mW/uW>"
      power_range:
        min: <number>
        max: <number>
        unit: "<W/mW>"
      n_points: <number>
      source_file: "<path to raw data>"
      date_measured: "<YYYY-MM-DD>"
      notes: []

  # IF camera-based
  camera_calibrations:
    - id: "<unique_id>"
      camera: "<camera_id from hardware>"
      type: "<flatfield / dark_frame / pixel_map / gain_map / ...>"
      source_file: "<path>"
      date_measured: "<YYYY-MM-DD>"
      notes: []

  # IF SIM
  sim_calibrations:
    - id: "<unique_id>"
      type: "<pattern_calibration / OTF_measurement / ...>"
      source_file: "<path>"
      notes: []

  # IF SMLM
  smlm_calibrations:
    - id: "<unique_id>"
      type: "<PSF_model / bead_calibration / astigmatism_curve / ...>"
      source_file: "<path>"
      notes: []

  position_calibrations:
    - id: "<unique_id>"
      type: "<objective_z/stage_xy/pixel_size/magnification/...>"
      input: { name: "", unit: "", min: 0, max: 0 }
      output: { name: "", unit: "", min: 0, max: 0 }
      model:
        kind: "<linear_fit/lookup_table/polynomial/affine_transform/...>"
        parameters: {}
      source_file: "<path>"
      notes: []
```

### software_config.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

software_config:
  source_file: "<original config file name>"
  software: "<ImSwitch/MicroManager/NIS-Elements/ZEN/LAS X/SlideBook/MetaMorph/custom/...>"

  how_to_interpret: []

  guidance:
    general: []                    # what's safe to change
    safety_critical: []            # what must NOT be changed without expert knowledge

  # Include only sections relevant to the control software in use.
  # The structure should mirror the software's own config hierarchy.
  detectors: {}
  lasers: {}
  positioners: {}
  scan_defaults: {}
  roi_presets: {}
  acquisition_presets: {}
  # ... other software-specific sections as needed
```

### procedures.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

procedures:

  # REQUIRED for all systems:
  startup:
    name: "<system startup>"
    purpose: "<one sentence>"
    prerequisites: []
    safety_notes: []
    steps:
      - step: <number>
        action: "<what to do>"
        details: "<additional info>"
        warning: "<safety warning, if any>"
    verification: "<how to confirm success>"

  shutdown:
    name: "<system shutdown>"
    purpose: "<one sentence>"
    steps: []

  # RECOMMENDED for all systems:
  sample_preparation:
    name: "<sample mounting and preparation>"
    steps: []

  routine_maintenance:
    name: "<cleaning, oil changes, etc.>"
    steps: []

  # IF point-scanning or STED:
  alignment_check:
    name: "<PSF / donut / beam overlap check>"
    required_materials: []
    workflow: []

  # IF SIM:
  sim_pattern_calibration:
    name: "<SIM grating/pattern calibration>"
    steps: []

  # IF SMLM:
  smlm_buffer_preparation:
    name: "<imaging buffer preparation for STORM/dSTORM>"
    steps: []

  # IF multiphoton:
  dispersion_compensation:
    name: "<GDD / prism compensation adjustment>"
    steps: []

  # IF light-sheet:
  sheet_alignment:
    name: "<light sheet alignment and thickness optimization>"
    steps: []
```

### safety.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

safety:
  scope: "<what system this covers>"

  general_principles: []

  laser_safety:
    hazard_classification: "<class 1 / 2 / 3R / 3B / 4>"
    wavelengths_present:
      - wavelength_nm: <number>
        hazard_level: "<high/medium/low>"
        visible: <true/false>
        type: "<CW/pulsed/femtosecond>"
        notes: "<specific concern, e.g., invisible IR, high peak power>"
    required_ppe: []
    key_rules: []
    emergency_procedures: []

    # IF multiphoton:
    multiphoton_specific:
      - "Femtosecond pulsed IR beams are invisible and extremely hazardous at high average power."
      - "OPO output wavelengths may change — verify safety eyewear coverage for the current tuning range."

    # IF TIRF:
    tirf_specific:
      - "Beam exits at steep angles near the objective — risk of direct eye exposure when sample is removed."

    # IF UV excitation present:
    uv_safety:
      - "UV exposure hazards: skin and eye damage even from scattered light."

  electrical_safety:
    rules: []

  mechanical_safety:
    rules: []

  chemical_safety:
    rules: []
    # IF SMLM (dSTORM buffer contains MEA/TCEP, potentially toxic):
    smlm_buffer_safety: []

  biosafety:
    rules: []

  checklists:
    before_start: []
    before_lasers: []
    after_session: []
```

### troubleshooting.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

troubleshooting:
  safety_first:
    always_do: []

  issues:
    # The issues should cover the SPECIFIC modalities on this system.
    # Below are EXAMPLE categories — include what applies.

    # UNIVERSAL issues (all systems):
    # - No image / black image
    # - Out of focus / blurry
    # - Weak signal / low SNR
    # - Unexpected background / autofluorescence
    # - Software crash / communication error
    # - Stage drift / focus drift

    # IF camera-based:
    # - Camera not triggering / wrong exposure
    # - Hot pixels / striping artifacts
    # - Uneven illumination (flatfield)
    # - Saturated pixels

    # IF point-scanning:
    # - Scan artifacts (ringing, stepping)
    # - Bidirectional scan misalignment
    # - Low signal despite correct laser power (pinhole misaligned)

    # IF spinning-disk:
    # - Pinhole artifacts / grid pattern visible
    # - Crosstalk between pinholes

    # IF STED:
    # - No resolution improvement
    # - Donut asymmetry / poor null
    # - Signal loss without resolution gain (timing issue)

    # IF SIM:
    # - Reconstruction artifacts (honeycomb, haloing)
    # - Low modulation contrast
    # - Pattern not visible / not modulated

    # IF SMLM:
    # - Molecules not blinking / too dense
    # - Too few localizations
    # - Drift in reconstruction
    # - Buffer degradation (loss of switching)

    # IF multiphoton:
    # - No signal at any depth (mode-lock lost?)
    # - Signal only at surface (scattering / power insufficient)
    # - Photodamage / heat damage at depth

    # IF light-sheet:
    # - Striping artifacts (absorption shadows)
    # - Sheet too thick / poor sectioning
    # - Sample drift in agarose

    - id: "<UNIQUE_SHORT_ID>"
      observation: "<what the user sees / symptom>"
      modality: "<which modality this applies to, or 'all'>"
      typical_causes:
        - id: "<cause_id>"
          cause: "<one sentence>"
          likelihood: "<high/medium/low>"
      steps_to_fix:
        - step: <number>
          check: "<what to verify>"
          how: "<how to check>"
          fix: "<what to do if this is the problem>"
      escalation: "<when to stop and get expert help>"

    # AIM FOR: at least 5-10 issues covering each modality on the system
```

### limits.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

limits:
  notes: []

  # One section per modality available on the system:
  <modality>:
    resolution:
      lateral_nm: <number or range>
      axial_nm: <number or range>
      conditions: "<at what settings / what sample>"

    # IF camera-based:
    field_of_view_um: [<x>, <y>]
    effective_pixel_size_nm: <number>             # after magnification
    max_frame_rate_fps: <number>
    exposure_range_ms: { min: <n>, max: <n> }

    # IF point-scanning:
    scan_field_um: [<x>, <y>]
    max_line_rate_hz: <number>
    pixel_dwell_time_us: { min: <n>, max: <n> }
    frame_time_s: "<depends on ROI, note formula or lookup>"

    # IF spinning-disk:
    disk_confocality: "<pinhole size and spacing>"
    max_frame_rate_fps: <number>

    # IF light-sheet:
    volume_rate_hz: <number>
    sheet_thickness_um: <number>
    sample_size_limits_mm: [<x>, <y>, <z>]

    # IF SIM:
    raw_frames_per_reconstruction: <number>       # e.g., 9 for 2D-SIM (3 angles x 3 phases)
    reconstruction_time_s: <number or null>

    # IF SMLM:
    typical_frames_required: <number or range>    # e.g., 5000-20000
    localization_precision_nm: <number>
    min_acquisition_time_s: <number>
    reconstruction_time_min: <number or null>

    # IF multiphoton:
    max_imaging_depth_um: <number>
    two_photon_excitation_range_nm: [<min>, <max>]

  # COMMON sections:
  stage_limits:
    xy_travel_mm: [<x>, <y>]
    z_range_um: <number>

  laser_power_limits:
    <laser_id>:
      max_safe_at_sample_mW: <number>
      recommended_range_mW: [<min>, <max>]
      notes: "<photobleaching/damage threshold info>"

  temporal:
    min_latency_ms: <number or null>              # relevant for triggered systems
    notes: []

  environmental:
    max_experiment_duration_hours: <number or null>
    notes: []
```

### recipes.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

recipes:
  notes: []

  entries:
    - id: "<recipe_id>"
      name: "<descriptive name>"
      purpose: "<what biological question / experiment type>"
      modalities_used: []

      sample:
        type: "<cell type / organism / in vitro / tissue>"
        label: "<dye/fluorophore/protein>"
        mounting: "<live chamber / fixed slide / agarose / clearing medium / ...>"
        preparation_notes: "<fixation protocol, buffer, etc.>"

      settings:
        # Include ONLY the sections relevant to the modality:

        # IF camera-based (widefield, TIRF, SIM, SMLM, spinning-disk):
        camera:
          camera_id: "<from hardware>"
          exposure_ms: <number>
          em_gain: <number or null>              # EMCCD only
          binning: "<1x1 / 2x2 / ...>"
          roi_px: [<x>, <y>]

        # IF spinning-disk:
        spinning_disk:
          disk_speed_rpm: <number>
          pinhole_size_um: <number or 'fixed'>

        # IF TIRF:
        tirf:
          penetration_depth_nm: <number>
          angle: "<auto / manual value>"

        # IF SIM:
        sim:
          pattern_orientations: <number>
          pattern_phases: <number>
          reconstruction_software: "<name/version>"
          reconstruction_parameters: {}

        # IF SMLM:
        smlm:
          activation_laser: "<wavelength or null>"
          activation_power: <number>
          imaging_laser: "<wavelength>"
          imaging_power: <number>
          frames: <number>
          frame_rate_fps: <number>
          buffer: "<buffer composition>"
          reconstruction_software: "<ThunderSTORM / SMAP / picasso / ...>"
          localization_parameters: {}

        # IF point-scanning (confocal, STED, multiphoton):
        scan:
          pixel_size_nm: <number>
          dwell_time_us: <number>
          roi_um: [<x>, <y>]
          z_slices: <number or null>
          z_step_um: <number or null>
          averaging: "<line / frame, N times>"
          bidirectional: <true/false>

        # IF STED:
        sted:
          depletion_laser: "<laser id>"
          depletion_power: <number>
          phase_mask: "<vortex / top_hat / ...>"

        # IF multiphoton:
        multiphoton:
          excitation_wavelength_nm: <number>
          average_power_mW: <number>
          zoom: <number>

        # IF light-sheet:
        light_sheet:
          excitation_wavelength_nm: <number>
          sheet_na: <number>
          z_step_um: <number>
          views: <number>                        # for multi-view systems

        # COMMON:
        excitation:
          laser: "<laser id from hardware>"
          power_setpoint: <value>
          power_at_sample_approx: "<if known>"
        detection:
          detector: "<detector id>"
          filter: "<filter spec>"

      analysis_pipeline: "<software name or description>"
      expected_resolution_nm: <number or range>
      expected_snr: "<qualitative or quantitative>"
      common_pitfalls: []
      tips: []
```

### faq.yaml
```yaml
version: "<version>"
last_updated: "<YYYY-MM-DD>"

faq:
  entries:
    - q: "<question>"
      a: "<concise canonical answer>"
      refs: ["<file.yaml#section>"]

    # Aim for 10-20 entries. Good universal starters:
    # - "What resolution can I achieve with [modality]?"
    # - "What dyes/fluorophores work on this system?"
    # - "Can I do live-cell imaging?"
    # - "How long does startup take?"
    # - "What's the maximum field of view?"
    # - "Can I do multi-color imaging?"
    # - "How deep can I image into tissue?" (if multiphoton/light-sheet)
    # - "What reconstruction software do I use?" (if SIM/SMLM)
    # - "How do I prepare the imaging buffer?" (if SMLM)
    # - "What is the difference between [modality A] and [modality B] on this system?"
```

### _changelog.yaml
```yaml
changelog:
  knowledge_base: "<system name>"
  versions:
    - version: "<version>"
      date: "<YYYY-MM-DD>"
      summary: "<what changed>"
      changes: []
```
