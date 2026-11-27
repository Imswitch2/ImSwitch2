"""Tutorial timetagger 03 -- Dark counts: the background every fit must know.

You will learn
  * that a photon detector counts without light (dark counts) and after
    light (afterpulses), and that both are flat over the laser period
  * why that matters: a flat background pulls the moment lifetime towards
    half the period and the phasor towards the origin
  * ``dark_rates()``: the count rate with the excitation blocked
  * where the number goes: the FLIM detector's ``background_rate_hz``,
    which is subtracted from every pixel's histogram before fitting

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a SPAD with 2 kHz of dark counts and 2 % afterpulsing on a
                500 kHz photon stream from the parked beam. The simulated
                card cannot see the setup's lasers, so the script blocks
                its light through set_mock_laser(); on a real card that
                call does nothing.
  Your own microscope: a real card sees the real lasers: the script turns
                every laser that is on off through the Laser widget's API
                and puts back exactly the states it found afterwards (a
                laser that was off stays off). Block the excitation by hand
                instead if a laser is not under ImSwitch's control.

Next: 04_laser_sync.py
"""

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()   # 'FLIM' on the mock setup
APPLY = False

# With the light on, for reference.
lit = tt.count_rates(['photons'], duration_s=1.0).rates_hz['photons']
print(f'photons with excitation: {lit:,.0f} Hz')

# Block the excitation: every laser that is on goes off, and the mock's
# light off. Read the states first, so that only those are put back: a
# laser that was off must not come on because this script ran.
lasers = api.imcontrol.getLaserNames()
wasOn = {name: bool(api.imcontrol.getLaserActive(name)) for name in lasers}
for name in lasers:
    if wasOn[name]:
        api.imcontrol.setLaserActive(name, False)
tt.set_mock_laser(False)
try:
    sleep(0.5)                               # let a real laser actually go dark
    dark = tt.dark_rates(['photons'], duration_s=2.0)
finally:
    tt.set_mock_laser(True)
    for name in lasers:
        if wasOn[name]:
            api.imcontrol.setLaserActive(name, True)

rate = dark.rates_hz['photons']
print(f'photons with excitation blocked: {rate:,.0f} Hz  (dark counts)')
print(f'that is {100 * rate / max(1.0, lit):.1f} % of the lit rate')
print()
print('Afterpulses -- a detector firing again shortly after a real photon -- are a')
print('few percent of the lit rate on a SPAD and look just like dark counts to a')
print('histogram: flat over the laser period. The fitters subtract one flat rate')
print('for both; measure the afterpulse fraction from the vendor\'s data sheet or')
print('from a self-histogram, and add it to the dark rate here.')
AFTERPULSE_FRACTION = 0.02
background = rate + AFTERPULSE_FRACTION * lit
print(f'background to subtract: {background:,.0f} Hz '
      f'(dark {rate:,.0f} + {100 * AFTERPULSE_FRACTION:.0f} % afterpulsing)')

if DETECTOR is None:
    print('No FLIM detector in this setup to write background_rate_hz to.')
elif APPLY:
    api.imcontrol.setDetectorParameter(DETECTOR, 'background_rate_hz', float(background))
    print(f'Applied: {DETECTOR}.background_rate_hz = {background:.0f}')
else:
    print(f'Not applied (APPLY is False). Set {DETECTOR}.background_rate_hz = '
          f'{background:.0f} in the Settings widget, or in the setup file.')
