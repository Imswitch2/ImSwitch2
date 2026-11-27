"""Measure the laser repetition rate on a Swabian Time Tagger, without ImSwitch's GUI.

Usage:
    python measure_laser_rep_rate.py [--channel N] [--trigger-level V] [--duration S]
                                     [--divider N] [--photon-channel N] [--filtered]

The same measurement ``facade.time_tagger.rep_rate()`` makes from a script
(tutorial timetagger/04): the sync is divided by ``--divider`` so an 80 MHz
sync fits a Time Tagger 20's USB budget, the conditional filter is off for
the measurement, and the card is put back afterwards. Prints the rate, the
period and the period jitter with the card's own floor, and the value to put
into the FLIM detector's ``laser_rep_rate_mhz``.

Pass ``--filtered --photon-channel N`` when the rig runs with the conditional
filter on (``filterSyncByPhotons``), so the filter is restored afterwards.
"""
import argparse

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.workflows.time_tagger_facade import TimeTaggerFacade


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--channel', type=int, default=3,
                   help='Input carrying the laser sync (default: 3; negative = falling edge)')
    p.add_argument('--trigger-level', type=float, default=0.5,
                   help='Trigger threshold in volts (default: 0.5)')
    p.add_argument('--duration', type=float, default=2.0,
                   help='Integration time in seconds (default: 2.0)')
    p.add_argument('--divider', type=int, default=16,
                   help='Event divider on the sync during the measurement (default: 16)')
    p.add_argument('--photon-channel', type=int, default=1,
                   help='Input carrying the photons; only used with --filtered (default: 1)')
    p.add_argument('--filtered', action='store_true',
                   help='The rig runs the conditional filter (restored afterwards)')
    p.add_argument('--simulation', action='store_true',
                   help='Use the in-process mock card instead of hardware')
    args = p.parse_args()

    info = TimeTaggerInfo(
        simulation=args.simulation,
        photonsChannel=args.photon_channel,
        laserSyncChannel=args.channel,
        laserSyncTriggerV=args.trigger_level,
        filterSyncByPhotons=args.filtered,
    )
    manager = TimeTaggerManager(info)
    try:
        if not manager.connected:
            print('No Time Tagger: the vendor library is missing or the card is not on '
                  'the USB (use --simulation for the mock).')
            return
        result = TimeTaggerFacade(manager).rep_rate(
            duration_s=args.duration, divider=args.divider,
        )
    finally:
        manager.finalize()

    print(f'Channel {args.channel} @ {args.trigger_level} V:')
    print(f'  rate   = {result.rate_hz / 1e6:.6f} MHz')
    print(f'  period = {result.period_ps / 1000:.4f} ns')
    print(f'  jitter = {result.jitter_ps:.0f} ps RMS over {result.divider} periods'
          + (f' (card-limited, floor {result.card_floor_ps:.0f} ps)'
             if result.card_limited else ''))
    print()
    print(f'Set laser_rep_rate_mhz = {result.rate_hz / 1e6:.4f}')


if __name__ == '__main__':
    main()
