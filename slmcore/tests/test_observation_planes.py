import json

import pytest

from slmcore.application.observation import ObservationPlaneService
from slmcore.workspace import ObservationPlaneDefinition,ObservationPlaneStore,SLMWorkspace


def test_observation_plane_store_add_load_delete_known_detector(tmp_path):
    store=ObservationPlaneStore(tmp_path)
    events=[]; store.add_listener(lambda: events.append("changed"))
    plane=store.add(ObservationPlaneDefinition(
        name="Camera A",preferred_detector="cam_a",default_pixel_size_um=0.108,
        description="Primary view",
    ))
    assert plane.preferred_detector == "cam_a"
    loaded=ObservationPlaneStore(tmp_path).get("Camera A")
    assert loaded == plane
    assert json.loads((tmp_path/"observation_planes.json").read_text())["version"] == 1
    assert store.delete("Camera A") == plane
    assert store.list() == ()
    assert events == ["changed","changed"]


def test_observation_plane_store_external_custom_and_update(tmp_path):
    service=ObservationPlaneService(ObservationPlaneStore(tmp_path))
    plane=service.add(ObservationPlaneDefinition(
        name="External",preferred_detector=None,default_pixel_size_um=0.25,
    ))
    assert plane.preferred_detector is None
    updated=service.update("External",default_pixel_size_um=0.5,description="Loaded file")
    assert updated.default_pixel_size_um == 0.5
    assert ObservationPlaneStore(tmp_path).get("External") == updated
    with pytest.raises(ValueError,match="Renaming"):
        service.update("External",name="Other")


@pytest.mark.parametrize("value",[0,-1,float("nan"),float("inf")])
def test_observation_plane_pixel_size_must_be_positive_finite(value):
    with pytest.raises(ValueError,match="positive finite"):
        ObservationPlaneDefinition(name="View",default_pixel_size_um=value)


def test_workspace_observation_plane_store_is_root_scoped_and_independent(tmp_path):
    workspace=SLMWorkspace(tmp_path)
    workspace.observation_plane_store.add(ObservationPlaneDefinition(name="External"))
    assert (tmp_path/"observation_planes.json").is_file()
    assert not (tmp_path/"mappings.json").exists()
    assert workspace.mapping_store.names == ()


def test_observation_plane_crud_does_not_mutate_runtime_mapping_or_cgh(tmp_path):
    import numpy as np
    from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
    from slmcore.core.engine.section import split_slm_geometry

    geometry=SLMGeometry(width=32,height=32,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","OBS-INDEPENDENCE"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),registries=DEFAULT_REGISTRIES,
    )
    before=runtime.get_section_snapshot("sec_0")
    frame_before=np.array(runtime.artifacts.eightbit,copy=True)
    service=ObservationPlaneService(ObservationPlaneStore(tmp_path))
    service.add(ObservationPlaneDefinition(name="View",default_pixel_size_um=0.1))
    service.update("View",description="edited")
    service.delete("View")
    after=runtime.get_section_snapshot("sec_0")
    assert after == before
    assert np.array_equal(runtime.artifacts.eightbit,frame_before)
