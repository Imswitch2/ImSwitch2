"""Display-only reductions are bounded by the bytes they would allocate."""

import numpy as np
import pytest

from imswitch.improcess.model import contrast


def test_the_contrast_gate_counts_bytes_not_elements():
    """A 16-frame 2048x2048 uint16 stack sat exactly at the old 64 Mi-element
    threshold and paid a ~1 GiB transient for two numbers."""
    big_uint16 = np.zeros((16, 2048, 2048), dtype=np.uint16)
    assert contrast._should_sample(big_uint16)
    small_float64 = np.zeros((1024, 1024), dtype=np.float64)
    assert not contrast._should_sample(small_float64)
    limit = contrast._SAMPLE_WORKING_SET_BYTES // contrast._WORKING_SET_BYTES_PER_ELEMENT
    assert not contrast._should_sample(np.zeros(limit, dtype=np.uint8))
    assert contrast._should_sample(np.zeros(limit + 1, dtype=np.uint8))


def test_a_processor_without_an_id_cannot_be_made():
    from imswitch.improcess.processors.base import Processor

    class Nameless(Processor):
        name = "Nameless"

        @property
        def applies_to(self):
            return lambda result: True

        def make_param_widget(self, parent):
            return None

        def apply(self, result, params):
            return result

    with pytest.raises(TypeError, match="must set a stable `id`"):
        Nameless()

    class Named(Nameless):                  # a shared base may itself be nameless
        name = "Named"
        id = "named"

    assert Named().id == "named"


def test_the_live_stall_watchdog_is_opt_in_per_continuous_source():
    from imswitch.improcess.live import sources

    assert sources.LiveSource.idles_between_stacks is True
    assert sources.ZarrLiveSource.idles_between_stacks is False
    assert sources.Hdf5LiveSource.idles_between_stacks is False
    for lapse in (sources.ZarrLapseSource, sources.Hdf5LapseSource,
                  sources.ZarrMultiFileLapseSource, sources.Hdf5MultiFileLapseSource):
        assert lapse.idles_between_stacks is True, lapse


def test_beadrec_requires_the_scan_loops_it_measures_with_to_be_calibrated():
    from types import SimpleNamespace

    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA, PAYLOAD_DETECTOR_FRAME_STREAM, AcquisitionLayout,
        AcquisitionLoop, encode_acquisition_layout,
    )
    from imswitch.improcess.model.acquisition_layout_resolver import resolve_acquisition_layout
    from imswitch.improcess.reconstructors.beadrec.reconstructor import BeadRecReconstructor

    assert BeadRecReconstructor.acquisition_requirements.requires_calibrated_loops == {"scan_x", "scan_y"}

    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 2),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
    )
    resolved = resolve_acquisition_layout(
        {"AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
         "AcquisitionLayout:json": encode_acquisition_layout(layout)},
        shape=(6, 4, 4), detector="Cam",
    )
    inspection = BeadRecReconstructor().inspect_source(
        SimpleNamespace(sourceKind="image", acquisition_layout=resolved)
    )
    codes = {issue.code for issue in inspection.issues}
    assert "UNCALIBRATED_ACQUISITION_LOOP" in codes
