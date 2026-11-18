"""Write the polarization rig's scans for the Expert panel.

The rig's old Microscope_GUI modes -- live Wide Field, live Galvo Scan, Wide
Field 2D, Line Scan 2D, and the dark frames the Line Scan took first -- as
scan files the Expert panel opens (Load) and a script loads
(``loadScanParamsFromFile``). They are built from the rig's setup, whose
galvos' ``conversionFactor`` turns the old GUI's volts into the programs'
µm, and from the old GUI's saved values, so that each scan plays what the old
GUI played (``imswitch/imcontrol/model/scan_program/legacy_microscope_gui.py``
says how, and what differs on purpose).

Usage::

    python tools/polarization_rig_scans.py --setup <the rig's setup .json>
        [--panel <the old GUI's saved spin boxes, its config's "ConWidget Path">]
        [--timing <the old GUI's "Digital Timing Presets" file>]
        [--pairs 5] [--galvo-step-samples 2] [--out <folder>]

Without ``--panel`` and ``--timing`` the old panel's defaults are used. The
scans go to ``<ImSwitchConfig>/imcontrol_scans/polarization_rig`` unless
``--out`` names a folder. Run it again whenever the setup's conversion
factors or the old values change.
"""

import argparse
import json
import sys
from pathlib import Path


def _read(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--setup', required=True,
                        help="the rig's setup file: the galvos and their conversionFactor")
    parser.add_argument('--panel', help="the old GUI's saved spin boxes (ConWidget Path)")
    parser.add_argument('--timing', help="the old GUI's Digital Timing Presets file")
    parser.add_argument('--pairs', type=int, default=5,
                        help='pairs of frames (Wide Field 2D) or passes (Line Scan 2D)')
    parser.add_argument('--galvo-step-samples', type=int, default=2,
                        help='old samples (4 µs) per Galvo Scan step')
    parser.add_argument('--out', help='the folder to write to')
    args = parser.parse_args(argv)

    from imswitch.imcontrol.model.scan_program import legacy_microscope_gui as lm

    wiring = lm.Wiring.from_setup(_read(args.setup))
    scans = lm.rig_scans(wiring, panel=_read(args.panel) if args.panel else None,
                         timing_presets=_read(args.timing) if args.timing else None,
                         pairs=args.pairs, galvo_step_samples=args.galvo_step_samples)
    if args.out:
        out = Path(args.out)
    else:
        from imswitch.imcommon.model import dirtools
        out = Path(dirtools.UserFileDirs.Root) / 'imcontrol_scans' / 'polarization_rig'
    out.mkdir(parents=True, exist_ok=True)
    for name, state in scans.items():
        path = out / f'{name}.json'
        path.write_text(json.dumps(state, indent=2), encoding='utf-8')
        print(path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
