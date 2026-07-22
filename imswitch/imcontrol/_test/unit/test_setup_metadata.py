"""Tests for the single source of setup kind/category metadata."""

from imswitch.imcontrol.model.configeditor.catalog import (
    CATEGORY_TO_DIR,
    CATEGORY_TO_KIND,
    KIND_TO_CATEGORY,
    audit_core_manager_coverage,
)
from imswitch.imcontrol.model.plugins.manifest import (
    ALL_VALID_KINDS,
    REGISTRY_BACKED_KINDS,
)
from imswitch.imcontrol.model.plugins.setup_metadata import (
    daq_device_categories,
    registry_backed_setup_kinds,
    setup_kinds,
    setup_section_to_kind,
)
from imswitch.imcontrol.model.plugins.validation import (
    DAQ_DEVICE_CATEGORIES,
    SETUP_SECTION_TO_KIND,
)


def test_metadata_is_the_shared_mapping_contract():
    """Catalog, validation, and manifest capabilities agree on every kind."""
    metadata = setup_kinds()

    assert {item.kind for item in metadata} == ALL_VALID_KINDS
    assert {item.kind for item in registry_backed_setup_kinds()} == REGISTRY_BACKED_KINDS
    assert KIND_TO_CATEGORY == {item.kind: item.editor_category for item in metadata}
    assert CATEGORY_TO_KIND == {item.editor_category: item.kind for item in metadata}
    assert CATEGORY_TO_DIR == {
        item.editor_category: item.legacy_manager_directory for item in metadata
    }
    assert SETUP_SECTION_TO_KIND == setup_section_to_kind()
    assert DAQ_DEVICE_CATEGORIES == list(daq_device_categories())


# A source-tree manager added to a registry-backed kind must be consciously
# registered in builtins.py or added to this migration inventory.  This turns
# silent filename discovery into a review decision.
_UNREGISTERED_CORE_MANAGERS = {
    ("detector", "APDManager"),
    ("detector", "BaslerManager"),
    ("detector", "ESP32CamManager"),
    ("detector", "GXPIPYManager"),
    ("detector", "JetsonCamManager"),
    ("detector", "PMTManager"),
    ("detector", "PhotometricsManager"),
    ("detector", "PiCamManager"),
    ("detector", "SwabianTimeTaggerManager"),
    ("detector", "TISManager"),
    ("detector", "ThorCamTSIManager"),
    ("laser", "AAAOTFLaserManager"),
    ("laser", "Cobolt0601LaserManager"),
    ("laser", "Cobolt0601NewLaserManager"),
    ("laser", "CoolLEDLaserManager"),
    ("laser", "ESP32LEDLaserManager"),
    ("laser", "ESP32LEDMatrixManager"),
    ("laser", "ESP32LightSheetManager"),
    ("laser", "GRBLLaserManager"),
    ("laser", "LEDMatrixManager"),
    ("laser", "LantzLaserManager"),
    ("laser", "MPBLaserManager"),
    ("laser", "OxxiusCombinerLaserManager"),
    ("laser", "OxxiusLaserManager"),
    ("laser", "PulseGeneratorLaserManager"),
    ("laser", "PulseStreamerLaserManager"),
    ("laser", "PyCoboltManager"),
    ("laser", "PyMicroscopeLaserManager"),
    ("laser", "TriggerScopeLaserManager"),
    ("positioner", "BSC203StageManager"),
    ("positioner", "ESP32StageManager"),
    ("positioner", "GRBLStageManager"),
    ("positioner", "JenaPiezoZManager"),
    ("positioner", "KDC101PositionerManager"),
    ("positioner", "KinesisStageManager"),
    ("positioner", "LeicaDMIManager"),
    ("positioner", "MHXYStageManager"),
    ("positioner", "PIStageManager"),
    ("positioner", "PiezoconceptZManager"),
    ("positioner", "SQUIDStageManager"),
    ("positioner", "SerialDacZManager"),
    ("positioner", "SmarACTPositionerManager"),
    ("positioner", "TriggerScopePositionerManager"),
    ("rotator", "ElliptecRotatorManager"),
    ("rotator", "KinesisRotatorManager"),
    ("rotator", "StandaRotatorManager"),
    ("rs232", "ESP32Manager"),
    ("rs232", "ElliptecManager"),
    ("rs232", "GRBLManager"),
    ("rs232", "KDC101Manager"),
    ("rs232", "SQUIDManager"),
    ("slm", "HamamatsuSLMdviManager"),
    ("slm", "HamamatsuSLMusbManager"),
}


def test_core_manager_registry_coverage_is_an_explicit_inventory():
    """New core managers cannot silently become legacy-scanned editor entries."""
    coverage = audit_core_manager_coverage()
    by_status = {
        status: {
            (item.manager.kind, item.manager.manager_name)
            for item in coverage
            if item.status == status
        }
        for status in ("registered", "unregistered", "legacy_only")
    }

    assert len(by_status["registered"]) == 9
    assert by_status["unregistered"] == _UNREGISTERED_CORE_MANAGERS
    assert by_status["legacy_only"] == {
        ("pulse_generator", "PulseStreamerManager"),
        ("pulse_generator", "TeensyPulseManager"),
    }


def test_registry_backed_kinds_match_the_named_loader_subsets():
    """The derived kind set and the hand-written loader subsets must agree.

    REGISTRY_BACKED_KINDS is now derived from setup_metadata's
    supports_external_plugins flag, while MULTIMANAGER_BACKED_KINDS and
    STANDALONE_REGISTRY_BACKED_KINDS remain as documentation of *which* loader
    handles each kind. Nothing recomputes one from the other, so they can drift:
    flipping supports_external_plugins on a kind with no loader would let a
    manifest declare it and then fail at runtime. That drift is exactly what
    consolidating onto setup_metadata was meant to prevent, so assert it.
    """
    from imswitch.imcontrol.model.plugins.manifest import (
        BESPOKE_LOADER_KINDS,
        MULTIMANAGER_BACKED_KINDS,
        REGISTRY_BACKED_KINDS,
        STANDALONE_REGISTRY_BACKED_KINDS,
        ALL_VALID_KINDS,
    )

    assert (
        MULTIMANAGER_BACKED_KINDS | STANDALONE_REGISTRY_BACKED_KINDS
        == REGISTRY_BACKED_KINDS
    )
    assert REGISTRY_BACKED_KINDS | BESPOKE_LOADER_KINDS == ALL_VALID_KINDS
    assert not (REGISTRY_BACKED_KINDS & BESPOKE_LOADER_KINDS)
