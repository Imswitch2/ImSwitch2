from types import SimpleNamespace

from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)



def test_advanced_ttl_targets_filter_scanning_positioners():
    setup_info = SimpleNamespace(
        positioners={
            "x_stage": SimpleNamespace(forScanning=True),
            "filter_wheel": SimpleNamespace(forScanning=False),
        }
    )

    assert AdvancedScanTTLCycleDesigner._ttl_targets(
        ["laser", "x_stage", "filter_wheel"], setup_info
    ) == ["laser", "filter_wheel"]


