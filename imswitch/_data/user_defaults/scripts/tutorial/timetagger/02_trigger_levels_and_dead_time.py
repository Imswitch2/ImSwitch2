"""Tutorial timetagger 02 -- Trigger levels and dead time.

You will learn
  * why a trigger level is the first thing to get right: too low counts
    noise, too high counts nothing, the wrong sign counts nothing at all
  * ``trigger_sweep()``: the count rate at each of a series of levels, and
    the *plateau* where every pulse is counted
  * that the card's inputs are 50 ohm and a DAQ line drives only about
    1 V into them, so the clock thresholds stay low
  * dead time: how a detector's own dead time stops ringing from counting
    twice
  * the APPLY pattern of these tutorials: results are printed as
    suggestions and only written to the card when APPLY is True

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a SPAD with negative pulses of about -0.5 V on the photons
                input, a +0.8 V laser sync, and scan clocks of about
                +1.2 V, each behind a comparator with a plateau.
  Your own microscope: the same block; put the levels to sweep in the
                polarity of each input's pulses (negative for a SPAD, PMT
                or any NIM output; positive for a TTL clock).

Next: 03_dark_counts_and_afterpulsing.py
"""

import numpy as np

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
helpers.describeCard(tt)
print()

# Change to True to write the suggested levels to the card. They hold until
# ImSwitch restarts; to keep them, copy them into the timeTagger block of
# your setup file (the line printed at the end).
APPLY = False

# The sweep runs inside a *calibration transaction*: while it owns the card
# no scan can start on its temporary levels, and the original level is put
# back when it is done -- also if you press Stop.
suggestions = {}
for role, levels in (('photons', np.linspace(-0.9, -0.05, 18)),
                     ('laser_sync', np.linspace(0.05, 1.2, 24))):
    sweep = tt.trigger_sweep(role, levels, duration_s=0.1)
    print(f'{role}:')
    helpers.printTable(
        [(f'{v:+.2f} V', f'{r:,.0f}') for v, r in zip(sweep.levels_v, sweep.rates_hz)],
        header=('trigger', 'counts/s'))
    if sweep.plateau_v is None:
        print(f'  nothing counted at any level: check the cable, and the sign of '
              f'the levels against the pulse polarity of this input')
    else:
        print(f'  plateau around {sweep.plateau_v:+.2f} V '
              f'(current setting {sweep.restored_v:+.2f} V)')
        suggestions[role] = sweep.plateau_v
    print()

# The scan clocks are DAQ lines. Into the card's 50 ohm input a DAQ output
# drives roughly 1 to 1.5 V, not 3.3 or 5 V: a threshold of 1.5 V may never
# trigger, which shows up as "line 0 Hz" during a scan. Their sweep needs a
# running scan (tutorial 07); the rule here is: keep clock thresholds around
# 0.5 V unless a buffer is in the path.
print('Scan clocks (line_clock, frame_clock) only fire during a scan; tutorial 07 '
      'sweeps them. Keep their thresholds near +0.5 V: a DAQ line into 50 ohm '
      'is about 1 V.')
print()

# Dead time: after a pulse the input ignores further edges for this long.
# Set it to the detector's own dead time (a SPAD: 20-100 ns) and the
# ringing on the trailing edge of a pulse, which can trigger the comparator
# a second time, is not counted as a second photon.
DEADTIME_NS = 50
print(f'Suggested dead time on photons: {DEADTIME_NS} ns (the SPAD\'s own).')

if suggestions:
    print()
    if APPLY:
        for role, level in suggestions.items():
            tt.set_trigger_level(role, level)
        tt.set_deadtime('photons', DEADTIME_NS * 1000)
        print('Applied to the card (until restart).')
    else:
        print('Not applied (APPLY is False). To keep them, put into the timeTagger block:')
    print('  "photonsTriggerV": %.2f, "laserSyncTriggerV": %.2f, "photonsDeadtimePs": %d'
          % (suggestions.get('photons', tt.channels()['photons'].trigger_v),
             suggestions.get('laser_sync', tt.channels()['laser_sync'].trigger_v),
             DEADTIME_NS * 1000))
