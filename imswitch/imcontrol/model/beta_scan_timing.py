"""Operator-facing diagnostics for the Beta stage scan timing configuration.

Only report what the setup declares; reading timing must never populate missing
keys or pretend that the MoNaLISA fallback is a calibrated stage value.
"""

import math

DEFAULT_MOVE_TIME_S = 0.002
DEFAULT_SETTLE_TIME_S = 0.002


def missing_beta_timing_keys(params):
    """Timing keys absent from the setup (not the independent return_time)."""
    params = params if isinstance(params, dict) else {}
    return tuple(k for k in ('move_time', 'settle_time') if k not in params)


def beta_timing_config_warning(params):
    """A log/UI warning when stage timing relies on historical defaults."""
    missing = missing_beta_timing_keys(params)
    if not missing:
        return ''
    return (
        f'scan.scanDesignerParams does not declare {"/".join(missing)}; '
        f'using {DEFAULT_MOVE_TIME_S * 1000:g} ms move and '
        f'{DEFAULT_SETTLE_TIME_S * 1000:g} ms settle, the MoNaLISA stage\'s '
        'values. Declare the times your stage needs in the setup file.'
    )


def beta_scan_timing_ui_warning(params, dwell_s=None):
    """Return ``(short message, tooltip)`` for non-modal scan UI feedback.

    A too-short dwell takes precedence; if timing is not explicitly configured
    both concerns are conveyed in the same line instead of two warning banners.
    """
    missing = missing_beta_timing_keys(params)
    params = params if isinstance(params, dict) else {}
    config_detail = beta_timing_config_warning(params)
    try:
        move = float(params.get('move_time', DEFAULT_MOVE_TIME_S))
        settle = float(params.get('settle_time', DEFAULT_SETTLE_TIME_S))
    except (ValueError, TypeError):
        return 'Invalid Beta stage timing settings', 'Check move_time and settle_time in the hardware configuration.'
    if not (math.isfinite(move) and math.isfinite(settle)) or min(move, settle) < 0:
        return 'Invalid Beta stage timing settings', 'Move and settle must be finite, nonnegative durations.'

    if dwell_s is not None:
        try:
            dwell = float(dwell_s)
            stationary = dwell - move - settle
        except (ValueError, TypeError):
            dwell = None
        if dwell is not None and dwell > 0 and stationary < dwell / 2:
            suffix = ' · timing uses defaults' if missing else ''
            if stationary <= 0:
                message = (f'{dwell * 1000:g} ms dwell is too short for '
                           f'{(move + settle) * 1000:g} ms move + settle')
            else:
                message = f'Only {stationary * 1000:g} ms of {dwell * 1000:g} ms dwell is stationary'
            return (
                message + suffix,
                f'Move {move * 1000:g} ms + settle {settle * 1000:g} ms. '
                + (config_detail or 'The detector may integrate stage motion during the dwell.'),
            )

    if missing:
        return (
            f'Stage timing uses defaults: move {move * 1000:g} ms · settle {settle * 1000:g} ms',
            config_detail,
        )
    return '', ''
