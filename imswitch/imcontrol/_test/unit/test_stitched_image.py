import numpy as np
import pytest

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


def test_one_overlap_cannot_tell_a_brighter_tile_from_vignetting():
    # Two tiles, one overlap: the right one reads brighter there, which is
    # just what a detector dimmer on its left-hand side would also do. With
    # nothing else to compare against, the correction leaves it as captured
    # rather than guess (it used to match it, and that guess is what tilted
    # every vignetted mosaic). Brightness changes are corrected once other
    # overlaps of the same direction show what the direction itself does.
    def overview(correction):
        stitcher = StitchedImage(
            tile_size_px=None,
            tile_step_um=2.0,
            px_per_um=None,
            tile_shape_px=(4, 4),
            pixel_size_um=(1.0, 1.0),
            blend_overlaps=True,
            intensity_correction=correction,
        )
        stitcher.add_tile(np.full((4, 4), 0.5, dtype=np.float32), 0, 0)
        stitcher.add_tile(np.ones((4, 4), dtype=np.float32), 1, 0)
        return stitcher.get_overview()

    np.testing.assert_allclose(overview(True), overview(False))


def test_stitched_image_converts_between_pixel_and_stage_coordinates():
    stitcher = StitchedImage(
        tile_size_px=None,
        tile_step_um=2.0,
        px_per_um=None,
        tile_shape_px=(4, 4),
        pixel_size_um=(0.5, 1.0),
    )

    stage_x, stage_y = stitcher.pixel_to_stage(4, 3, (10.0, 20.0))

    assert (stage_x, stage_y) == (13.0, 22.0)


# ---------------------------------------------------------------------------
# Intensity correction under detector-fixed shading
# ---------------------------------------------------------------------------
# Found on the rig (2026-11-18): with "Intensity correction" on, the live
# mosaic grew bright on one side and dark on the other. Vignetting is fixed to
# the detector, so a tile's bright edge always overlaps its neighbour's dim
# one; matching those as if they were sample darkened each tile further than
# the last -- 3.2x across five tiles of a 1.0 -> 0.7 falloff. Dividing out an
# estimated profile was not enough on its own: whatever it got wrong at the
# border repeated on every overlap, and a blobby specimen still drifted 35 %.

from scipy.ndimage import gaussian_filter  # noqa: E402

from imswitch.imcontrol.model.workflows.spiral import (  # noqa: E402
    SERPENTINE,
    SPIRAL,
    tile_moves,
)


def _specimen(shape, kind, rng):
    """One specimen the whole mosaic is cut from, flat on average."""
    if kind == 'fine':
        field = 0.5 + 0.05 * rng.standard_normal(shape)
    else:  # 'blobs': cell-sized structure that does not average out quickly
        blobs = gaussian_filter(rng.standard_normal(shape), shape[0] / 60)
        field = 0.5 + 0.15 * blobs / blobs.std() + 0.02 * rng.standard_normal(shape)
    return np.clip(field, 0.02, None).astype(np.float32)


def _run(falloff, *, lamps=(), specimen='fine', nx=7, ny=3, overlap=0.2,
         pattern=SPIRAL, correction=True, finish=True):
    """Tile a shared specimen through ``falloff``; ``lamps`` maps index -> brightness."""
    height, width = falloff.shape
    step = int(round(width * (1 - overlap)))
    rng = np.random.default_rng(0)
    field = _specimen(
        (step * (2 * ny + 3) + height, step * (2 * nx + 3) + width), specimen, rng,
    )
    stitcher = StitchedImage(
        tile_size_px=None, tile_step_um=step, px_per_um=None,
        tile_shape_px=(height, width), pixel_size_um=1.0,
        blend_overlaps=True, intensity_correction=correction,
    )
    gx = gy = 0
    order, brightness = [], []
    for index, (dx, dy) in enumerate(tile_moves(nx, ny, pattern)):
        gx += dx
        gy += dy
        row, col = (ny + 1 + gy) * step, (nx + 1 + gx) * step
        lamp = dict(lamps).get(index, 1.0)
        tile = field[row:row + height, col:col + width] * falloff * 0.8 * lamp
        stitcher.add_tile(np.clip(tile, 0, 1), gx, gy)
        order.append((gx, gy))
        brightness.append(lamp)
    if finish:
        stitcher.finish()
    return stitcher, order, brightness


def _unevenness(stitcher, order, brightness):
    """How far any tile ends up from the rest: 0 is a perfectly level mosaic."""
    shown = np.array([lamp * stitcher._gains.get(key, 1.0)
                      for key, lamp in zip(order, brightness)])
    return float(np.abs(shown / np.median(shown) - 1).max())


def _linear_falloff(height=100, width=100):
    return np.linspace(1.0, 0.7, width, dtype=np.float32)[None, :].repeat(height, 0)


def _rig_falloff(size=128):
    # The rig's measured one-sided falloff, 1.15 -> 0.55 across the field.
    return np.linspace(1.15, 0.55, size, dtype=np.float32)[None, :].repeat(size, 0)


def _off_centre_vignetting(height=100, width=100):
    yy, xx = np.mgrid[0:height, 0:width]
    return (1 - 0.4 * (((xx - 30) / width) ** 2 + ((yy - 50) / height) ** 2)).astype(np.float32)


# Tolerance for "level": the worst case measured over ramp and off-centre
# vignetting, fine and blobby specimens, 10 and 20 % overlap, was 0.3 %.
_LEVEL = 0.01


def test_one_sided_falloff_no_longer_compounds_across_the_mosaic():
    stitcher, order, brightness = _run(_linear_falloff())
    assert stitcher._shading is not None, 'the profile was estimated'
    assert _unevenness(stitcher, order, brightness) < _LEVEL


def test_the_mosaic_looks_like_it_does_with_the_correction_off():
    on, order, _ = _run(_linear_falloff())
    off, _, _ = _run(_linear_falloff(), correction=False)
    assert on.get_overview() == pytest.approx(off.get_overview(), abs=_LEVEL)


def test_a_blobby_specimen_with_little_overlap_stays_level():
    # The case dividing out the profile alone left 35 % uneven.
    stitcher, order, brightness = _run(
        _rig_falloff(), specimen='blobs', overlap=0.1, nx=7, ny=5,
    )
    assert _unevenness(stitcher, order, brightness) < _LEVEL


def test_off_centre_vignetting_stays_level_too():
    stitcher, order, brightness = _run(_off_centre_vignetting(), specimen='blobs')
    assert _unevenness(stitcher, order, brightness) < _LEVEL


def test_a_real_brightness_change_is_still_corrected():
    stitcher, order, brightness = _run(_linear_falloff(), lamps={8: 0.8})
    assert _unevenness(stitcher, order, brightness) < _LEVEL
    others = np.median([stitcher._gains[key] for key in order])
    assert stitcher._gains[order[8]] / others == pytest.approx(1 / 0.8, rel=_LEVEL)


def test_moving_tiles_re_solves_their_gains():
    stitcher, order, _ = _run(_linear_falloff(), nx=3, ny=3, lamps={4: 0.8})
    before = dict(stitcher._gains)
    key = order[4]
    row, col = stitcher.placement(*key)
    stitcher.set_placements({key: (row + 3, col + 3)})
    assert stitcher._gains.keys() == before.keys()
    others = np.median([stitcher._gains[k] for k in order])
    assert stitcher._gains[key] / others == pytest.approx(1 / 0.8, rel=0.03)


def test_full_solves_happen_at_the_estimator_counts_then_on_doubling(monkeypatch):
    solved = []
    original = StitchedImage._full_solve

    def record(self):
        solved.append(len(self._tiles))
        original(self)

    monkeypatch.setattr(StitchedImage, '_full_solve', record)
    _run(_linear_falloff(40, 40), nx=7, ny=6, finish=False)
    assert solved == [4, 9, 18, 36]


def test_finish_solves_again_once_when_tiles_arrived_since():
    stitcher, _, _ = _run(_linear_falloff(), nx=7, ny=3, finish=False)
    assert len(stitcher._tiles) == 21, 'past the last checkpoint (18)'
    assert stitcher.finish() is True
    assert stitcher._fullSolveAt == 21
    assert stitcher.finish() is False


def test_finish_does_nothing_without_the_correction():
    stitcher, _, _ = _run(_linear_falloff(), correction=False, finish=False)
    assert stitcher.finish() is False


def test_finish_levels_a_flickering_serpentine():
    rng = np.random.default_rng(1)
    lamps = {i: 1.0 + 0.05 * rng.standard_normal() for i in range(35)}
    kwargs = dict(lamps=lamps, nx=7, ny=5, pattern=SERPENTINE, overlap=0.15)
    raw = max(abs(v / np.median(list(lamps.values())) - 1) for v in lamps.values())
    stitcher, order, brightness = _run(_linear_falloff(), **kwargs)
    # Flicker is per tile, so part of it is indistinguishable from a
    # direction bias and stays; the run must still end up more level than
    # it was captured.
    assert _unevenness(stitcher, order, brightness) < raw / 2
