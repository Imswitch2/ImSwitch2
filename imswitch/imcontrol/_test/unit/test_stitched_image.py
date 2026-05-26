import numpy as np

from imswitch.imcontrol.model.workflows import StitchedImage


def test_stitched_image_uses_pixel_size_for_tile_step_and_means_overlaps():
    stitcher = StitchedImage(
        tile_size_px=None,
        tile_step_um=2.0,
        px_per_um=None,
        tile_shape_px=(4, 4),
        pixel_size_um=(1.0, 1.0),
        blend_overlaps=True,
    )

    stitcher.add_tile(np.ones((4, 4), dtype=np.float32), 0, 0)
    stitcher.add_tile(np.zeros((4, 4), dtype=np.float32), 1, 0)

    overview = stitcher.get_overview()

    assert overview.shape == (4, 6)
    np.testing.assert_allclose(overview[:, :2], 1.0)
    np.testing.assert_allclose(overview[:, 2:4], 0.5)
    np.testing.assert_allclose(overview[:, 4:], 0.0)


def test_stitched_image_supports_anisotropic_pixels():
    stitcher = StitchedImage(
        tile_size_px=None,
        tile_step_um=2.0,
        px_per_um=None,
        tile_shape_px=(4, 6),
        pixel_size_um=(0.5, 1.0),
    )

    stitcher.add_tile(np.ones((4, 6), dtype=np.float32), 0, 0)
    stitcher.add_tile(np.ones((4, 6), dtype=np.float32), 1, 1)

    overview = stitcher.get_overview()

    assert stitcher.step_x_px == 2
    assert stitcher.step_y_px == 4
    assert overview.shape == (8, 8)


def test_stitched_image_can_match_overlap_intensity():
    stitcher = StitchedImage(
        tile_size_px=None,
        tile_step_um=2.0,
        px_per_um=None,
        tile_shape_px=(4, 4),
        pixel_size_um=(1.0, 1.0),
        blend_overlaps=True,
        intensity_correction=True,
    )

    stitcher.add_tile(np.full((4, 4), 0.5, dtype=np.float32), 0, 0)
    stitcher.add_tile(np.ones((4, 4), dtype=np.float32), 1, 0)

    overview = stitcher.get_overview()

    np.testing.assert_allclose(overview, 0.5)


def test_stitched_image_converts_between_pixel_and_stage_coordinates():
    stitcher = StitchedImage(
        tile_size_px=None,
        tile_step_um=2.0,
        px_per_um=None,
        tile_shape_px=(4, 4),
        pixel_size_um=(0.5, 1.0),
    )

    stage_x, stage_y = stitcher.pixel_to_stage(4, 3, (10.0, 20.0))
    row, col = stitcher.stage_to_pixel(stage_x, stage_y, (10.0, 20.0))

    assert (stage_x, stage_y) == (13.0, 22.0)
    assert (row, col) == (4, 3)
