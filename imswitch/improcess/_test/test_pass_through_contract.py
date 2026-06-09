"""Architectural contract tests for the Reconstructor.is_pass_through flag.

These tests don't spin up Qt; they only verify the class-level flags that
ImProcessMainViewController consults to decide whether to auto-route data
through to the napari viewer on currentDataChanged / drag-drop.
"""

from imswitch.improcess.reconstructors.base import Reconstructor


def test_reconstructor_base_default_is_not_pass_through():
    assert Reconstructor.is_pass_through is False


def test_view_only_opts_into_pass_through():
    from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor

    assert ViewOnlyReconstructor.is_pass_through is True


def test_modality_reconstructors_are_not_pass_through_by_default():
    """Anything that runs actual reconstruction must keep the explicit click
    gate; otherwise the auto-route would trigger long compute on every load."""
    from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
    from imswitch.improcess.reconstructors.snouty import SnoutyReconstructor
    from imswitch.improcess.reconstructors.snouty_projections import (
        SnoutyProjectionsReconstructor,
    )
    from imswitch.improcess.reconstructors.widefield_starss import (
        WidefieldStarssReconstructor,
    )

    for cls in (
        MonalisaReconstructor,
        SnoutyReconstructor,
        SnoutyProjectionsReconstructor,
        WidefieldStarssReconstructor,
    ):
        assert cls.is_pass_through is False, (
            f"{cls.__name__} must not auto-route — it runs real compute"
        )
