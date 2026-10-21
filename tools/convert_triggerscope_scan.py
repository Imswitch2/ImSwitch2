"""Turn an old TriggerScope scan file (the pLS-RESOLFT or raster widget's) into
a scan file the Expert panel loads, and check it against what the old
firmware did for that file.

    python tools/convert_triggerscope_scan.py SCAN [SCAN ...] --out DIR [--map Orca=OrcaFusion]
                                              [--camera OrcaFusion]

Each SCAN becomes DIR/<name>.json. ``--map`` renames a device the file names
to the setup's name for it (repeatable); ``--camera`` binds a camera to a
raster, which named none. The output says, per file, what the program keeps
exactly and what it cannot (the notes), and whether every frame of the old
scan is in the program (the check). A file the model cannot hold exactly is
refused with the reason and makes the exit code 1.
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('scans', nargs='+', help='old scan files')
    parser.add_argument('--out', required=True, help='the directory the programs are written to')
    parser.add_argument('--map', action='append', default=[], metavar='OLD=NEW',
                        help='rename a device the file names (repeatable)')
    parser.add_argument('--camera', help='the camera a raster triggers (it named none)')
    parser.add_argument('--no-check', action='store_true',
                        help='skip the frame-by-frame check against the old firmware')
    args = parser.parse_args(argv)

    from imswitch.imcontrol.model.scan_program.legacy_triggerscope import (
        LegacyScanError, compare, convert_legacy_scan, read_legacy_scan,
    )
    from imswitch.imcontrol.model.scan_program.serialize import program_to_dict

    device_map = {}
    for item in args.map:
        old, sep, new = item.partition('=')
        if not sep or not old or not new:
            parser.error(f'--map takes OLD=NEW, not {item!r}')
        device_map[old] = new
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for path in args.scans:
        name = Path(path).stem
        print(f'== {name}')
        try:
            legacy = read_legacy_scan(path)
            conversion = convert_legacy_scan(legacy, device_map=device_map, camera=args.camera)
        except LegacyScanError as error:
            print(f'   REFUSED: {error}')
            failed += 1
            continue
        for note in conversion.notes:
            print(f'   note: {note}')
        if not args.no_check:
            problems = compare(legacy, conversion, device_map=device_map, camera=args.camera)
            if problems:
                failed += 1
                print('   CHECK FAILED:')
                for problem in problems:
                    print(f'     - {problem}')
            else:
                print('   check: every frame of the old scan is in the program.')
        program = conversion.program
        state = {
            'controller': 'ScanControllerExpert',
            'scanWidgetType': 'Expert',
            'program': program_to_dict(program),
            'timingPolicy': program.timing_policy,
            'mode': {'repeatEnabled': False},
            'convertedFrom': {'file': str(path), 'kind': legacy.kind, 'notes': conversion.notes},
        }
        target = out / f'{name}.json'
        target.write_text(json.dumps(state, indent=2), encoding='utf-8')
        steps = 1
        for loop in program.loops:
            steps *= loop.count
        print(f'   -> {target}  ({" x ".join(f"{l.id}:{l.count}" for l in program.loops)} = '
              f'{steps} steps of {program.step_s * 1e3:g} ms)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
