"""Put a board that speaks the step-engine protocol through the conformance
programs and say, program by program, whether it does what the scan model
means (docs/devices/step-engine-protocol.rst, "Testing a firmware").

    python tools/step_board_conformance.py --port COM5 --baudrate 115200 --reply-timeout 0.5
    python tools/step_board_conformance.py --port COM5 ... --trigger    # pulse its trigger input
    python tools/step_board_conformance.py --simulated                   # the reference board

The board needs a DAC and three lines. The programs run on its first DAC and
first three lines, at 1 µm per volt; nothing else may be wired to them. The
exit code is 0 when every program passes.
"""

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--port', help='the board\'s serial port')
    parser.add_argument('--baudrate', type=int, help='the link\'s rate')
    parser.add_argument('--reply-timeout', type=float,
                        help='seconds a command waits for its answer')
    parser.add_argument('--trigger', action='store_true',
                        help='also run trigger waits (something must pulse the trigger input)')
    parser.add_argument('--simulated', action='store_true',
                        help='run on the simulated reference board instead')
    parser.add_argument('--grid-ns', type=int,
                        help='the simulated board\'s grid (with --simulated)')
    args = parser.parse_args(argv)

    from imswitch.imcontrol.model.managers.scan_engines import conformance_runner
    from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
        SerialTransport,
    )
    if args.simulated:
        if args.grid_ns is None:
            parser.error('--simulated needs --grid-ns, the simulated board\'s grid')
        transport = conformance_runner.simulated_transport(args.grid_ns)
    else:
        missing = [name for name in ('port', 'baudrate', 'reply_timeout')
                   if getattr(args, name) is None]
        if missing:
            parser.error('a board needs --' + ', --'.join(m.replace('_', '-') for m in missing))
        transport = SerialTransport(args.port, args.baudrate, args.reply_timeout)
    try:
        results = conformance_runner.run(transport, trigger=args.trigger)
    finally:
        transport.close()
    failed = [r.program for r in results if not r.passed]
    print(f'{len(results) - len(failed)} of {len(results)} programs pass.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
