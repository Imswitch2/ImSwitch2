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

# Microscope Family Reference

Before building a KB, identify which family (or families) your system belongs to. Most microscopes combine several modalities — a confocal may also do FLIM, a TIRF system may also do SMLM, etc. The KB should cover **all modalities available** on the system.

## Architecture Axis 1: Image Formation

| Architecture | How image is formed | Key hardware signature | Typical detector |
|---|---|---|---|
| **Widefield** | Full-field illumination, camera captures entire FOV at once | LED/lamp + filter cubes + camera | sCMOS, EMCCD, CCD |
| **Point-scanning** | Focused spot raster-scanned by galvo mirrors | Galvo scanners + point detector(s) | PMT, APD, HyD |
| **Line-scanning** | Line of light swept across sample | Cylindrical optics + linear/area detector | sCMOS, linear CCD |
| **Spinning-disk** | Pinhole array on rotating disk (e.g., Yokogawa CSU) | Spinning disk unit + camera | EMCCD, sCMOS |
| **Light-sheet (SPIM)** | Thin sheet illuminates from side; camera detects orthogonally | Excitation arm + detection arm at ~90° | sCMOS |

## Architecture Axis 2: Resolution Regime

| Regime | Mechanism | Typical families |
|---|---|---|
| **Diffraction-limited** | Standard optics, ~200 nm lateral | Widefield, confocal, spinning-disk, light-sheet, multiphoton |
| **Super-resolution (deterministic)** | PSF engineering / patterned illumination | STED, SIM, RESOLFT |
| **Super-resolution (stochastic)** | Single-molecule localization | PALM, STORM, dSTORM, PAINT, MINFLUX |

## Microscope Family Cheat Sheet

Each family below has specific hardware, concepts, and troubleshooting patterns. The schema uses **conditional sections** — include only those relevant to your system.

**Camera-based, diffraction-limited:**
- **Widefield epifluorescence**: Simplest setup. LED or arc lamp, filter cubes, camera. No optical sectioning.
- **TIRF (Total Internal Reflection Fluorescence)**: Evanescent wave excitation (~100 nm depth). Requires high-NA objective + critical angle illumination. Often combined with SMLM.
- **Spinning-disk confocal**: Optical sectioning via pinhole array. Fast (camera-rate), good for live imaging. Yokogawa CSU-W1/X1 most common.
- **Light-sheet / SPIM**: Orthogonal illumination + detection. Gentle, fast volumetric imaging. Many geometries (diSPIM, lattice, Gaussian, Bessel).

**Camera-based, super-resolution:**
- **SIM (Structured Illumination Microscopy)**: Patterned illumination (grating, SLM, DMD) + reconstruction. ~2x resolution gain (100 nm). Variants: 2D-SIM, 3D-SIM, TIRF-SIM, nonlinear SIM.
- **SMLM (Single-Molecule Localization)**: Stochastic switching of individual fluorophores. PALM (photoactivatable), STORM/dSTORM (photoswitchable), PAINT (transient binding), DNA-PAINT. 20–30 nm resolution. Requires thousands of camera frames + reconstruction.

**Point-scanning, diffraction-limited:**
- **Confocal (CLSM)**: Pinhole-based optical sectioning. Galvo or resonant scanning. Standard workhorse.
- **Multiphoton (2P, 3P)**: Pulsed IR/NIR excitation. Intrinsic optical sectioning. Deep tissue imaging. Requires femtosecond pulsed laser (Ti:Sapph, OPO, fiber laser).
- **FLIM (Fluorescence Lifetime)**: Measures fluorescence decay kinetics. Can be combined with confocal or multiphoton. Requires TCSPC electronics or frequency-domain detection.
- **FCS / FCCS**: Fluorescence correlation spectroscopy. Point measurement of diffusion/concentration. Requires high-sensitivity APD + correlator.

**Point-scanning, super-resolution:**
- **STED**: Depletion beam (donut) shrinks PSF. Requires pulsed/CW depletion laser, beam shaping (SLM/phase plate), precise timing.
- **RESOLFT**: Like STED but with reversibly switchable fluorescent proteins. Lower light dose.
- **MINFLUX**: Patterned excitation + localization. Sub-5 nm resolution. Highly specialized.

**Hybrid / special:**
- **FRAP / Photoactivation / Photoconversion**: Uses targeted illumination to bleach or switch fluorophores. Often a module added to confocal.
- **Expansion microscopy (ExM)**: Physical sample expansion + conventional imaging. The KB would document the expansion protocol in procedures.yaml and adjusted resolution in limits.yaml.
