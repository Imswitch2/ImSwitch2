from imswitch.improcess.controller.runtime_result_processors import (
    runtime_result_processor_ids,
)


def test_runtime_result_processor_ids_track_generic_processor_widgets():
    ids = runtime_result_processor_ids()

    assert "drift-correct" in ids
    assert "denoise" in ids
    assert "projection" not in ids
    assert "roi-manager" not in ids
