"""Tutorial timetagger 02 -- Trigger levels and dead time.

You will learn
  * why a trigger level is the first thing to get right: too low counts
    noise, too high counts nothing, the wrong sign counts nothing at all
  * the *plateau*: the range of levels over which an input counts the same
    rate, because every pulse clears the level and no noise does; a good
    level sits in its middle
  * ``find_trigger_level()``: one call per input that sweeps the levels for
    you -- polarity from the channel's sign, a coarse sweep from 20 mV to
    the input's 2.5 V, a fine one around the plateau's edges -- and says
    what it found (a verdict, and what to do about it)
  * that the card's inputs are 50 ohm, so a DAQ line drives only about
    1 to 1.5 V into them and the clock thresholds stay near 0.5 V
  * dead time: how a detector's own dead time stops ringing from counting
    twice
  * the APPLY pattern of these tutorials: results are printed as
    suggestions and only written to the card when APPLY is True

Which level for which input, by kind of signal
  detector pulses   a SPAD's or hybrid detector's NIM pulse is about -0.4
                    to -0.8 V into 50 ohm: falling edge (a negative channel
                    number), a negative level around -0.2 to -0.3 V
  laser sync        either polarity, depending on the laser: a NIM sync is
                    negative, a driver's TTL-like sync about +0.5 to +1 V, a
                    photodiode sync can be under 0.1 V
  DAQ clocks        the scan's line and frame clocks: rising edge, about
                    +1 to +1.5 V into 50 ohm, so about +0.5 V and never
                    above 1 V; they fire only while a scan runs
  other TTL         whatever its source drives into 50 ohm: the plateau
                    tells

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a SPAD with negative pulses of about -0.5 V on the photons
                input, a +0.8 V laser sync, and scan clocks of about
                +1.2 V, each behind a comparator with a plateau.
  Your own microscope: the same calls, on the inputs your timeTagger block
                configures -- a card that only counts photons has just the
                photons input, and that is all this tutorial looks at.

Next: 03_dark_counts_and_afterpulsing.py
"""

import numpy as np

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
helpers.describeCard(tt)
print()

# Change to True to write the levels found to the card. They hold until
# ImSwitch restarts; to keep them, copy the printed lines into the
# timeTagger block of your setup file (or use the Lifetime widget's Signals
# panel: Find, then "Apply and keep in setup file").
APPLY = False

roles = tt.roles()

# 1. What a sweep looks like. trigger_sweep() counts at the levels you
#    give it -- here the photon input's, in its polarity. Read the table:
#    the counts climb out of the comparator's floor near 0 V, stay flat
#    over a range (the plateau: every pulse counted, no noise), and drop
#    once the level is above the pulses. The sweep runs inside a
#    *calibration transaction*: no scan can start on its temporary levels,
#    and the original level is put back when it is done -- also on Stop.
if 'photons' in roles:
    sign = -1.0 if tt.channels()['photons'].channel < 0 else 1.0
    sweep = tt.trigger_sweep('photons', sign * np.linspace(0.05, 0.9, 18), duration_s=0.1)
    print('photons, swept by hand:')
    helpers.printTable(
        [(f'{v:+.2f} V', f'{r:,.0f}') for v, r in zip(sweep.levels_v, sweep.rates_hz)],
        header=('trigger', 'counts/s'))
    print(f'  {sweep.summary().split("; ")[-1]}')
    print()

# 2. The same, without choosing anything: find_trigger_level() on every
#    configured input. It takes the polarity from the sign of the channel
#    number (and probes the other sign when nothing counts), sweeps from
#    20 mV to 2.5 V, places the plateau's edges with finer steps, and
#    returns the plateau's middle with a verdict:
#      good            a wide plateau: use the level
#      too_small       the pulses barely clear the noise: no safe level
#      wrong_polarity  the pulses have the other sign: flip the channel's sign
#      noise_only      counts only next to 0 V: noise, or pulses under ~0.1 V
#      no_signal       nothing at any level: cable, input number, source off
#      needs_scan      a scan clock between scans: it fires only in a scan
#    The level is always put back afterwards; nothing is applied here.
found = {}
for role in roles:
    result = tt.find_trigger_level(role, duration_s=0.1)
    print(result.summary())
    found[role] = result
print()

# The scan clocks (line_clock, frame_clock) read needs_scan here: they are
# DAQ lines that fire only while a scan runs. Into the card's 50 ohm input
# a DAQ output drives roughly 1 to 1.5 V, not 3.3 or 5 V, so a threshold of
# 1.5 V may never trigger ("line 0 Hz" during a scan). Keep them near
# +0.5 V; tutorial 07 checks the line clock during a scan, and the Signals
# panel's Find works on it while an APD-only scan repeats.

# 3. Dead time: after a pulse the input ignores further edges for this
#    long. Set it to the detector's own dead time (a SPAD: 20-100 ns) and
#    the ringing on the trailing edge of a pulse, which can trigger the
#    comparator a second time, is not counted as a second photon.
DEADTIME_NS = 50
if 'photons' in roles:
    print(f'Suggested dead time on photons: {DEADTIME_NS} ns (the SPAD\'s own).')

applicable = {role: r for role, r in found.items()
              if r.verdict == 'good' and r.level_v is not None}
if applicable:
    print()
    if APPLY:
        for role, result in applicable.items():
            tt.set_trigger_level(role, result.level_v)
        if 'photons' in roles:
            tt.set_deadtime('photons', DEADTIME_NS * 1000)
        print('Applied to the card (until restart).')
    else:
        print('Not applied (APPLY is False). To keep them, put into the timeTagger block:')
    lines = [r.block_line() for r in applicable.values()]
    if 'photons' in roles:
        lines.append(f'"photonsDeadtimePs": {DEADTIME_NS * 1000}')
    print('  ' + ', '.join(lines))
