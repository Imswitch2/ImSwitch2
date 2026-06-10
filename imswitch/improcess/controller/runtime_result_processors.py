"""Helpers for wiring runtime-loaded result processor widgets."""


def runtime_result_processor_ids() -> list[str]:
    """Return processor ids that may expose a generic result processor panel."""
    from imswitch.improcess.processors import available_processor_ids

    return available_processor_ids()

