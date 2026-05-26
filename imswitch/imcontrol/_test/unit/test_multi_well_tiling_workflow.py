"""Unit tests for MultiWellTilingWorkflow."""

from pathlib import Path
from unittest.mock import Mock, call
import tempfile

import pytest

from imswitch.imcontrol.model.workflows import (
    MultiWellTilingWorkflow,
    MultiWellTilingParams,
)
from imswitch.imcontrol.model.workflows.paths import default_measurements_root


def build_mock_facade():
    """Create a mock MicroscopeFacade with minimal stage interfaces."""
    facade = Mock()
    
    # Mock stage_con with position tracking
    facade.stage_con = Mock()
    facade.stage_con._position = [1000.0, 2000.0]  # Initial position
    
    def mock_move_to(x, y):
        facade.stage_con._position = [x, y]
    
    def mock_get_position():
        return tuple(facade.stage_con._position)
    
    facade.stage_con.move_to = Mock(side_effect=mock_move_to)
    facade.stage_con.get_position = Mock(side_effect=mock_get_position)
    
    # Mock z_stage_con (required for validation)
    facade.z_stage_con = Mock()
    
    return facade


def build_mock_tiling_workflow():
    """Create a mock TilingWorkflow."""
    wf = Mock()
    wf.run = Mock()
    return wf


def build_mock_zstack_workflow():
    """Create a mock ZStackWorkflow with params."""
    wf = Mock()
    wf.params = Mock()
    wf.params.n_planes = 50  # Default value
    wf.params.step_um = 1.0  # Default value
    wf.run_autofocus = Mock()
    return wf


# ---------------------------------------------------------------------------
# Tests: Parameter validation and construction
# ---------------------------------------------------------------------------


def test_multi_well_tiling_params_defaults():
    """MultiWellTilingParams should provide reasonable defaults."""
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=3,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
    )
    assert params.n_rows == 2
    assert params.n_cols == 3
    assert params.well_pitch_x_units == 5000.0
    assert params.well_pitch_y_units == 5000.0
    assert params.autofocus_n_planes == 10
    assert params.autofocus_step_um == 2.0
    assert params.autofocus_z_center_um is None
    assert params.tiling_n_tiles == 9
    assert params.measurements_root == default_measurements_root()


def test_multi_well_tiling_requires_stage_con():
    """Constructor should raise if facade.stage_con is None."""
    facade = Mock()
    facade.stage_con = None
    facade.z_stage_con = Mock()
    
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    params = MultiWellTilingParams(n_rows=2, n_cols=2, well_pitch_x_units=5000, well_pitch_y_units=5000)
    
    with pytest.raises(ValueError, match="requires facade.stage_con"):
        MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)


def test_multi_well_tiling_requires_z_stage_con():
    """Constructor should raise if facade.z_stage_con is None."""
    facade = Mock()
    facade.stage_con = Mock()
    facade.z_stage_con = None
    
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    params = MultiWellTilingParams(n_rows=2, n_cols=2, well_pitch_x_units=5000, well_pitch_y_units=5000)
    
    with pytest.raises(ValueError, match="requires facade.z_stage_con"):
        MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)


# ---------------------------------------------------------------------------
# Tests: Grid iteration and well positioning
# ---------------------------------------------------------------------------


def test_multi_well_tiling_visits_all_wells():
    """run() should visit n_rows * n_cols wells in row-major order."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=3,
        well_pitch_x_units=1000.0,
        well_pitch_y_units=2000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Should move to 6 wells (2 rows × 3 cols)
    # Plus one final move back to origin
    assert facade.stage_con.move_to.call_count == 7
    
    # Verify row-major order: r0c0, r0c1, r0c2, r1c0, r1c1, r1c2, then origin
    expected_calls = [
        call(1000.0, 2000.0),  # r0c0 (origin)
        call(2000.0, 2000.0),  # r0c1
        call(3000.0, 2000.0),  # r0c2
        call(1000.0, 4000.0),  # r1c0
        call(2000.0, 4000.0),  # r1c1
        call(3000.0, 4000.0),  # r1c2
        call(1000.0, 2000.0),  # return to origin
    ]
    facade.stage_con.move_to.assert_has_calls(expected_calls)


def test_multi_well_tiling_calls_autofocus_per_well():
    """run() should call autofocus once per well."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=2,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        autofocus_z_center_um=100.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Should call autofocus 4 times (2×2 grid)
    assert zstack_wf.run_autofocus.call_count == 4
    
    # All calls should use the z_center from params
    for call_obj in zstack_wf.run_autofocus.call_args_list:
        assert call_obj == call(z_start=100.0)


def test_multi_well_tiling_overrides_autofocus_params():
    """run() should temporarily override zstack params during autofocus."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    # Set original params
    zstack_wf.params.n_planes = 100
    zstack_wf.params.step_um = 5.0
    
    params = MultiWellTilingParams(
        n_rows=1,
        n_cols=1,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        autofocus_n_planes=15,
        autofocus_step_um=1.5,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # After run completes, params should be restored to original values
    assert zstack_wf.params.n_planes == 100
    assert zstack_wf.params.step_um == 5.0


def test_multi_well_tiling_calls_tiling_per_well():
    """run() should call tiling.run() once per well with correct save_folder."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    temp_root = Path(tempfile.mkdtemp())
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=2,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        measurements_root=str(temp_root),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Should call tiling 4 times (2×2 grid)
    assert tiling_wf.run.call_count == 4
    
    # Verify save_folder arguments in row-major order
    expected_folders = [
        temp_root / "well_r0_c0",
        temp_root / "well_r0_c1",
        temp_root / "well_r1_c0",
        temp_root / "well_r1_c1",
    ]
    for i, expected_folder in enumerate(expected_folders):
        call_obj = tiling_wf.run.call_args_list[i]
        assert call_obj.kwargs["save_folder"] == expected_folder


def test_multi_well_tiling_uses_default_root_when_params_root_is_none(monkeypatch, tmp_path):
    """measurements_root=None should resolve before creating well folders."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    monkeypatch.setenv("IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT", str(tmp_path))
    params = MultiWellTilingParams(
        n_rows=1,
        n_cols=1,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        measurements_root=None,
    )

    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()

    assert tiling_wf.run.call_args.kwargs["save_folder"] == tmp_path / "well_r0_c0"


# ---------------------------------------------------------------------------
# Tests: Error handling and recovery
# ---------------------------------------------------------------------------


def test_multi_well_tiling_continues_on_autofocus_error():
    """run() should continue to next well if autofocus raises."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    # Make autofocus fail on second well
    call_count = [0]
    
    def autofocus_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("Autofocus failed at well 2")
    
    zstack_wf.run_autofocus.side_effect = autofocus_side_effect
    
    params = MultiWellTilingParams(
        n_rows=1,
        n_cols=3,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()  # Should not raise
    
    # Should still attempt autofocus at all 3 wells
    assert zstack_wf.run_autofocus.call_count == 3
    
    # Should only call tiling twice (well 2 failed before tiling)
    assert tiling_wf.run.call_count == 2


def test_multi_well_tiling_continues_on_tiling_error():
    """run() should continue to next well if tiling raises."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    # Make tiling fail on first well
    call_count = [0]
    
    def tiling_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Tiling failed at well 1")
    
    tiling_wf.run.side_effect = tiling_side_effect
    
    params = MultiWellTilingParams(
        n_rows=1,
        n_cols=3,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()  # Should not raise
    
    # Should still attempt tiling at all 3 wells
    assert tiling_wf.run.call_count == 3


def test_multi_well_tiling_returns_to_origin():
    """run() should return stage to origin position after completing grid."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    # Set a specific origin
    facade.stage_con._position = [12345.0, 67890.0]
    
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=2,
        well_pitch_x_units=1000.0,
        well_pitch_y_units=1000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Final position should match origin
    final_pos = facade.stage_con.get_position()
    assert final_pos == (12345.0, 67890.0)


def test_multi_well_tiling_returns_to_origin_even_on_error():
    """run() should return to origin even if all wells fail."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    # Make tiling always fail
    tiling_wf.run.side_effect = RuntimeError("Tiling always fails")
    
    facade.stage_con._position = [99999.0, 88888.0]
    
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=2,
        well_pitch_x_units=1000.0,
        well_pitch_y_units=1000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()  # Should not raise
    
    # Should still return to origin
    final_pos = facade.stage_con.get_position()
    assert final_pos == (99999.0, 88888.0)


# ---------------------------------------------------------------------------
# Tests: Integration scenarios
# ---------------------------------------------------------------------------


def test_multi_well_tiling_single_well():
    """run() should work correctly with 1×1 grid (single well)."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    params = MultiWellTilingParams(
        n_rows=1,
        n_cols=1,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Should call autofocus once
    assert zstack_wf.run_autofocus.call_count == 1
    
    # Should call tiling once
    assert tiling_wf.run.call_count == 1
    
    # Should move to well (origin) then return (2 moves total)
    assert facade.stage_con.move_to.call_count == 2


def test_multi_well_tiling_asymmetric_grid():
    """run() should handle non-square grids correctly."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    params = MultiWellTilingParams(
        n_rows=3,
        n_cols=5,
        well_pitch_x_units=1000.0,
        well_pitch_y_units=2000.0,
        measurements_root=tempfile.mkdtemp(),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Should process 15 wells (3×5)
    assert zstack_wf.run_autofocus.call_count == 15
    assert tiling_wf.run.call_count == 15


def test_multi_well_tiling_creates_well_folders():
    """run() should create subdirectories for each well."""
    facade = build_mock_facade()
    tiling_wf = build_mock_tiling_workflow()
    zstack_wf = build_mock_zstack_workflow()
    
    temp_root = Path(tempfile.mkdtemp())
    params = MultiWellTilingParams(
        n_rows=2,
        n_cols=2,
        well_pitch_x_units=5000.0,
        well_pitch_y_units=5000.0,
        measurements_root=str(temp_root),
    )
    
    wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, params)
    wf.run()
    
    # Verify all well folders were created
    assert (temp_root / "well_r0_c0").is_dir()
    assert (temp_root / "well_r0_c1").is_dir()
    assert (temp_root / "well_r1_c0").is_dir()
    assert (temp_root / "well_r1_c1").is_dir()
