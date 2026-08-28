import h5py
import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.config import SLM_CONFIG_SCHEMA_VERSION
from slmcore.workspace import SLMConfigStore
from slmcore.core.engine.section import split_slm_geometry


def _runtime():
    geometry = SLMGeometry(width=16,height=8,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2),
        registries=DEFAULT_REGISTRIES,
    )


def test_store_reads_compiled_frame_without_reconstructing_sections(tmp_path):
    runtime = _runtime()
    store = SLMConfigStore(tmp_path,DEFAULT_REGISTRIES)
    config = runtime.create_config()
    expected = np.arange(
        config.geometry.height * config.geometry.width,dtype=np.uint8,
    ).reshape(config.geometry.shape)
    config.final_eightbit = expected
    store.save("compiled.h5",config,"compiled",overwrite=False)

    compiled = store.read_compiled_frame("compiled.h5")

    assert compiled.identity == runtime.identity
    assert compiled.geometry == runtime.geometry
    np.testing.assert_array_equal(compiled.final_eightbit,expected)
    assert compiled.final_eightbit.dtype == np.uint8
    assert not compiled.final_eightbit.flags.writeable


def test_compiled_frame_reader_requires_current_schema(tmp_path):
    runtime = _runtime()
    store = SLMConfigStore(tmp_path,DEFAULT_REGISTRIES)
    path = store.resolve("compiled.h5")
    store.save(path,runtime.create_config(),"compiled",overwrite=False)

    with h5py.File(str(path),"r+") as handle:
        group = handle["config"]
        if "schema_version" in group:
            del group["schema_version"]
        group.attrs["schema_version"] = str(SLM_CONFIG_SCHEMA_VERSION - 1)

    with pytest.raises(ValueError,match="requires schema version"):
        store.read_compiled_frame(path)
