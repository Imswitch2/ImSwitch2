"""Tutorial timetagger 01 -- Meet the card: roles, channels, the test signal.

You will learn
  * how a script reaches the Swabian Time Tagger: the ``time_tagger`` part
    of the workflow facade
  * that every input is named by its *role* (photons, laser_sync,
    line_clock, ...) and what the setup's ``timeTagger`` block maps each
    role to: channel, edge, trigger level
  * ``count_rates()``: what every input is counting right now
  * the card's built-in test signal: a cable-free check that an input and
    the whole counting path work

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a point-scanning FLIM microscope -- galvo mirrors and a
                Z piezo on an NI-DAQ card, an APD, two lasers, and a
                Swabian Time Tagger with a photon detector (a SPAD whose
                pulses are negative), an 80 MHz laser sync and the scan's
                line and frame clocks. A synthetic sample sits under it.
  Your own microscope: a setup with a top-level "timeTagger" block (see the
                Time Tagger chapter of the documentation). Roles and
                channels then come from your block.

How to run it
  Read the script first and guess what it will print. Then press Run all
  and compare with the Output panel. tutorial/README.md explains how to
  load the setup named above.

Next: 02_trigger_levels_and_dead_time.py
"""

# The facade bundles the devices a workflow needs; its time_tagger part is
# the card. On a setup without a "timeTagger" block it is None.
facade = api.imcontrol.buildWorkflowFacade()
tt = facade.time_tagger
if tt is None:
    raise RuntimeError('This setup has no "timeTagger" block; load '
                       'galvo_flim_mock_scan_setup.json for this tutorial.')

print(f'Card: {tt.model}, serial {tt.serial}'
      + (' -- a simulated card' if tt.is_mock else ''))
print(f'TCSPC direction: {tt.tcspc_direction}  (forward = the laser sync '
      f'starts the clock and the photon stops it)')
print()

# Every input the setup uses has a role. The channel number carries the
# edge: a negative number means the falling edge of that input, which is
# how a SPAD's negative NIM pulse is counted.
print('Roles on this card:')
for role, c in tt.channels().items():
    print(f'  {role:11s} channel {c.channel:+d} ({c.edge} edge), '
          f'trigger {c.trigger_v:+.2f} V, dead time {c.deadtime_ps / 1000:g} ns, '
          f'delay {c.delay_ps} ps')
print()

# What is every input counting, right now, for one second? The sync should
# be the laser's repetition rate (80 MHz here). The scan clocks are 0 Hz:
# they only fire while a scan runs. The photons are the sample under the
# parked beam.
rates = tt.count_rates(duration_s=1.0)
print(rates.summary())
print()

# The card can feed its own test signal (a fixed rate, about 850 kHz on a
# Time Tagger 20) into any input, in place of the cable. An input that counts
# the test signal but not your signal has a cabling or trigger-level problem,
# not a card problem. Always switch it off again: a test signal left on
# counts into your next scan.
tt.test_signal(['line_clock'], True)
try:
    with_test = tt.count_rates(['line_clock'], duration_s=0.5)
    print(f'line_clock with the test signal: {with_test.rates_hz["line_clock"]:,.0f} Hz')
finally:
    tt.test_signal(['line_clock'], False)
without = tt.count_rates(['line_clock'], duration_s=0.5)
print(f'line_clock without it:           {without.rates_hz["line_clock"]:,.0f} Hz '
      f'(no scan is running, so 0 is right)')
print()
print('Next: 02 finds the right trigger level for every input.')
