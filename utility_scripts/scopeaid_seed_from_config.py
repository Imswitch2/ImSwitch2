#!/usr/bin/env python3
"""
Deterministic JSON-to-YAML extractor for ImSwitch→ScopeAId KB seeding.

Transforms an ImSwitch setup JSON (e.g., example_sted.json) into seed YAML files
matching the ScopeAId microscope KB schema. Three output files are produced:
  - software_config.yaml       (narrative skeleton with device list)
  - imswitch_defaults.yaml     (full machine-faithful extraction)
  - hardware.yaml              (partial: specs set to null with comments)
  - _index.yaml                (starter stub)

Usage:
  python scopeaid_seed_from_config.py <input.json> -o <output_dir> [--force] [--print-wave1-prompt]

No LLM, no network calls — pure deterministic transformation.
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not found. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def snake_case(name: str) -> str:
    """Convert arbitrary name to snake_case ID."""
    s = re.sub(r'[^\w\s-]', '', name)  # remove non-word chars except space and dash
    s = re.sub(r'[-\s]+', '_', s)     # replace spaces/dashes with underscore
    s = s.lower().strip('_')
    return s or 'unnamed'


def humanize_name(key: str) -> str:
    """Convert JSON key to human-readable name."""
    # If it's already human-readable (has spaces/capitals), keep it
    if ' ' in key or any(c.isupper() for c in key):
        return key
    # Otherwise, convert snake_case/camelCase to Title Case
    words = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+', key)
    return ' '.join(w.capitalize() for w in words) if words else key


def extract_devices(data: Dict[str, Any], section: str) -> List[Dict[str, Any]]:
    """Extract devices from a JSON section (detectors/lasers/positioners/etc.)."""
    devices = []
    section_data = data.get(section, {})
    for key, value in section_data.items():
        device = {
            'id': snake_case(key),
            'name': humanize_name(key),
            'manager': value.get('managerName'),
            'properties': value.get('managerProperties', {}),
        }
        # Add section-specific fields
        if section == 'lasers':
            device.update({
                'wavelength': value.get('wavelength'),
                'value_range': {
                    'min': value.get('valueRangeMin'),
                    'max': value.get('valueRangeMax'),
                    'step': value.get('valueRangeStep', 1.0)
                },
                'analog_channel': value.get('analogChannel'),
                'digital_line': value.get('digitalLine'),
            })
        elif section == 'detectors':
            device.update({
                'for_acquisition': value.get('forAcquisition', False),
                'for_focus_lock': value.get('forFocusLock', False),
                'analog_channel': value.get('analogChannel'),
                'digital_line': value.get('digitalLine'),
            })
        elif section == 'positioners':
            device.update({
                'axes': value.get('axes', []),
                'for_positioning': value.get('forPositioning', False),
                'for_scanning': value.get('forScanning', False),
            })
        elif section == 'rs232devices':
            # RS232 devices have no parent DeviceInfo structure
            device = {
                'id': snake_case(key),
                'name': humanize_name(key),
                'manager': value.get('managerName'),
                'properties': value.get('managerProperties', {}),
            }
        devices.append(device)
    return devices


def generate_software_config(data: Dict[str, Any], source_file: str) -> Dict[str, Any]:
    """Generate software_config.yaml (narrative skeleton)."""
    detectors = extract_devices(data, 'detectors')
    lasers = extract_devices(data, 'lasers')
    positioners = extract_devices(data, 'positioners')
    rs232 = extract_devices(data, 'rs232devices')
    
    config = {
        'version': '1.0',
        'last_updated': str(date.today()),
        'software_config': {
            'source_file': source_file,
            'software': 'ImSwitch',
            'how_to_interpret': [
                'This file was auto-extracted from an ImSwitch setup JSON.',
                'It provides a narrative overview of device wiring and safe configuration boundaries.',
                'See imswitch_defaults.yaml for the full machine-readable extraction.'
            ],
            'guidance': {
                'general': [
                    'Laser power values, detector exposure times, and ROI settings are safe to adjust within documented ranges.',
                    'Stage position limits should be verified against physical hardware stops.',
                ],
                'safety_critical': [
                    'Never exceed laser max power values without photodetector power meter verification.',
                    'Do not modify NI-DAQ channel assignments without verifying physical wiring.',
                    'Detector gating and timing parameters require expert knowledge — incorrect values can damage hardware.',
                ]
            },
            'detectors': {d['id']: {
                'name': d['name'],
                'manager': d['manager'],
                'for_acquisition': d.get('for_acquisition', False),
                'notes': []
            } for d in detectors},
            'lasers': {l['id']: {
                'name': l['name'],
                'manager': l['manager'],
                'wavelength_nm': l.get('wavelength'),
                'notes': []
            } for l in lasers},
            'positioners': {p['id']: {
                'name': p['name'],
                'manager': p['manager'],
                'axes': p.get('axes', []),
                'notes': []
            } for p in positioners},
        }
    }
    
    if rs232:
        config['software_config']['rs232_devices'] = {
            r['id']: {
                'name': r['name'],
                'manager': r['manager'],
                'notes': []
            } for r in rs232
        }
    
    return config


def generate_imswitch_defaults(data: Dict[str, Any], source_file: str) -> Dict[str, Any]:
    """Generate imswitch_defaults.yaml (full machine-faithful extraction)."""
    detectors = extract_devices(data, 'detectors')
    lasers = extract_devices(data, 'lasers')
    positioners = extract_devices(data, 'positioners')
    rs232 = extract_devices(data, 'rs232devices')
    
    defaults = {
        'version': '1.0',
        'last_updated': str(date.today()),
        'imswitch_defaults': {
            'intent': f'Auto-extracted defaults from {Path(source_file).name}',
            'source_file': source_file,
            'how_to_interpret': [
                'This file mirrors the ImSwitch JSON structure with normalized field names.',
                'All numeric values, paths, and channel assignments are preserved exactly.',
                'This is the highest-fidelity source for factual device configuration questions.',
            ],
            'guidance': [
                'Use this file to answer "what is the default X for device Y" questions.',
                'Cross-reference hardware IDs with hardware.yaml for physical specifications.',
            ],
            'detectors': {},
            'lasers': {},
            'positioners': {},
        }
    }
    
    for d in detectors:
        defaults['imswitch_defaults']['detectors'][d['id']] = {
            'name': d['name'],
            'manager': d['manager'],
            'manager_properties': d['properties'],
            'for_acquisition': d.get('for_acquisition', False),
            'for_focus_lock': d.get('for_focus_lock', False),
            'analog_channel': d.get('analog_channel'),
            'digital_line': d.get('digital_line'),
        }
    
    for l in lasers:
        defaults['imswitch_defaults']['lasers'][l['id']] = {
            'name': l['name'],
            'manager': l['manager'],
            'manager_properties': l['properties'],
            'wavelength_nm': l.get('wavelength'),
            'value_range_min': l['value_range']['min'],
            'value_range_max': l['value_range']['max'],
            'value_range_step': l['value_range']['step'],
            'analog_channel': l.get('analog_channel'),
            'digital_line': l.get('digital_line'),
        }
    
    for p in positioners:
        defaults['imswitch_defaults']['positioners'][p['id']] = {
            'name': p['name'],
            'manager': p['manager'],
            'manager_properties': p['properties'],
            'axes': p.get('axes', []),
            'for_positioning': p.get('for_positioning', False),
            'for_scanning': p.get('for_scanning', False),
        }
    
    if rs232:
        defaults['imswitch_defaults']['rs232_devices'] = {}
        for r in rs232:
            defaults['imswitch_defaults']['rs232_devices'][r['id']] = {
                'name': r['name'],
                'manager': r['manager'],
                'manager_properties': r['properties'],
            }
    
    # Add scan/nidaq/focuslock sections if present
    if 'scan' in data:
        defaults['imswitch_defaults']['scan_defaults'] = data['scan']
    if 'nidaq' in data:
        defaults['imswitch_defaults']['nidaq'] = data['nidaq']
    if 'focusLock' in data:
        defaults['imswitch_defaults']['focus_lock'] = data['focusLock']
    
    return defaults


def generate_hardware_yaml(data: Dict[str, Any]) -> Dict[str, Any]:
    """Generate hardware.yaml (partial: specs null with comments)."""
    detectors = extract_devices(data, 'detectors')
    lasers = extract_devices(data, 'lasers')
    positioners = extract_devices(data, 'positioners')
    
    # Classify system architecture based on detector managers
    detector_managers = {d['manager'] for d in detectors}
    has_cameras = any('camera' in m.lower() or 'Manager' in m for m in detector_managers if m and 'APD' not in m and 'PMT' not in m)
    has_point_detectors = any('APD' in m or 'PMT' in m or 'SPAD' in m for m in detector_managers if m)
    
    image_formation = 'hybrid' if (has_cameras and has_point_detectors) else ('camera-based' if has_cameras else 'point-scanning')
    
    hardware = {
        'version': '1.0',
        'last_updated': str(date.today()),
        'microscope': {
            'name': None,  # fill from datasheet
            'type': None,  # fill from datasheet
            'stand': None,  # fill from datasheet
            'commercial_system': 'custom-built',  # or fill with commercial system name
            'objectives': [],
            'control_software': {
                'name': 'ImSwitch',
                'version': None,
            },
            'core_modalities': [],
        },
        'functional_blocks': {
            'illumination_sources': {},
        }
    }
    
    # Add lasers as illumination sources
    for laser in lasers:
        source_id = laser['id']
        hardware['functional_blocks']['illumination_sources'][source_id] = {
            'wavelength_nm': laser.get('wavelength'),
            'type': None,  # not in config — fill from datasheet (CW/pulsed/LED/etc.)
            'vendor': None,  # not in config — fill from datasheet
            'model': None,  # not in config — fill from datasheet
            'max_power_mW': None,  # not in config — fill from datasheet
            'pulse_width': None,  # not in config — fill from datasheet (or "CW")
            'repetition_rate': None,  # not in config — fill from datasheet (or null for CW)
            'purpose': None,  # not in config — fill from datasheet
        }
    
    # Add cameras if present
    if has_cameras:
        hardware['functional_blocks']['cameras'] = {}
        for detector in detectors:
            if detector['manager'] and 'camera' in detector['manager'].lower() or ('Manager' in detector['manager'] and 'APD' not in detector['manager'] and 'PMT' not in detector['manager']):
                camera_id = detector['id']
                hardware['functional_blocks']['cameras'][camera_id] = {
                    'type': None,  # not in config — fill from datasheet (sCMOS/EMCCD/CCD)
                    'vendor': None,  # not in config — fill from datasheet
                    'model': None,  # not in config — fill from datasheet
                    'pixel_size_um': None,  # not in config — fill from datasheet
                    'chip_size_px': [None, None],  # not in config — fill from datasheet
                    'quantum_efficiency_peak': None,  # not in config — fill from datasheet
                    'read_noise_e': None,  # not in config — fill from datasheet
                    'max_frame_rate_fps': None,  # not in config — fill from datasheet
                    'cooling': None,  # not in config — fill from datasheet
                    'notes': [],
                }
    
    # Add point detectors if present
    if has_point_detectors:
        hardware['functional_blocks']['point_detectors'] = {}
        for detector in detectors:
            if detector['manager'] and ('APD' in detector['manager'] or 'PMT' in detector['manager'] or 'SPAD' in detector['manager']):
                detector_id = detector['id']
                hardware['functional_blocks']['point_detectors'][detector_id] = {
                    'type': None,  # not in config — fill from datasheet (PMT/APD/SPAD/HyD/GaAsP)
                    'vendor': None,  # not in config — fill from datasheet
                    'model': None,  # not in config — fill from datasheet
                    'spectral_filters': {
                        'bandpass': None,  # not in config — fill from datasheet
                        'notch': None,  # not in config — fill from datasheet
                        'dichroic': None,  # not in config — fill from datasheet
                    },
                    'gating_capable': None,  # not in config — fill from datasheet
                    'photon_counting_capable': None,  # not in config — fill from datasheet
                    'notes': [],
                }
    
    # Add scanning if point detectors present
    if has_point_detectors:
        hardware['functional_blocks']['scanning'] = {
            'main_scanner': {
                'type': None,  # not in config — fill from datasheet (galvo/resonant/piezo)
                'vendor': None,  # not in config — fill from datasheet
                'model': None,  # not in config — fill from datasheet
                'max_scan_field_um': [None, None],  # not in config — fill from datasheet
                'max_line_rate_hz': None,  # not in config — fill from datasheet
                'bidirectional': None,  # not in config — fill from datasheet
            }
        }
    
    # Add filter configuration (always present)
    hardware['functional_blocks']['filter_configuration'] = {
        'type': None,  # not in config — fill from datasheet
        'elements': [],
    }
    
    # Add sample positioning
    hardware['functional_blocks']['sample_positioning'] = {
        'xy_stage': {
            'vendor': None,  # not in config — fill from datasheet
            'model': None,  # not in config — fill from datasheet
            'travel_mm': [None, None],  # not in config — fill from datasheet
            'motorized': True if positioners else None,
        },
        'z_focus': {
            'type': None,  # not in config — fill from datasheet (objective_piezo/stage_motor/voice_coil)
            'vendor': None,  # not in config — fill from datasheet
            'model': None,  # not in config — fill from datasheet
            'range_um': None,  # not in config — fill from datasheet
            'resolution_nm': None,  # not in config — fill from datasheet
        }
    }
    
    hardware['functional_blocks']['notes'] = [
        'This file was partially auto-seeded from ImSwitch config.',
        'All fields marked "# not in config" must be filled from datasheets/manuals.',
        'Run with --print-wave1-prompt to get the Wave 1 generation prompt.',
    ]
    
    return hardware


def generate_index_yaml(source_file: str) -> Dict[str, Any]:
    """Generate _index.yaml (starter stub)."""
    basename = Path(source_file).stem
    return {
        'knowledge_base': {
            'system': f'{basename} (FILL: full microscope name)',
            'family': None,  # fill during Wave 1
            'architecture': {
                'image_formation': None,  # fill during Wave 1 (widefield/point-scanning/hybrid/etc.)
                'resolution_regime': None,  # fill during Wave 1 (diffraction-limited/super-resolution-*/etc.)
            },
            'institution': None,  # fill during Wave 1
            'version': '1.0',
            'last_updated': str(date.today()),
            'modalities_available': [],  # fill during Wave 1
            'usage_instructions': 'FILL: 2-3 sentences for AI on how to use this KB.',
            'files': [
                {
                    'path': 'hardware.yaml',
                    'seeded': True,
                    'covers': 'Physical components and optical layout (partially auto-seeded)',
                    'query_types': ['hardware specs', 'component models', 'optical path'],
                },
                {
                    'path': 'imswitch_defaults.yaml',
                    'seeded': True,
                    'covers': 'ImSwitch configuration defaults (fully auto-seeded)',
                    'query_types': ['device names', 'channel wiring', 'default values'],
                },
                {
                    'path': 'software_config.yaml',
                    'seeded': True,
                    'covers': 'Control software device wiring and safety guidance (auto-seeded skeleton)',
                    'query_types': ['what can I change', 'safe parameter ranges'],
                },
                {
                    'path': 'concepts.yaml',
                    'seeded': False,
                    'covers': 'Modality explanations (generate via Wave 2 prompt)',
                    'query_types': ['how does X work', 'what is Y'],
                },
                {
                    'path': 'calibrations.yaml',
                    'seeded': False,
                    'covers': 'Power, position, and calibration data (generate via Wave 2 prompt)',
                    'query_types': ['calibration values', 'power measurements'],
                },
                {
                    'path': 'procedures.yaml',
                    'seeded': False,
                    'covers': 'Startup, shutdown, alignment, maintenance (generate via Wave 2 prompt)',
                    'query_types': ['how to start up', 'alignment procedure', 'maintenance schedule'],
                },
                {
                    'path': 'safety.yaml',
                    'seeded': False,
                    'covers': 'All safety information (generate via Wave 2 prompt)',
                    'query_types': ['safety hazards', 'PPE required', 'emergency procedures'],
                },
                {
                    'path': 'troubleshooting.yaml',
                    'seeded': False,
                    'covers': 'Symptom → fix workflows (generate via Wave 2 prompt)',
                    'query_types': ['problem X', 'not working', 'artifacts'],
                },
                {
                    'path': 'limits.yaml',
                    'seeded': False,
                    'covers': 'Performance envelope (generate via Wave 2 prompt)',
                    'query_types': ['maximum X', 'resolution', 'speed'],
                },
                {
                    'path': 'recipes.yaml',
                    'seeded': False,
                    'covers': 'Complete experiment configurations (generate via Wave 2 prompt)',
                    'query_types': ['settings for X', 'recipe for Y sample'],
                },
                {
                    'path': 'faq.yaml',
                    'seeded': False,
                    'covers': 'Common questions (generate via Wave 2 prompt)',
                    'query_types': ['can I do X', 'what dyes work'],
                },
            ],
        }
    }


def print_wave1_prompt(hardware_yaml_path: Path):
    """Print the Wave 1 generation prompt with hardware.yaml pre-pasted."""
    with open(hardware_yaml_path, 'r') as f:
        hardware_content = f.read()
    
    prompt = '''You are a microscopy instrumentation expert creating a structured, AI-readable
knowledge base for a microscope system. I have auto-seeded hardware.yaml from an
ImSwitch config. Please complete it by filling all null fields from datasheets/manuals.

## STEP 1: REVIEW AUTO-SEEDED CONTENT

The following hardware.yaml was auto-extracted from ImSwitch configuration JSON.
All device IDs, wavelengths, and channel assignments are correct. However, all
physical specifications (vendor, model, power, filter specs, etc.) are marked null
because they are not in the software config.

--- BEGIN hardware.yaml (auto-seeded) ---
'''
    prompt += hardware_content
    prompt += '''
--- END hardware.yaml ---

## STEP 2: FILL NULL FIELDS

For every field marked "# not in config — fill from datasheet", provide the actual
value from:
- Datasheets / manuals (if you have them)
- Publications describing this system
- Turn-on procedures or internal documentation
- Direct measurement (e.g., power meter readings)

If you genuinely don't have a value, keep it as `null` and add a comment:
`# not specified in provided sources`

## STEP 3: CLASSIFY THE SYSTEM

After completing hardware.yaml, state:

1. **Architecture**: camera-based / point-scanning / spinning-disk / light-sheet / hybrid
2. **Resolution regime**: diffraction-limited / super-resolution (deterministic) / super-resolution (stochastic)
3. **Modality families present**: (list from schema — e.g., confocal, STED, widefield, etc.)

## RULES

1. **ID stability**: Do NOT change any device IDs — they are already cross-referenced
   by imswitch_defaults.yaml and software_config.yaml.
2. **Explicit nulls for unknowns**: Use `null` + comment, never guess.
3. **Units always explicit**: Include units in field names or companion `unit` fields.
4. **No AI artifacts**: Clean YAML only — no citation markers.

Respond with the completed hardware.yaml.
'''
    
    print(prompt)


def main():
    parser = argparse.ArgumentParser(
        description='Extract ImSwitch setup JSON to ScopeAId KB seed files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('input_json', help='Path to ImSwitch setup JSON file')
    parser.add_argument('-o', '--output', default='./microscope-kb',
                        help='Output directory (default: ./microscope-kb/)')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing output files')
    parser.add_argument('--print-wave1-prompt', action='store_true',
                        help='Print Wave 1 generation prompt with hardware.yaml pre-pasted')
    parser.add_argument('--strict', action='store_true',
                        help='Error on any dropped fields (default: warn and continue)')
    
    args = parser.parse_args()
    
    # Load input JSON
    input_path = Path(args.input_json)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    
    with open(input_path, 'r') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)
    
    # Validate basic structure
    if not any(k in data for k in ['detectors', 'lasers', 'positioners']):
        print("ERROR: Input JSON does not look like an ImSwitch setup config", file=sys.stderr)
        print("Expected at least one of: detectors, lasers, positioners", file=sys.stderr)
        sys.exit(1)
    
    # Create output directory
    output_dir = Path(args.output)
    if output_dir.exists() and not args.force:
        # Check if any of our target files exist
        target_files = ['hardware.yaml', 'imswitch_defaults.yaml', 'software_config.yaml', '_index.yaml']
        existing = [f for f in target_files if (output_dir / f).exists()]
        if existing:
            print(f"ERROR: Output directory exists and contains: {', '.join(existing)}", file=sys.stderr)
            print(f"Use --force to overwrite.", file=sys.stderr)
            sys.exit(1)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate files
    print(f"Extracting from: {input_path}")
    print(f"Output directory: {output_dir}")
    
    software_config = generate_software_config(data, str(input_path))
    imswitch_defaults = generate_imswitch_defaults(data, str(input_path))
    hardware = generate_hardware_yaml(data)
    index = generate_index_yaml(str(input_path))
    
    # Write files
    files_written = []
    
    for filename, content in [
        ('software_config.yaml', software_config),
        ('imswitch_defaults.yaml', imswitch_defaults),
        ('_index.yaml', index),
    ]:
        output_path = output_dir / filename
        with open(output_path, 'w') as f:
            yaml.dump(content, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        files_written.append(filename)
        print(f"  ✓ {filename}")
    
    # Write hardware.yaml with inline comments for null fields
    # (PyYAML doesn't support comments, so we post-process the output)
    hardware_path = output_dir / 'hardware.yaml'
    with open(hardware_path, 'w') as f:
        yaml.dump(hardware, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    # Add comments to null fields
    with open(hardware_path, 'r') as f:
        lines = f.readlines()
    
    with open(hardware_path, 'w') as f:
        for line in lines:
            # Add comment after null values (except for known-empty fields like notes: [])
            if line.strip().endswith(': null') and not any(x in line for x in ['objectives:', 'version:', 'name:', 'system:', 'family:']):
                f.write(line.rstrip() + '  # not in config — fill from datasheet\n')
            elif line.strip() == '- null':
                f.write(line.rstrip() + '  # not in config — fill from datasheet\n')
            else:
                f.write(line)
    
    files_written.append('hardware.yaml')
    print(f"  ✓ hardware.yaml")
    
    print(f"\nSuccess! Generated {len(files_written)} seed files.")
    print(f"\nNext steps:")
    print(f"1. Review {output_dir}/hardware.yaml and fill null fields from datasheets")
    print(f"2. Run with --print-wave1-prompt to get the Wave 1 generation prompt")
    print(f"3. Paste the prompt + your source docs into Claude/ChatGPT")
    print(f"4. Review and save the completed hardware.yaml")
    print(f"5. Use the Wave 2 prompt (see docs/microscope-kb/prompts/generation-prompt.md)")
    
    # Print Wave 1 prompt if requested
    if args.print_wave1_prompt:
        print("\n" + "="*80)
        print_wave1_prompt(output_dir / 'hardware.yaml')


if __name__ == '__main__':
    main()
