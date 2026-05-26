"""Measure the laser repetition rate on a Swabian Time Tagger channel.

Usage:
    python measure_laser_rep_rate.py [--channel N] [--trigger-level V] [--duration S]

Hooks a TimeTagger.Countrate onto the channel that carries the laser sync
pulse (the same `start_channel` you'd configure for the FLIM detector),
integrates for the requested duration, and prints the measured rate in MHz.

Plug the printed value into `laser_rep_rate_mhz` in the FLIM detector
config (or set it from the GUI parameter panel) so the phasor fit uses
the correct ω = 2π · f_rep.
"""
import argparse
import time

import TimeTagger


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--channel', type=int, default=3,
                   help='Channel carrying the laser sync signal (default: 3)')
    p.add_argument('--trigger-level', type=float, default=0.5,
                   help='Trigger threshold in volts (default: 0.5)')
    p.add_argument('--duration', type=float, default=2.0,
                   help='Integration time in seconds (default: 2.0)')
    args = p.parse_args()

    tt = TimeTagger.createTimeTagger()
    try:
        tt.setTriggerLevel(args.channel, args.trigger_level)

        cr = TimeTagger.Countrate(tt, [args.channel])
        cr.startFor(int(args.duration * 1e12))  # picoseconds
        cr.waitUntilFinished()
        rate_hz = float(cr.getData()[0])

        if rate_hz <= 0:
            print(f'No edges detected on channel {args.channel}. '
                  f'Check cabling and trigger level.')
            return

        period_ns = 1e9 / rate_hz
        print(f'Channel {args.channel} @ {args.trigger_level} V:')
        print(f'  rate   = {rate_hz / 1e6:.6f} MHz')
        print(f'  period = {period_ns:.4f} ns')
        print()
        print(f'Set laser_rep_rate_mhz = {rate_hz / 1e6:.4f}')
    finally:
        TimeTagger.freeTimeTagger(tt)


if __name__ == '__main__':
    main()
