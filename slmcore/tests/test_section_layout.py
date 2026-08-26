import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,
    SectionSplitLayout,
    SLMConfig,
    SLMGeometry,
    SLMIdentity,
    SLMRuntime,
    SLMSectionConfig,
    SLM_CONFIG_SCHEMA_VERSION,
    SLMSectionState,
    create_split_section_geometries,
    split_layout_signature,
    validate_config_section_layout,
)
from slmcore.cgh import CGHResult


def _geometry():
    return SLMGeometry(width=10,height=6,pixel_size_um=1.0)


def _config(geometry,sections):
    return SLMConfig(
        schema_version=SLM_CONFIG_SCHEMA_VERSION,
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        sections={
            key:SLMSectionConfig(
                geometry=section,
                state=SLMSectionState.create(DEFAULT_REGISTRIES),
            )
            for key,section in sections.items()
        },
        final_eightbit=np.zeros(geometry.shape,dtype=np.uint8),
    )


def test_even_split_layout_supports_x_and_y():
    geometry = _geometry()

    x_sections = create_split_section_geometries(
        geometry,SectionSplitLayout(n_sections=3,axis="x",mode="even"),
    )
    assert split_layout_signature(geometry,x_sections).axis == "x"
    assert split_layout_signature(geometry,x_sections).sizes == (4,3,3)

    y_sections = create_split_section_geometries(
        geometry,SectionSplitLayout(n_sections=4,axis="y",mode="even"),
    )
    assert split_layout_signature(geometry,y_sections).axis == "y"
    assert split_layout_signature(geometry,y_sections).sizes == (2,2,1,1)


def test_manual_layout_requires_positive_exact_coverage():
    geometry = _geometry()

    with pytest.raises(ValueError,match="positive"):
        SectionSplitLayout(
            n_sections=2,axis="x",mode="manual",sizes=(5,0),
        )

    with pytest.raises(ValueError,match="sum"):
        create_split_section_geometries(
            geometry,
            SectionSplitLayout(
                n_sections=2,axis="x",mode="manual",sizes=(5,4),
            ),
        )

    sections = create_split_section_geometries(
        geometry,
        SectionSplitLayout(
            n_sections=2,axis="x",mode="manual",sizes=(3,7),
        ),
    )
    assert tuple(section.width for section in sections.values()) == (3,7)


def test_split_layout_validator_rejects_gaps_and_wrong_count():
    geometry = _geometry()
    sections = create_split_section_geometries(
        geometry,
        SectionSplitLayout(n_sections=2,axis="x",mode="manual",sizes=(5,5)),
    )
    gapped = dict(sections)
    gapped["sec_1"] = type(gapped["sec_1"])(
        key="sec_1",x=6,y=0,width=4,height=geometry.height,
    )

    with pytest.raises(ValueError,match="contiguous"):
        split_layout_signature(geometry,gapped,axis="x")

    with pytest.raises(ValueError,match="expected 3"):
        split_layout_signature(geometry,sections,n_sections=3)


def test_config_policy_enforces_physical_geometry_and_setup_layout():
    geometry = _geometry()
    setup_sections = create_split_section_geometries(
        geometry,SectionSplitLayout(n_sections=2,axis="x",mode="even"),
    )
    y_sections = create_split_section_geometries(
        geometry,SectionSplitLayout(n_sections=2,axis="y",mode="even"),
    )

    with pytest.raises(ValueError,match="physical SLM"):
        validate_config_section_layout(
            physical_geometry=geometry,
            config_geometry=SLMGeometry(width=11,height=6,pixel_size_um=1.0),
            config_section_geometries=setup_sections,
            setup_section_geometries=setup_sections,
            section_layout_customizable=True,
        )

    with pytest.raises(ValueError,match="setup-defined"):
        validate_config_section_layout(
            physical_geometry=geometry,
            config_geometry=geometry,
            config_section_geometries=y_sections,
            setup_section_geometries=setup_sections,
            section_layout_customizable=False,
        )

    signature = validate_config_section_layout(
        physical_geometry=geometry,
        config_geometry=geometry,
        config_section_geometries=y_sections,
        setup_section_geometries=setup_sections,
        section_layout_customizable=True,
    )
    assert signature.axis == "y"


def test_runtime_from_config_restores_config_owned_layout_exactly():
    geometry = _geometry()
    sections = create_split_section_geometries(
        geometry,SectionSplitLayout(n_sections=2,axis="y",mode="manual",sizes=(1,5)),
    )
    config = _config(geometry,sections)

    runtime = SLMRuntime.from_config(config,registries=DEFAULT_REGISTRIES)

    assert split_layout_signature(
        runtime.geometry,
        {key:runtime.get_section_geometry(key) for key in runtime.section_keys},
    ) == split_layout_signature(geometry,sections)


def test_cgh_session_round_zero_roundtrips_from_config_dict():
    geometry = SLMGeometry(width=32,height=32,pixel_size_um=1.0)
    sections = create_split_section_geometries(
        geometry,SectionSplitLayout(n_sections=1,axis="x",mode="even"),
    )
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=sections,
        registries=DEFAULT_REGISTRIES,
    )
    section_key = "sec_0"

    runtime.apply_section_patch(
        section_key,
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci",
        },
    )
    job = runtime.prepare_section_cgh(section_key)
    result = CGHResult(
        generation=job.generation,
        spec=job.spec,
        target_name=job.target_name,
        pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
    )

    assert runtime.commit_section_cgh(section_key,result) is not None

    loaded,warnings = SLMConfig.from_dict(
        runtime.create_config().to_dict(),DEFAULT_REGISTRIES,
    )
    restored = loaded.sections[section_key].cgh_session

    assert warnings == ()
    assert restored is not None
    assert restored.committed_target is not None
    assert len(restored.rounds) == 1
    assert restored.rounds[0].index == 0
    assert restored.rounds[0].result is not None
