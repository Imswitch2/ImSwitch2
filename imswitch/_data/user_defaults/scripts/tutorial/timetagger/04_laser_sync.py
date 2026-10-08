"""Tutorial timetagger 04 -- The laser sync: repetition rate and period jitter.

You will learn
  * why the phasor fit needs the laser's repetition rate exactly, and why
    the histogram window is one laser period
  * ``rep_rate()``: how to measure the rate safely on any card -- the sync
    is divided and the conditional filter is taken off for the measurement,
    because an 80 MHz sync would flood a Time Tagger 20's USB link
  * what the measured period jitter can and cannot tell you: below the
    card's own timing jitter it is the card, not the laser

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: an 80.000 MHz laser sync with 20 ps of jitter on a card
                with 8 ps of its own.
  Your own microscope: the same block; the measurement holds the card for
                its duration, so no scan can start meanwhile.

Next: 05_bandwidth_and_the_filter.py
"""

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
APPLY = False

configured = (api.imcontrol.getDetectorParameter(DETECTOR, 'laser_rep_rate_mhz')
              if DETECTOR else None)
print(f'configured laser_rep_rate_mhz: {configured}')

# rep_rate() runs a calibration transaction: filter off, sync divided by 16
# (5 M tags/s instead of 80 M on a Time Tagger 20), count for two seconds,
# take the period histogram, put everything back -- also if you press Stop.
rep = tt.rep_rate(duration_s=2.0, divider=16)
print(rep.summary())
print()
print(f'measured: {rep.rate_hz / 1e6:.5f} MHz, period {rep.period_ps / 1000:.4f} ns')
if rep.card_limited:
    print(f'period jitter {rep.jitter_ps:.0f} ps RMS is at the card\'s floor '
          f'({rep.card_floor_ps:.0f} ps): the laser is at least this good; the '
          f'number says nothing more about it.')
else:
    print(f'period jitter {rep.jitter_ps:.0f} ps RMS over {rep.divider} periods -- '
          f'above the card\'s {rep.card_floor_ps:.0f} ps floor, so this is the laser.')
print()

# Why it matters: the phasor fit projects the decay onto sin and cos of
# 2 pi f_rep t. A wrong f_rep rotates every phasor and shifts every lifetime.
# And the histogram window defaults to one period of this rate: too short a
# window cuts the decay off and reads as a short lifetime.
if configured is not None:
    off = abs(rep.rate_hz / 1e6 - float(configured)) / float(configured)
    if off <= 0.005:
        print(f'Configured and measured agree to {100 * off:.2f} %. Nothing to do.')
    else:
        print(f'Configured {configured} MHz is {100 * off:.1f} % off the measured rate.')
        if APPLY:
            api.imcontrol.setDetectorParameter(DETECTOR, 'laser_rep_rate_mhz',
                                               round(rep.rate_hz / 1e6, 4))
            print(f'Applied: {DETECTOR}.laser_rep_rate_mhz = {rep.rate_hz / 1e6:.4f}')
        else:
            print(f'Not applied (APPLY is False). Set {DETECTOR}.laser_rep_rate_mhz = '
                  f'{rep.rate_hz / 1e6:.4f}.')
