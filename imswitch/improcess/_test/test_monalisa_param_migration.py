"""MoNaLISA parameter contract and historical replay regressions."""

import pytest

from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
from imswitch.improcess.reconstructors.monalisa.params import default_params
from imswitch.improcess.model.param_spec import spec_problems


def test_parameter_spec_includes_all_defaults_and_spot_source():
    defaults = MonalisaReconstructor.default_params()
    assert spec_problems(MonalisaReconstructor) == []
    fields = {field.key: field for field in MonalisaReconstructor.param_spec()}
    assert set(fields) == set(defaults)
    assert fields["spot_width_source"].default == defaults["spot_width_source"]
    assert fields["spot_width_source"].type == "text"


def test_sparse_pre_v6_jobs_preserve_old_off_defaults():
    plugin = MonalisaReconstructor()
    assert plugin.params_version == 6
    for version in (1, 4, 5):
        previous = {"preset": "Fast", "spot_fwhm": 220.0}
        migrated = plugin.migrate_params(previous, version)
        assert migrated["intensity_correction"] is False
        assert migrated["cell_offsets"] is False
        assert previous == {"preset": "Fast", "spot_fwhm": 220.0}


@pytest.mark.parametrize("intensity,offsets", [(False, True), (True, False), (True, True), (False, False)])
def test_migration_preserves_explicit_historical_correction_values(intensity, offsets):
    plugin = MonalisaReconstructor()
    explicit = {"intensity_correction": intensity, "cell_offsets": offsets}
    assert plugin.migrate_params(explicit, 5) == explicit


def test_sparse_historical_job_preserves_explicit_single_correction():
    plugin = MonalisaReconstructor()
    migrated = plugin.migrate_params({"intensity_correction": True}, 5)
    assert migrated == {"intensity_correction": True, "cell_offsets": False}


def test_current_version_preserves_new_correction_defaults():
    plugin = MonalisaReconstructor()
    assert plugin.migrate_params({}, plugin.params_version) == {}
    assert plugin.migrate_params(default_params(), plugin.params_version) == default_params()
    assert plugin.default_params()["intensity_correction"] is True
    assert plugin.default_params()["cell_offsets"] is True


def test_replay_decoder_uses_versioned_correction_migration():
    from imswitch.improcess.workflows.replay import _decode

    node = {
        "plugin_id": "monalisa",
        "params_version": 5,
        "params": {"preset": "Fast", "spot_fwhm": 220.0},
    }
    notes = []
    decoded = _decode(node, MonalisaReconstructor(), notes, "historical MoNaLISA")
    assert decoded["intensity_correction"] is False
    assert decoded["cell_offsets"] is False
    assert any("migrated" in note for note in notes)

    current = dict(node, params_version=6)
    notes = []
    decoded = _decode(current, MonalisaReconstructor(), notes, "new MoNaLISA")
    assert "intensity_correction" not in decoded
    assert "cell_offsets" not in decoded
    assert not notes
