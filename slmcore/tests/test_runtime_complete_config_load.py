import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.engine.section import split_slm_geometry


def _runtime():
    geometry = SLMGeometry(width=16,height=8,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2),
        registries=DEFAULT_REGISTRIES,
    )


def _changed_config():
    runtime = _runtime()
    runtime.apply_section_patch(
        "sec_0",{("optics","wavelength_nm"):600},
    )
    return runtime.create_config()


def test_require_complete_rejects_before_any_section_commit(monkeypatch):
    runtime = _runtime()
    config = _changed_config()
    before_revision = runtime.revision
    before_wavelength = runtime.get_section_snapshot(
        "sec_0"
    ).state.optics.wavelength_nm

    section = runtime._sections["sec_1"]
    monkeypatch.setattr(
        section,
        "prepare_config_load",
        lambda *_args,**_kwargs:(_ for _ in ()).throw(RuntimeError("bad section")),
    )

    with pytest.raises(RuntimeError,match="Complete SLM config restore failed"):
        runtime.load_config(config,require_complete=True)

    assert runtime.revision == before_revision
    assert runtime.get_section_snapshot(
        "sec_0"
    ).state.optics.wavelength_nm == before_wavelength


def test_default_config_load_keeps_existing_partial_recovery(monkeypatch):
    runtime = _runtime()
    config = _changed_config()
    section = runtime._sections["sec_1"]
    monkeypatch.setattr(
        section,
        "prepare_config_load",
        lambda *_args,**_kwargs:(_ for _ in ()).throw(RuntimeError("bad section")),
    )

    report = runtime.load_config(config)

    assert "sec_1" in report.failed_sections
    assert runtime.get_section_snapshot(
        "sec_0"
    ).state.optics.wavelength_nm == 600
