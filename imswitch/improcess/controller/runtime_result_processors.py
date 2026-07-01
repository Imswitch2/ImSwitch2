"""Helpers for wiring runtime-loaded result processor widgets."""


def runtime_result_processor_ids() -> list[str]:
    """Return processor ids that may expose a generic result processor panel."""
    from imswitch.improcess.model.runtime_tools import (
        runtime_result_processor_ids as _runtime_result_processor_ids,
    )

    return _runtime_result_processor_ids()
