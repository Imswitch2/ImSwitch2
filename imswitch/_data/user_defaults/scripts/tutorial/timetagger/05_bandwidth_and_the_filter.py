"""Tutorial timetagger 05 -- Bandwidth, overflows and the conditional filter.

You will learn
  * that every tag the card transmits costs USB bandwidth, and that an
    80 MHz sync alone is more than a Time Tagger 20 (about 8.5 M tags/s)
    or an Ultra (65-80 M) can carry -- only a Time Tagger X takes it raw
  * what an overflow is and why a frame read during one is not to be
    trusted; ``overflows()`` counts them for you
  * the conditional filter: transmit only the first sync *after* each
    photon, which cuts the sync to the photon rate -- and reverses the
    TCSPC direction, because the photon now starts the clock
  * why dividing the sync is *not* a remedy for FLIM (the window would be
    N periods and nothing folds it) and only a tool for measuring the rate

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a card with a Time Tagger X's bandwidth, so this setup does
                not overflow. To see the Time Tagger 20 case, put
                {"model": "Time Tagger 20"} into the block's mockFaults and
                restart: the sync alone then overflows, and the filter
                stops it.
  Your own microscope: the same block. filterSyncByPhotons in the block
                turns the filter on; the FLIM detector then runs in
                reverse mode (see the Time Tagger chapter).

Next: 06_irf_and_t0.py
"""

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()

# The tag budget: everything the card counts, added up.
rates = tt.count_rates(duration_s=1.0)
total = sum(rates.rates_hz.values())
print(rates.summary())
print(f'total: {total / 1e6:.2f} M tags/s')
print('budgets: Time Tagger 20 ~8.5 M/s, Ultra ~65 M/s, X ~1000 M/s (USB, per card)')
print()

# Overflows: the card dropped tags because the link could not carry them.
# overflows() is the one reader of the card's counter, kept as a running
# total, so a frame and this script never hide an overflow from each other.
# The card only transmits (and so only overflows) while a measurement is
# running, so take the baseline first and keep one running for the whole
# interval; a baseline taken after the count above would miss its overflows.
before = tt.overflows()
tt.count_rates(duration_s=2.0)
dropped = tt.overflows() - before
print(f'overflows in 2 s: {dropped}')
if dropped:
    print('  -> the link is over budget: tags are lost, and any FLIM frame read')
    print('     meanwhile is marked invalid. Enable the conditional filter.')
else:
    print('  -> within budget.')
print()

# The filter and the direction.
print(f'TCSPC direction: {tt.tcspc_direction}')
if tt.tcspc_direction == 'reverse':
    print('The conditional filter is on: only the first sync after each photon is')
    print('transmitted, so the sync rate you see equals the photon rate, and the')
    print('photon STARTS the histogram. The FLIM detector swaps its channels and')
    print('mirrors the time axis on the laser period, so decays still read forward.')
else:
    print('The conditional filter is off: every sync is transmitted. On a Time')
    print('Tagger 20 or Ultra at 80 MHz that overflows; set "filterSyncByPhotons":')
    print('true in the timeTagger block and the detector runs in reverse mode.')
print()

# What a histogram looks like in this direction -- the facade always returns
# forward time, so a decay rises at its peak and falls to the right.
hist = tt.histogram(duration_s=1.0)
print(hist.summary())
rising = hist.counts[:len(hist.counts) // 4].sum()
falling = hist.counts[len(hist.counts) // 4:].sum()
print(f'first quarter of the period holds {100 * rising / max(1, hist.total):.0f} % '
      f'of the counts, the rest {100 * falling / max(1, hist.total):.0f} %: '
      f'a decay, read forward.')
print()
print('Reverse mode on a real card is pending an acceptance test (see the plan,')
print('Lifetime 2.0 section 3.2): the vendor documents that the filter can')
print('reorder timestamps on some models. The simulated card does not do that.')
