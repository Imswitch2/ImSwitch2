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

# KB Generation Prompts

Generation happens in three steps as a tradeof between speed and consistency of results.
---

## Wave 1 — Foundation

Paste this prompt into a new AI session and attach all your source documents. This produces `hardware.yaml` and classifies the system.

````
You are a microscopy instrumentation expert creating a structured, AI-readable
knowledge base for a microscope system. I will provide source documents
(publications, protocols, config files, turn-on procedures, manuals, etc.).

## STEP 1: CLASSIFY THE SYSTEM

Before generating any files, state:

1. **Architecture**: camera-based / point-scanning / spinning-disk / light-sheet / hybrid
2. **Resolution regime**: diffraction-limited / super-resolution (deterministic) / super-resolution (stochastic)
3. **Modality families present**:
   - Camera-based diffraction-limited: widefield, TIRF, spinning-disk, light-sheet
   - Camera-based super-resolution: SIM, SMLM (PALM/STORM/dSTORM/PAINT)
   - Point-scanning diffraction-limited: confocal (CLSM), multiphoton, FLIM, FCS
   - Point-scanning super-resolution: STED, RESOLFT, MINFLUX
   - Hybrid/add-on: FRAP, photoactivation, expansion microscopy

## STEP 2: GENERATE hardware.yaml

This is the only file needed in this wave. Everything else will reference the
IDs established here, so be thorough and consistent.

## EXTRACTION RULES

1. **Be exhaustive**: Extract every factual detail — model numbers, wavelengths,
   power values, filter specs, serial numbers, port assignments, pixel sizes,
   frame rates, and all numerical parameters.

2. **Structured data, not prose**: Use YAML key-value pairs, lists, and nested
   objects. The goal is machine-readability.

3. **Use unique IDs**: Every referenceable entry MUST have a unique `id` field
   in descriptive snake_case (e.g. `laser_640_cw`, `cam_sCMOS`, `obj_100x_oil`).
   These IDs will be referenced by all other files — choose them carefully.

4. **Explicit nulls for unknowns**: Use `null` and add a comment like
   `# not specified in provided sources`.

5. **Units always explicit**: Include units in field names (e.g., `wavelength_nm`)
   or in companion `unit` fields. Never ambiguous.

6. **Modality-aware sections**: Use ONLY the conditional sections from the schema
   that apply to this system's modalities. Do not include STED sections for a
   widefield system, SMLM sections for a confocal, etc.

7. **No AI artifacts**: No citation markers, no [oai_citation:...], no internal
   references. Clean YAML only.

Ask clarifying questions if the source documents are ambiguous about critical
specifications — do NOT guess on safety-critical values.
````

---

## Wave 2 — All remaining files in parallel

Once you have reviewed and approved `hardware.yaml`, paste this prompt into **the same session** (so the AI retains the source documents as context). Replace the placeholder with the actual `hardware.yaml` content.

````
hardware.yaml is now finalised. Here it is for reference — use the IDs defined
here consistently across all files you generate in this wave:

--- BEGIN hardware.yaml ---
[PASTE YOUR APPROVED hardware.yaml HERE]
--- END hardware.yaml ---

Now generate ALL of the following files at once, in a single response downloadable.

FILES TO GENERATE IN THIS WAVE:

- concepts.yaml
- calibrations.yaml
- software_config.yaml   (or imswitch_defaults.yaml if the system uses ImSwitch)
- procedures.yaml
- safety.yaml
- troubleshooting.yaml
- limits.yaml
- recipes.yaml
- faq.yaml
- _changelog.yaml

## RULES FOR THIS WAVE

1. **ID consistency is mandatory**: All hardware IDs (laser IDs, detector IDs,
   camera IDs, etc.) must exactly match those defined in hardware.yaml above.
   Do not invent new component IDs.

2. **Cross-reference, don't duplicate**: Use references like
   `(ref: hardware.yaml#lasers.laser_640_cw)` rather than re-describing hardware.

3. **Modality-aware sections only**: Include conditional sections only for
   modalities present on this system.

4. **Troubleshooting must be actionable**: Each issue needs: observable symptom,
   modality tag, ranked likely causes, step-by-step fix, escalation criteria.

5. **Recipes must be complete**: Include enough to reproduce the experiment —
   all laser settings, detector settings, scan/camera parameters, sample info.

6. **Safety is non-negotiable**: Any hazard mentioned in the sources MUST appear
   in safety.yaml. Flag uncertain hazard levels as high with a note to verify.

7. **Explicit nulls for unknowns**: Use `null` with a comment rather than omitting
   fields or guessing values.

8. **No AI artifacts**: Clean YAML only — no citation markers, no internal refs.

Ask clarifying questions only if something is safety-critical and genuinely
ambiguous. Otherwise proceed and flag uncertainties as comments in the YAML.
````

---

## Wave 3 — Index

Once you have reviewed all Wave 2 files, generate `_index.yaml` last. It summarises everything and tells an AI how to navigate the KB.

````
All KB files are now finalised. Generate _index.yaml — the entry point for any
AI querying this knowledge base.

It should include:
- System name, family, architecture, institution, KB version, last_updated date
- A usage_instructions paragraph (2-3 sentences) telling an AI what this system
  is, what questions the KB can answer, and to start here before querying other files
- A key_capabilities list (bullet points of what makes this system distinctive)
- A files list: one entry per file with path, covers (one line), and 3-4 example
  query_types that file is best suited to answer

Base it on the hardware.yaml and all files generated in Waves 1 and 2.
````

---

## Extraction rules (quick reference)

These apply across all waves:

| Rule | Detail |
|---|---|
| Exhaustive | Every model number, wavelength, power, filter spec, port assignment |
| Structured | YAML key-value and nested objects — not prose |
| Unique IDs | snake_case, descriptive, consistent across all files |
| Null for unknowns | `null` + comment, never guess |
| Units explicit | In field names (`_nm`, `_mW`, `_us`) or companion `unit:` fields |
| No duplication | Cross-reference hardware IDs, don't re-describe components |
| Modality-aware | Only include schema sections relevant to this system |
| Safety first | Any hazard → safety.yaml, flag uncertainties as high |
