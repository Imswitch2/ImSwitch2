# etSTED setup — validation sign-off

Sign-off log for ROADMAP 13.A. One row per check: the ImSwitch2 commit it was
run on, the date, the tester, and the measured result. The FLIM rows follow
the protocol in `docs/timetagger/validation.rst`; their numbers also go into
`docs/design/plans/lifetime-2-1.md`.

## FLIM (Lifetime 2.0)

| Step | Check | Commit | Date | Tester | Result |
|---|---|---|---|---|---|
| 1 | Card and roles count (tutorial 01) | | | | |
| 2 | Photon and sync trigger plateaus (02) | | | | |
| 3 | Dark rate (03) | | | | |
| 4 | Rep rate within 0.5 %, jitter at the floor (04) | | | | |
| 5 | No overflows; filter and direction (05) | | | | |
| 6 | IRF width and t0 reproducible (06) | | | | |
| 7 | Line edges = Ny; period = Nx·dwell + flyback (07) | | | | |
| 8 | Frame edge seen; skew below the pattern offset; last frame closes (08) | | | | |
| 9 | Line delay: shift 0 px on the second run (09) | | | | |
| 10 | Pre-flight green; convergence per method (10 extended) | | | | |
| 11 | Cube file reads back; gates resolved (11) | | | | |
| 12 | Widget's gated file equals tutorial 12's gates and conditioning (12) | | | | |
| 13 | Tau STED lifetime agrees with 10; pile-up below 5 % (13) | | | | |
| 14 | Live overlay on the dye; Stop within one scan | | | | |

## Other checks (13.A)

| Check | Commit | Date | Tester | Result |
|---|---|---|---|---|
| Confocal scan: image geometry verified | | | | |
| STED arm/disarm via SLM pattern switching | | | | |
| Event-triggered loop on a real sample | | | | |
| Tiling: stitched overview + cell-target navigation | | | | |
