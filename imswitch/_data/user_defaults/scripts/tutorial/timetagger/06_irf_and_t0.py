"""Tutorial timetagger 06 -- The IRF and t0: where the laser pulse sits.

You will learn
  * ``histogram()``: the photon-vs-sync TCSPC histogram, in forward time
    whatever the card's direction
  * the instrument response function (IRF): its peak is where the
    excitation pulse lands on the time axis, its width is the detector's
    and the card's timing resolution
  * ``t0_ps``: the FLIM detector's offset that moves the IRF peak to the
    start of the window, and how to find it from the histogram -- as an
    absolute position, so running this twice gives the same number
  * what a truncated window looks like, and why you never want one

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a 350 ps IRF whose peak sits 1 ns after the sync.
  Your own microscope: for a clean IRF put a scattering or reflecting
                sample under the beam and take the emission filter out
                (or use a dye with a lifetime far below the IRF width); a
                SPAD's IRF also shifts with wavelength. For t0 alone, the
                sample does not matter.

Next: 07_line_clock_during_a_scan.py
"""

import os

import numpy as np

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
APPLY = False

rep_rate_mhz = (float(api.imcontrol.getDetectorParameter(DETECTOR, 'laser_rep_rate_mhz'))
                if DETECTOR else 80.0)
t0_now = int(api.imcontrol.getDetectorParameter(DETECTOR, 't0_ps')) if DETECTOR else 0
print(f'laser_rep_rate_mhz {rep_rate_mhz}, current t0_ps {t0_now}')

# The histogram over one laser period, 32 ps bins, two seconds. The facade
# returns forward time: in reverse mode it has already mirrored the axis.
# It measures with the photon input at the card's configured delay (not
# whatever the last scan left there, and not the detector's software roll),
# so the peak is an absolute position. A scan must not be running.
hist = tt.histogram(binwidth_ps=32, duration_s=2.0, laser_rep_rate_mhz=rep_rate_mhz)
print(hist.summary())
print()

# Print the histogram as a text plot: 40 rows of the period, log scale.
rows = 40
edges = np.linspace(0, hist.t_axis_ns[-1], rows + 1)
binned = [hist.counts[(hist.t_axis_ns >= a) & (hist.t_axis_ns < b)].sum()
          for a, b in zip(edges[:-1], edges[1:])]
top = max(1.0, np.log10(max(binned) + 1))
for a, value in zip(edges[:-1], binned):
    bar = '#' * int(40 * np.log10(value + 1) / top)
    print(f'{a:6.2f} ns |{bar}')
print()

# The IRF peak is where the pulse is. t0_ps is that position: in forward
# mode the detector delays the photon input by -t0_ps on top of the
# configured delay so the peak lands at the start of the window (and puts
# the input back after the scan); in reverse mode it applies the same
# number as a circular roll instead. Either way the number is absolute --
# not "the old t0 plus what is left", which would double-count on a re-run.
new_t0 = int(round(hist.peak_ns * 1000))
print(f'IRF peak at {hist.peak_ns:.3f} ns, FWHM {hist.fwhm_ns * 1000:.0f} ps '
      f'(photon input delay during the measurement: {hist.photon_delay_ps} ps)')
print(f'suggested t0_ps = {new_t0}  (current {t0_now})')
print()
print('A window shorter than the period would cut the tail (forward) or the peak')
print('(reverse) off; the detector defaults to one period, leave n_bins undeclared.')

# Save the numbers where the recordings go, for the follow-up (an IRF-aware
# fit needs this histogram).
folder = api.imcontrol.getRecFolder()
os.makedirs(folder, exist_ok=True)          # today's folder may not exist yet
out = os.path.join(folder, 'timetagger_irf.npz')
np.savez(out, t_axis_ns=hist.t_axis_ns, counts=hist.counts, peak_ns=hist.peak_ns,
         fwhm_ns=hist.fwhm_ns, direction=hist.direction, t0_ps_suggested=new_t0)
print(f'saved to {out}')

if DETECTOR is None:
    print('No FLIM detector to write t0_ps to.')
elif APPLY:
    api.imcontrol.setDetectorParameter(DETECTOR, 't0_ps', new_t0)
    print(f'Applied: {DETECTOR}.t0_ps = {new_t0} (takes effect at the next scan).')
else:
    print(f'Not applied (APPLY is False). Set {DETECTOR}.t0_ps = {new_t0}.')
