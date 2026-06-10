from imswitch.improcess.controller.runtime_result_processors import (
    runtime_result_processor_ids,
)
from imswitch.improcess.processors import available_processor_ids


def test_runtime_result_processor_ids_track_builtin_processors():
    assert runtime_result_processor_ids() == available_processor_ids()

