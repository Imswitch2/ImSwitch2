#!/usr/bin/env python
"""Standalone BSC203 diagnostic — talk to the controller directly, no ImSwitch.

Purpose: isolate whether the "X/Y go crazy" behaviour comes from the hardware /
vendor-library layer or from ImSwitch logic, and to MEASURE the true steps/mm
scale and movement direction instead of assuming them.

It opens the BSC203 with the exact same construction parameters the ImSwitch
manager uses (x=3, swap_limit_switches=True, invert_direction_logic=False) and
turns on the vendor library's DEBUG logging, so every serial command and reply
is printed.

USAGE (run on the rig, in the Imswitch2 conda env):

    python utility_scripts/bsc203_diag.py status                 # dump status of all bays
    python utility_scripts/bsc203_diag.py monitor --seconds 10   # live-print positions
    python utility_scripts/bsc203_diag.py home                   # home all, wait, report
    python utility_scripts/bsc203_diag.py calibrate X --um 200   # MEASURE steps/mm + direction
    python utility_scripts/bsc203_diag.py moveabs X --um 1000    # absolute move (clamped)

SAFETY: moves are small by default, clamped to >= 0 encoder steps, and printed
before they are sent. Keep a hand near the controller power switch the first
time. Start with `status`, then `calibrate` with a SMALL --um.

ROOT CAUSE (confirmed from rig logs 2026-06-22): move_absolute RUNS AWAY whenever
the target is BELOW the current position (a downward / negative-direction move) —
the firmware reads the signed displacement as unsigned and drives to the
end-stop. Upward moves are fine. Scale is correct (DRV208 = 819200 counts/mm).
Fix (manager + this script): never use move_absolute; jog by |target - current|
with a positive step size + direction flag, which the firmware handles correctly
both ways.

What this script is for now:
  * `status`: dump loop/jog params and limit switches.
  * `calibrate X --um 200` then `calibrate X --um -200`: verify BOTH directions
    move cleanly and settle (the down move is the one that used to run away).
  * `monitor`: watch a move's profile — it should ramp to target and STOP.
"""

import argparse
import logging
import sys
import time

# Scale ImSwitch currently assumes (BSC203StageManager: STEPS_PER_REV * REV_PER_MM).
SCRIPT_STEPS_PER_MM = 409600 * 2  # = 819200


def _steps_from_um(um):
    return int(um / 1000 * SCRIPT_STEPS_PER_MM)


def _um_from_steps(steps):
    return steps / SCRIPT_STEPS_PER_MM * 1000


AXIS_TO_BAY = {"X": 0, "Y": 1, "Z": 2}


def _setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # The vendor library logs every serial command/reply at DEBUG.
    logging.getLogger("thorlabs_apt_device").setLevel(logging.DEBUG)


def _open(port, home=False):
    from thorlabs_apt_device.devices.bsc import BSC
    print(f"Opening BSC203 on {port} (home={home}) ...")
    dev = BSC(
        serial_port=port, vid=None, pid=None, manufacturer=None, product=None,
        serial_number=None, location=None, home=home, x=3,
        invert_direction_logic=False, swap_limit_switches=True,
    )
    # Give the polled status a moment to populate.
    time.sleep(1.0)
    return dev


def _status_row(dev, bay):
    s = dev.status_[bay][0]
    pos = s.get("position")
    return {
        "bay": bay,
        "position_steps": pos,
        "position_um(scale)": None if pos is None else round(_um_from_steps(pos), 2),
        "homed": s.get("homed"),
        "homing": s.get("homing"),
        "moving_fwd": s.get("moving_forward"),
        "moving_rev": s.get("moving_reverse"),
        "fwd_limit": s.get("forward_limit_switch"),
        "rev_limit": s.get("reverse_limit_switch"),
        "msg": s.get("msg"),
    }


def cmd_status(dev, args):
    print("\n=== BSC203 status (all bays) ===")
    print(f"(script assumes {SCRIPT_STEPS_PER_MM} steps/mm — confirmed correct for DRV208)")
    for bay in range(3):
        print(_status_row(dev, bay))

    # Control-loop + jog params reveal whether the (encoded) stage is in the
    # right mode. loop_mode: 1=open-loop, 2=closed-loop. A closed-loop stage with
    # a wrong encoder_const / PID hunts; an open-loop encoded stage mis-positions.
    print("\n--- loop / jog parameters (per bay) ---")
    for bay in range(3):
        loop = getattr(dev, "loopparams_", None)
        jog = getattr(dev, "jogparams_", None)
        loop_b = loop[bay][0] if loop else {}
        jog_b = jog[bay][0] if jog else {}
        print(f"bay {bay}: loop_mode={loop_b.get('loop_mode')} "
              f"encoder_const={loop_b.get('encoder_const')} "
              f"prop={loop_b.get('prop')} int={loop_b.get('int')} diff={loop_b.get('diff')} "
              f"| jog_mode={jog_b.get('jog_mode')} step_size={jog_b.get('step_size')}")


def cmd_monitor(dev, args):
    print(f"\n=== Monitoring positions for {args.seconds}s (Ctrl+C to stop) ===")
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            cols = []
            for axis, bay in AXIS_TO_BAY.items():
                p = dev.status_[bay][0].get("position")
                cols.append(f"{axis}={p}st/{_um_from_steps(p):.1f}um" if p is not None else f"{axis}=?")
            print("  ".join(cols))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("stopped")


def _wait_settled(dev, bay, timeout=30.0, verbose=True):
    """Wait until the bay stops moving (or timeout). Prints the motion profile so
    overshoot / hunting is visible. Returns final position."""
    deadline = time.time() + timeout
    last = None
    stable = 0
    t0 = time.time()
    while time.time() < deadline:
        s = dev.status_[bay][0]
        moving = s.get("moving_forward") or s.get("moving_reverse")
        pos = s.get("position")
        if verbose:
            print(f"    t={time.time()-t0:5.1f}s pos={pos}st "
                  f"moving={'F' if s.get('moving_forward') else ''}"
                  f"{'R' if s.get('moving_reverse') else ''}"
                  f"{'-' if not moving else ''}")
        if not moving and pos == last:
            stable += 1
            if stable >= 4:  # ~0.4s of no change and not moving
                if verbose:
                    print("    -> settled")
                return pos
        else:
            stable = 0
        last = pos
        time.sleep(0.1)
    if verbose:
        print("    -> TIMEOUT (never settled — likely hunting/oscillating)")
    return dev.status_[bay][0].get("position")


def cmd_home(dev, args):
    print("\n=== Homing all bays ===")
    for bay in range(3):
        dev.home(bay=bay)
    # Wait for homing to begin then complete (observe, do not assume).
    t0 = time.time()
    while time.time() - t0 < 5:
        if any(dev.status_[b][0].get("homing") or not dev.status_[b][0].get("homed")
               for b in range(3)):
            break
        time.sleep(0.05)
    while not all(dev.status_[b][0].get("homed") for b in range(3)):
        if time.time() - t0 > 120:
            print("  timeout waiting for homed")
            break
        time.sleep(0.1)
    print("  homed. Final status:")
    for bay in range(3):
        print(_status_row(dev, bay))


def _move_abs_steps(dev, bay, steps):
    """Move to absolute encoder position `steps` using a bounded JOG.

    move_absolute runs away on the BSC203 whenever the target is BELOW the
    current position (negative displacement underflows). So, exactly like the
    fixed manager, we jog by |target - current| with a positive size + direction.
    """
    target = max(0, int(steps))  # never command below the home end-stop
    cur = dev.status_[bay][0].get("position")
    if cur is None:
        cur = 0
    delta = target - int(cur)
    if delta == 0:
        print("  already at target")
        return
    size = abs(delta)
    direction = delta > 0  # forward jog assumed to increase the encoder
    print(f"  -> jog {'FWD' if direction else 'REV'} {size} steps "
          f"(current={cur} target={target})")
    # Moderate velocity (DRV208: 21987328 counts/mm/s == 1 mm/s).
    dev.set_jog_params(size, 4506, 21987328, continuous=False,
                       immediate_stop=False, bay=bay, channel=0)
    dev.move_jog(direction=direction, bay=bay, channel=0)


def cmd_moveabs(dev, args):
    bay = AXIS_TO_BAY[args.axis]
    target = _steps_from_um(args.um)
    print(f"\n=== Absolute move {args.axis} -> {args.um} um ({target} steps) ===")
    before = dev.status_[bay][0].get("position")
    _move_abs_steps(dev, bay, target)
    after = _wait_settled(dev, bay)
    print(f"  before={before}st  after={after}st  delta={None if (after is None or before is None) else after - before}st")


def cmd_calibrate(dev, args):
    """Command a known step delta and report the encoder delta, so the true
    steps/mm and direction can be derived from a physical measurement."""
    bay = AXIS_TO_BAY[args.axis]
    s = dev.status_[bay][0]
    before = s.get("position")
    if before is None:
        print("No position reported yet — run `status` first / check connection.")
        return
    delta_steps = _steps_from_um(args.um)
    target = before + delta_steps
    print(f"\n=== Calibrate {args.axis}: commanding {args.um:+.1f} um ({delta_steps:+d} steps) ===")
    print(f"  start={before}st ({_um_from_steps(before):.1f}um by script scale)")
    _move_abs_steps(dev, bay, target)
    after = _wait_settled(dev, bay)
    observed = None if after is None else after - before
    print(f"  end={after}st  observed encoder delta={observed}st (commanded {delta_steps}st)")
    print("\n  Interpretation:")
    print(f"  * Scale is confirmed correct ({SCRIPT_STEPS_PER_MM} st/mm for DRV208), so the")
    print(f"    observed delta should be ~{delta_steps}st if the move completed cleanly.")
    print("  * If the stage OSCILLATED / never settled (watch the per-0.1s prints from")
    print("    _wait_settled, or run `monitor`), that is closed-loop hunting — check the")
    print("    loop_mode/encoder_const printed by `status`.")
    print("  * If it ran to an end-stop on this small move, note which limit switch")
    print("    tripped in `status` (fwd_limit/rev_limit).")
    print("  * DIRECTION: did +um move the stage the way you expect physically?")


def main():
    p = argparse.ArgumentParser(description="BSC203 direct diagnostic (no ImSwitch).")
    p.add_argument("--port", default="COM9", help="serial port (default COM9)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    m = sub.add_parser("monitor")
    m.add_argument("--seconds", type=float, default=10.0)
    sub.add_parser("home")
    for name in ("moveabs", "calibrate"):
        sp = sub.add_parser(name)
        sp.add_argument("axis", choices=list(AXIS_TO_BAY))
        sp.add_argument("--um", type=float, default=200.0)

    args = p.parse_args()
    _setup_logging()

    dev = _open(args.port, home=False)
    try:
        {
            "status": cmd_status,
            "monitor": cmd_monitor,
            "home": cmd_home,
            "moveabs": cmd_moveabs,
            "calibrate": cmd_calibrate,
        }[args.cmd](dev, args)
    finally:
        time.sleep(0.5)
        try:
            dev.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
