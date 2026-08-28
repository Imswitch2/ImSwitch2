import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,
    CalibrationMismatchPolicy,
    SLM_CONFIG_SCHEMA_VERSION,
    SLMConfig,
    SLMControlMode,
    SLMGeometry,
    SLMIdentity,
    SLMRuntimeFactory,
    SLMSession,
    SLMSessionCallbacks,
    SLMSectionsSetup,
    SLMSetup,
    SLMWorkspace,
)
from slmcore.engine.section import SectionSplitLayout,create_split_section_geometries
from slmcore.host import MockSLMDeviceProvider,SLMHostServices


def _setup():
    return SLMSetup(
        identity=SLMIdentity("slm","SER123"),
        geometry=SLMGeometry(width=24,height=12,pixel_size_um=1.0),
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=2,axis="x"),
            customizable=True,
        ),
    )


def _session(tmp_path,*,callbacks=None):
    setup = _setup()
    workspace = SLMWorkspace(tmp_path)
    repository = workspace.config_repository(setup,DEFAULT_REGISTRIES)
    factory = SLMRuntimeFactory(
        setup=setup,
        registries=DEFAULT_REGISTRIES,
        config_repository=repository,
    )
    device = MockSLMDeviceProvider()
    session = SLMSession(
        runtime=factory.create_default(),
        host_services=SLMHostServices(device=device),
        runtime_factory=factory,
        config_repository=repository,
        callbacks=callbacks,
    )
    return session,repository,factory,device


def _save_config(session,repository,name,value,*,config=None):
    config = session.runtime.create_config() if config is None else config
    saved = SLMConfig(
        schema_version=config.schema_version,
        identity=config.identity,
        geometry=config.geometry,
        sections=config.sections,
        final_eightbit=np.full(config.geometry.shape,value,dtype=np.uint8),
    )
    return repository.save(name,saved,"test",overwrite=False).path


def test_normal_load_preserves_partial_recovery_policy(tmp_path,monkeypatch):
    session,repository,_factory,_device = _session(tmp_path)
    path = _save_config(session,repository,"normal.h5",17)
    calls = []
    original = session.runtime.load_config

    def wrapped(config,*,require_complete=False):
        calls.append(bool(require_complete))
        return original(config,require_complete=require_complete)

    monkeypatch.setattr(session.runtime,"load_config",wrapped)
    outcome = session.load_config(
        str(path),
        calibration_mismatch_policy=CalibrationMismatchPolicy.REJECT,
    )
    assert outcome.report is not None
    assert calls == [False]
    assert session.current_config_path == str(path)


def test_fast_config_restore_is_always_strict(tmp_path,monkeypatch):
    session,repository,_factory,device = _session(tmp_path)
    path = _save_config(session,repository,"fast.h5",71)
    session.load_config(str(path))

    calls = []
    original = session._configuration.commit_load

    def wrapped(runtime,prepared,**kwargs):
        calls.append(bool(kwargs.get("require_complete",False)))
        return original(runtime,prepared,**kwargs)

    monkeypatch.setattr(session._configuration,"commit_load",wrapped)
    assert session.set_control_mode(SLMControlMode.FAST_CONFIG)
    assert calls == [True]
    assert session.fast_config_path == str(path)
    np.testing.assert_array_equal(
        device.last_frame,np.full(session.runtime.geometry.shape,71,dtype=np.uint8),
    )


def test_fast_activation_does_not_mutate_editor_runtime_and_exit_restores_strictly(
    tmp_path,monkeypatch,
):
    session,repository,_factory,_device = _session(tmp_path)
    path_a = _save_config(session,repository,"a.h5",11)
    path_b = _save_config(session,repository,"b.h5",93)
    session.load_config(str(path_a))
    assert session.set_control_mode(SLMControlMode.FAST_CONFIG)

    revision = session.runtime.revision
    snapshots = session.runtime.get_section_snapshots()
    session.activate_compiled_config(str(path_b))
    assert session.current_config_path == str(path_a)
    assert session.fast_config_path == str(path_b)
    assert session.runtime.revision == revision
    assert session.runtime.get_section_snapshots() == snapshots

    calls = []
    original = session._configuration.commit_load

    def wrapped(runtime,prepared,**kwargs):
        calls.append(bool(kwargs.get("require_complete",False)))
        return original(runtime,prepared,**kwargs)

    monkeypatch.setattr(session._configuration,"commit_load",wrapped)
    assert session.set_control_mode(SLMControlMode.EDITOR)
    assert calls == [True]
    assert session.current_config_path == str(path_b)
    assert session.fast_config_path is None


def test_presentation_callback_failure_does_not_rollback_config_commit(tmp_path):
    errors = []

    def bad_presenter(_outcome):
        raise RuntimeError("presentation broke")

    callbacks = SLMSessionCallbacks(
        on_config_committed=bad_presenter,
        on_error=lambda title,error:errors.append((title,error)),
    )
    session,repository,_factory,_device = _session(tmp_path,callbacks=callbacks)
    path = _save_config(session,repository,"committed.h5",23)

    outcome = session.load_config(str(path))
    assert outcome.path == str(path)
    assert session.current_config_path == str(path)
    assert errors
    assert "presentation" in errors[-1][0].lower()


def test_layout_replacement_is_application_authoritative_if_adapter_fails(tmp_path):
    errors = []

    def bad_runtime_adapter(_runtime):
        raise RuntimeError("view rebuild broke")

    callbacks = SLMSessionCallbacks(
        on_runtime_replaced=bad_runtime_adapter,
        on_error=lambda title,error:errors.append((title,error)),
    )
    session,repository,factory,_device = _session(tmp_path,callbacks=callbacks)
    runtime = session.runtime
    y_geometries = create_split_section_geometries(
        runtime.geometry,SectionSplitLayout(n_sections=2,axis="y"),
    )
    replacement = factory.create_layout_replacement(runtime,y_geometries)
    path = repository.save(
        "layout.h5",replacement.create_config(),"layout",overwrite=False,
    ).path

    outcome = session.load_config(str(path))
    assert outcome.runtime_replaced
    assert session.runtime.get_section_geometry("sec_0") == y_geometries["sec_0"]
    assert session.current_config_path == str(path)
    assert errors


def test_headless_calibration_mismatch_default_is_reject(tmp_path):
    session,repository,_factory,_device = _session(tmp_path)
    config = session.runtime.create_config()
    section = config.sections["sec_0"]
    # A valid calibration with deliberately stale recorded section geometry.
    from slmcore import SLMSectionCalibration
    calibration = SLMSectionCalibration(
        kx_per_um=1.0,
        ky_per_um=1.0,
        section_geometry={
            "key":"sec_0","x":0,"y":0,"width":1,"height":1,
        },
    )
    section.calibration = calibration
    path = repository.save("mismatch.h5",config,"mismatch",overwrite=False).path

    prepared = session.prepare_config_load(str(path))
    assert prepared.calibration_mismatches
    with pytest.raises(ValueError,match="calibration geometry"):
        session.apply_config_load(prepared)
