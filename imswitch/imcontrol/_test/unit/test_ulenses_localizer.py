import importlib
import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.algorithms.grid_localization import LOCALIZATION_DEFAULTS
from imswitch.imcontrol.model.ulenses_localizer import (
    GridLocalizationResult,
    LocalizationResult,
    localize_grid,
    localizer,
    refine_grid_foci,
    search_missing_grid_foci,
)

ulenses_controller_module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.ULensesController'
)


def _render_grid(
    *,
    shape=(180, 220),
    n_cols=6,
    n_rows=5,
    origin=(42.0, 38.0),
    basis_x=(4.0, 24.0),
    basis_y=(22.0, 3.0),
    stagger_axis=None,
    stagger_fraction=0.0,
    sigma=1.2,
    missing=(),
):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    image = np.zeros(shape, dtype=np.float64)
    points = []
    origin = np.asarray(origin, dtype=float)
    basis_x = np.asarray(basis_x, dtype=float)
    basis_y = np.asarray(basis_y, dtype=float)
    missing = set(missing)
    for j in range(n_rows):
        for i in range(n_cols):
            point = origin + i * basis_x + j * basis_y
            if stagger_axis == "rows":
                point = point + (j % 2) * stagger_fraction * basis_x
            elif stagger_axis == "columns":
                point = point + (i % 2) * stagger_fraction * basis_y
            points.append(point)
            if (i, j) in missing:
                continue
            image += np.exp(
                -((yy - point[0]) ** 2 + (xx - point[1]) ** 2) / (2 * sigma**2)
            )
    return image, np.asarray(points)

def test_localize_grid_accepts_complete_shared_parameter_contract():
    """The uLenses/Transform shared settings must bind directly to the API."""
    signature = inspect.signature(localize_grid)
    signature.bind_partial(
        np.zeros((8, 8)),
        **dict(LOCALIZATION_DEFAULTS),
    )
    for name, expected in LOCALIZATION_DEFAULTS.items():
        assert signature.parameters[name].default == expected


def test_localize_grid_keeps_pre_shared_parameter_aliases_compatible():
    image, _ = _render_grid()
    canonical = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
        spot_threshold_rel=0.08,
        spot_blur_sigma=0.9,
        spot_min_distance_fraction=0.30,
    )
    legacy = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
        threshold_rel=0.08,
        gaussian_sigma=0.9,
        min_distance_fraction=0.30,
    )
    np.testing.assert_allclose(canonical.grid_points_rc, legacy.grid_points_rc)
    assert canonical.matched_count == legacy.matched_count


def test_localizer_recovers_grid_periods_from_initial_guesses():
    height, width = 160, 192
    y, x = np.mgrid[0:height, 0:width]
    image = (
        np.cos(np.pi * (x - 2.3) / 8.0) ** 6
        * np.cos(np.pi * (y - 4.1) / 11.0) ** 6
    )

    result = localizer(image, xp_guess=8.0, yp_guess=11.0)

    assert result.xp == pytest.approx(8.0, abs=0.15)
    assert result.yp == pytest.approx(11.0, abs=0.15)
    assert result.xo == pytest.approx(2.3, abs=0.6)
    assert result.yo == pytest.approx(4.1, abs=0.6)


@pytest.mark.parametrize("stagger_axis", [None, "rows", "columns"])
def test_localize_grid_uses_manual_finite_shape_and_recovers_tilt_stagger(stagger_axis):
    fraction = 0.35 if stagger_axis else 0.0
    image, _ = _render_grid(
        stagger_axis=stagger_axis,
        stagger_fraction=fraction,
    )

    result = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
    )

    assert result.shape == (5, 6)
    assert result.diagnostics["grid_size_source"] == "manual"
    assert result.x_period_px == pytest.approx(np.hypot(4, 24), abs=0.5)
    assert result.y_period_px == pytest.approx(np.hypot(22, 3), abs=0.5)
    assert result.angle_deg == pytest.approx(np.degrees(np.arctan2(4, 24)), abs=1.0)
    assert result.stagger_axis == stagger_axis
    assert result.stagger_fraction == pytest.approx(fraction, abs=0.06)
    assert result.diagnostics["fit_rms_px"] < 0.5


def test_localize_grid_auto_infers_finite_shape():
    image, _ = _render_grid(stagger_axis="rows", stagger_fraction=0.4)

    result = localize_grid(image, xp_guess=24.3, yp_guess=22.2)

    assert result.shape == (5, 6)
    assert result.diagnostics["grid_size_source"] == "auto"
    assert result.stagger_axis == "rows"
    assert result.stagger_fraction == pytest.approx(0.4, abs=0.06)


def test_localize_grid_manual_counts_are_authoritative_but_allow_missing_sites():
    image, _ = _render_grid()

    result = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=7,
        n_rows=5,
    )

    assert result.shape == (5, 7)
    assert result.diagnostics["grid_size_source"] == "manual"
    assert result.missing_count > 0


def test_refine_grid_foci_preserves_grid_index_order():
    image, expected = _render_grid(stagger_axis="columns", stagger_fraction=0.3)
    grid = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
    )

    refined = refine_grid_foci(image, grid, search_radius_px=4)

    assert refined.shape == expected.shape
    assert np.max(np.linalg.norm(refined - expected, axis=1)) < 0.5


def test_localize_grid_manual_shape_tolerates_one_missing_site():
    image, _ = _render_grid(missing={(2, 3)})

    result = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
    )

    assert result.shape == (5, 6)
    assert result.matched_count == 29
    assert result.missing_count == 1
    np.testing.assert_array_equal(result.logical_indices[~result.matched_mask], [[2, 3]])


def test_localize_grid_auto_shape_tolerates_one_missing_site():
    image, _ = _render_grid(missing={(2, 3)})

    result = localize_grid(image, xp_guess=24.3, yp_guess=22.2)

    assert result.shape == (5, 6)
    assert result.matched_count == 29
    assert result.missing_count == 1


def test_localize_grid_auto_crop_ignores_dim_outside_distractors():
    image, _ = _render_grid(shape=(320, 360), origin=(100.0, 105.0))
    image[15, 20] = 0.25
    image[290, 330] = 0.30

    result = localize_grid(image, xp_guess=24.3, yp_guess=22.2)

    y0, y1, x0, x1 = result.diagnostics["crop_coord"]
    assert result.shape == (5, 6)
    assert y0 > 15 and x0 > 20
    assert y1 < 290 and x1 < 330
    assert result.matched_count == 30


def test_localize_grid_exact_half_stagger_keeps_overlay_on_spots():
    n_cols = n_rows = 41
    period = 11.0
    origin = np.array([70.0, 80.0])
    image = np.zeros((620, 620), dtype=np.float64)
    expected = []
    for j in range(n_rows):
        for i in range(n_cols):
            point = np.array([
                origin[0] + j * period,
                origin[1] + (i + 0.5 * (j % 2)) * period,
            ])
            expected.append(point)
            image[int(round(point[0])), int(round(point[1]))] = 1.0
    from scipy.ndimage import gaussian_filter
    image = gaussian_filter(image, 1.0)

    result = localize_grid(
        image,
        xp_guess=period,
        yp_guess=period,
        n_cols=n_cols,
        n_rows=n_rows,
        stagger_mode="auto",
        stagger_axis="auto",
        crop_threshold=0.05,
        spot_threshold_rel=0.05,
    )

    assert result.matched_count == n_cols * n_rows
    assert result.stagger_axis == "rows"
    assert result.stagger_fraction == pytest.approx(0.5, abs=0.02)
    expected = np.asarray(expected)
    # This is the regression that the lighter implementation failed: the
    # predicted overlay itself must land on the physical spots.
    distances = np.linalg.norm(result.grid_points_rc - expected, axis=1)
    assert np.median(distances) < 0.05
    assert np.max(distances) < 0.1


def test_search_missing_grid_foci_recovers_local_image_evidence():
    image, expected = _render_grid(missing={(2, 3)})
    missing_point = expected[3 * 6 + 2]
    yy, xx = np.mgrid[0:image.shape[0], 0:image.shape[1]]
    image += 0.07 * np.exp(
        -((yy - missing_point[0]) ** 2 + (xx - missing_point[1]) ** 2)
        / (2 * 1.2**2)
    )
    grid = localize_grid(
        image,
        xp_guess=24.3,
        yp_guess=22.2,
        n_cols=6,
        n_rows=5,
        spot_threshold_rel=0.10,
    )
    assert grid.missing_count == 1

    recovered = search_missing_grid_foci(
        image, grid, search_radius_px=5, threshold_rel=0.02
    )

    assert recovered.missing_count == 0
    assert recovered.matched_count == 30


def test_controller_uses_current_periodicities_as_localizer_guesses(monkeypatch):
    calls = {}

    def fake_localizer(image, *, xp_guess, yp_guess):
        calls['image'] = image
        calls['xp_guess'] = xp_guess
        calls['yp_guess'] = yp_guess
        return LocalizationResult(
            xp=7.6,
            xo=1.25,
            yp=7.4,
            yo=2.5,
            nx_c=1,
            ny_c=1,
            num_cols=10,
            num_rows=10,
        )

    monkeypatch.setattr(ulenses_controller_module, 'localizer', fake_localizer)

    image = np.ones((10, 10))
    set_parameters = {}

    class Widget:
        def getParameters(self):
            return 0.0, 0.0, 157.5, 1182.0, 1200.0

        def getLocalizationMethod(self):
            return 'legacy'

        def setParameters(self, **kwargs):
            set_parameters.update(kwargs)

        def setLocalizationStatus(self, *_args, **_kwargs):
            pass

    fake_controller = SimpleNamespace(
        _widget=Widget(),
        _commChannel=SimpleNamespace(get_image=lambda: image),
        _logger=SimpleNamespace(warning=lambda _msg: None),
    )
    fake_controller._syncPixelSizeFromDetector = lambda: 157.5
    fake_controller.updateGrid = lambda: calls.__setitem__('updated', True)

    ulenses_controller_module.ULensesController.localize(fake_controller)

    assert calls['image'] is image
    assert calls['xp_guess'] == pytest.approx(1182.0 / 157.5)
    assert calls['yp_guess'] == pytest.approx(1200.0 / 157.5)
    assert set_parameters == {
        'x': 1.25,
        'y': 2.5,
        'px': 157.5,
        'upx': round(7.6 * 157.5, 2),
        'upy': round(7.4 * 157.5, 2),
    }
    assert calls['updated'] is True



def test_controller_grid_method_uses_manual_shape_and_preserves_legacy_offsets(monkeypatch):
    calls = {}
    grid = GridLocalizationResult(
        origin_rc=np.array([10.0, 20.0]),
        basis_x_rc=np.array([2.0, 8.0]),
        basis_y_rc=np.array([7.0, -1.0]),
        n_cols=6,
        n_rows=5,
        stagger_axis="rows",
        stagger_fraction=0.5,
        grid_points_rc=np.array([
            [10.0, 20.0], [12.0, 28.0], [14.0, 36.0], [16.0, 44.0], [18.0, 52.0], [20.0, 60.0],
            [18.0, 23.0], [20.0, 31.0], [22.0, 39.0], [24.0, 47.0], [26.0, 55.0], [28.0, 63.0],
            [24.0, 18.0], [26.0, 26.0], [28.0, 34.0], [30.0, 42.0], [32.0, 50.0], [34.0, 58.0],
            [32.0, 21.0], [34.0, 29.0], [36.0, 37.0], [38.0, 45.0], [40.0, 53.0], [42.0, 61.0],
            [38.0, 16.0], [40.0, 24.0], [42.0, 32.0], [44.0, 40.0], [46.0, 48.0], [48.0, 56.0],
        ]),
        logical_indices=np.array([(i, j) for j in range(5) for i in range(6)]),
        detected_points_rc=np.zeros((30, 2)),
        matched_mask=np.ones(30, dtype=bool),
        measured_points_rc=np.array([
            [10.0, 20.0], [12.0, 28.0], [14.0, 36.0], [16.0, 44.0], [18.0, 52.0], [20.0, 60.0],
            [18.0, 23.0], [20.0, 31.0], [22.0, 39.0], [24.0, 47.0], [26.0, 55.0], [28.0, 63.0],
            [24.0, 18.0], [26.0, 26.0], [28.0, 34.0], [30.0, 42.0], [32.0, 50.0], [34.0, 58.0],
            [32.0, 21.0], [34.0, 29.0], [36.0, 37.0], [38.0, 45.0], [40.0, 53.0], [42.0, 61.0],
            [38.0, 16.0], [40.0, 24.0], [42.0, 32.0], [44.0, 40.0], [46.0, 48.0], [48.0, 56.0],
        ]),
        num_cols=6,
        num_rows=5,
        diagnostics={"fit_rms_px": 0.23, "grid_size_source": "manual"},
    )

    def fake_localize_grid(image, **kwargs):
        calls['image'] = image
        calls['kwargs'] = kwargs
        return grid

    monkeypatch.setattr(ulenses_controller_module, 'localize_grid', fake_localize_grid)
    image = np.ones((80, 90))
    updates = []
    statuses = []

    class Widget:
        def getParameters(self):
            return 3.0, 4.0, 150.0, 1200.0, 1050.0

        def getLocalizationMethod(self):
            return 'grid'

        def getGridSize(self):
            return 6, 5

        def getGridLocalizationSettings(self):
            return {"registration_mode": "finite_grid", "spot_threshold_rel": 0.2}

        def setParameters(self, **kwargs):
            updates.append(kwargs)

        def setLocalizationStatus(self, text, **kwargs):
            statuses.append((text, kwargs))

    fake_controller = SimpleNamespace(
        _widget=Widget(),
        _commChannel=SimpleNamespace(get_image=lambda: image),
        _logger=SimpleNamespace(warning=lambda _msg: None),
        _gridLocalizationResult=None,
    )
    fake_controller._syncPixelSizeFromDetector = lambda: 150.0
    fake_controller.updateGrid = lambda: calls.__setitem__('updated', True)

    ulenses_controller_module.ULensesController.localize(fake_controller)

    assert calls['image'] is image
    assert calls['kwargs']['xp_guess'] == pytest.approx(8.0)
    assert calls['kwargs']['yp_guess'] == pytest.approx(7.0)
    assert calls['kwargs']['n_cols'] == 6
    assert calls['kwargs']['n_rows'] == 5
    assert calls['kwargs']['registration_mode'] == 'finite_grid'
    assert calls['kwargs']['spot_threshold_rel'] == pytest.approx(0.2)
    assert fake_controller._gridLocalizationResult is grid
    # New localization updates physical periods only; legacy offsets survive.
    assert updates == [{
        'px': 150.0,
        'upx': round(grid.x_period_px * 150.0, 2),
        'upy': round(grid.y_period_px * 150.0, 2),
    }]
    assert 'Grid 6x5' in statuses[-1][0]
    assert 'rows stagger 0.50' in statuses[-1][0]
    assert calls['updated'] is True


def test_controller_grid_method_auto_passes_no_shape(monkeypatch):
    calls = {}
    image, _ = _render_grid()
    real_grid = localize_grid(image, xp_guess=24.3, yp_guess=22.2)

    def fake_localize_grid(_image, **kwargs):
        calls.update(kwargs)
        return real_grid

    monkeypatch.setattr(ulenses_controller_module, 'localize_grid', fake_localize_grid)

    class Widget:
        def getParameters(self):
            return 0.0, 0.0, 150.0, 3645.0, 3330.0

        def getLocalizationMethod(self):
            return 'grid'

        def getGridSize(self):
            return None, None

        def getGridLocalizationSettings(self):
            return {}

        def setParameters(self, **_kwargs):
            pass

        def setLocalizationStatus(self, *_args, **_kwargs):
            pass

    fake_controller = SimpleNamespace(
        _widget=Widget(),
        _commChannel=SimpleNamespace(get_image=lambda: image),
        _logger=SimpleNamespace(warning=lambda _msg: None),
        _gridLocalizationResult=None,
    )
    fake_controller._syncPixelSizeFromDetector = lambda: 150.0
    fake_controller.updateGrid = lambda: None

    ulenses_controller_module.ULensesController.localize(fake_controller)

    assert calls['n_cols'] is None
    assert calls['n_rows'] is None


def test_grid_overlay_uses_col_as_x_and_row_as_y():
    points = np.array([[11.0, 21.0], [31.0, 41.0], [51.0, 61.0]])
    grid = GridLocalizationResult(
        origin_rc=points[0],
        basis_x_rc=np.array([20.0, 20.0]),
        basis_y_rc=np.array([40.0, 40.0]),
        n_cols=3,
        n_rows=1,
        stagger_axis=None,
        stagger_fraction=0.0,
        grid_points_rc=points,
        logical_indices=np.array([[0, 0], [1, 0], [2, 0]]),
        detected_points_rc=points,
        matched_mask=np.ones(3, dtype=bool),
        measured_points_rc=points,
        num_cols=3,
        num_rows=1,
    )
    shown = {}

    class Widget:
        def getLocalizationMethod(self):
            return 'grid'

        def setData(self, *, x, y):
            shown['x'] = np.asarray(x)
            shown['y'] = np.asarray(y)

    fake_controller = SimpleNamespace(
        _widget=Widget(),
        _gridLocalizationResult=grid,
    )
    fake_controller._syncPixelSizeFromDetector = lambda: None

    ulenses_controller_module.ULensesController.updateGrid(fake_controller)

    np.testing.assert_array_equal(shown['x'], points[:, 1])
    np.testing.assert_array_equal(shown['y'], points[:, 0])


def test_detector_pixel_size_uses_current_detector_and_camera_binning():
    source = SimpleNamespace(value='setup file')
    detector = SimpleNamespace(
        pixelSizeUm=[1.0, 0.1575, 0.1575],
        binning=2,
        isScanDriven=False,
        parameters={'Camera pixel size source': source},
    )

    class Manager:
        def getCurrentDetectorName(self):
            return 'CAM'

        def execOnCurrent(self, fn):
            return fn(detector)

    fake_controller = SimpleNamespace(
        _master=SimpleNamespace(detectorsManager=Manager()),
    )

    pixel_nm, detector_name, pixel_source = (
        ulenses_controller_module.ULensesController._detectorPixelSizeNm(fake_controller)
    )

    assert pixel_nm == pytest.approx(315.0)
    assert detector_name == 'CAM'
    assert pixel_source == 'setup file'


def test_detector_pixel_size_rejects_anisotropic_pixels():
    detector = SimpleNamespace(
        pixelSizeUm=[1.0, 0.15, 0.20],
        binning=1,
        isScanDriven=False,
        parameters={},
    )

    class Manager:
        def getCurrentDetectorName(self):
            return 'CAM'

        def execOnCurrent(self, fn):
            return fn(detector)

    fake_controller = SimpleNamespace(
        _master=SimpleNamespace(detectorsManager=Manager()),
    )

    with pytest.raises(ValueError, match='anisotropic'):
        ulenses_controller_module.ULensesController._detectorPixelSizeNm(fake_controller)



def test_detector_assumed_default_pixel_size_keeps_manual_fallback():
    detector = SimpleNamespace(
        pixelSizeUm=[1.0, 0.15, 0.15],
        binning=1,
        isScanDriven=False,
        parameters={
            'Camera pixel size source': SimpleNamespace(value='assumed default')
        },
    )

    class Manager:
        def getCurrentDetectorName(self):
            return 'CAM'

        def execOnCurrent(self, fn):
            return fn(detector)

    fake_controller = SimpleNamespace(
        _master=SimpleNamespace(detectorsManager=Manager()),
    )

    with pytest.raises(ValueError, match='assumed default'):
        ulenses_controller_module.ULensesController._detectorPixelSizeNm(fake_controller)


def test_controller_grid_can_localize_selected_napari_image(monkeypatch):
    """Offline image source must never request a mock/live detector frame."""
    image = np.ones((80, 110), dtype=float)
    points = np.array([[10.0, 20.0], [17.0, 39.0], [26.0, 58.0]])
    grid = GridLocalizationResult(
        origin_rc=points[0],
        basis_x_rc=np.array([7.0, 19.0]),
        basis_y_rc=np.array([9.0, 0.0]),
        n_cols=3,
        n_rows=1,
        stagger_axis=None,
        stagger_fraction=0.0,
        grid_points_rc=points,
        logical_indices=np.array([[0, 0], [1, 0], [2, 0]]),
        detected_points_rc=points,
        matched_mask=np.ones(3, dtype=bool),
        measured_points_rc=points,
        num_cols=3,
        num_rows=1,
    )
    calls = {}

    def fake_localize(image_input, **kwargs):
        calls['image'] = image_input
        calls['kwargs'] = kwargs
        return grid

    monkeypatch.setattr(ulenses_controller_module, 'localize_grid', fake_localize)

    layer = SimpleNamespace(
        name='imported',
        data_to_world=lambda rc: (rc[0] * 2.0 + 10.0, rc[1] * 3.0 + 20.0),
    )

    class Widget:
        def getImageSource(self):
            return 'napari'

        def getParameters(self):
            return 0., 0., 100., 2000., 1800.

        def getLocalizationMethod(self):
            return 'grid'

        def getGridSize(self):
            return None, None

        def getGridLocalizationSettings(self):
            return {}

        def setParameters(self, **kwargs):
            calls['parameters'] = kwargs

        def setLocalizationStatus(self, status, **kwargs):
            calls['status'] = status

        def setWorldData(self, x, y):
            calls['world_x'] = np.asarray(x)
            calls['world_y'] = np.asarray(y)

    def no_detector_frame():
        raise AssertionError('detector should not be accessed in Napari mode')

    controller = SimpleNamespace(
        _widget=Widget(),
        _commChannel=SimpleNamespace(get_image=no_detector_frame),
        _imageWidget=SimpleNamespace(getActiveImageLayerSnapshot=lambda: (image, layer)),
        _logger=SimpleNamespace(warning=lambda _: None),
        _gridLocalizationResult=None,
        _napariSourceLayer=None,
        _napariImageShape=None,
    )
    controller._syncPixelSizeFromDetector = lambda: None
    controller._showGridPoints = lambda pts: (
        ulenses_controller_module.ULensesController._showGridPoints(controller, pts)
    )
    controller._napariWorldCoordinates = lambda src, pts: (
        ulenses_controller_module.ULensesController._napariWorldCoordinates(src, pts)
    )
    controller.updateGrid = lambda: (
        ulenses_controller_module.ULensesController.updateGrid(controller)
    )

    ulenses_controller_module.ULensesController.localize(controller)

    assert calls['image'] is image
    assert calls['kwargs']['xp_guess'] == pytest.approx(20)
    assert calls['kwargs']['yp_guess'] == pytest.approx(18)
    assert controller._napariSourceLayer is layer
    assert controller._napariImageShape == (80, 110)
    np.testing.assert_allclose(calls['world_x'], 3.0 * points[:, 1] + 20.0)
    np.testing.assert_allclose(calls['world_y'], 2.0 * points[:, 0] + 10.0)


def test_napari_layer_change_discards_old_grid():
    events = {}
    widget = SimpleNamespace(
        getImageSource=lambda: 'napari',
        setData=lambda **kwargs: events.setdefault('points', kwargs),
        setLocalizationStatus=lambda status: events.setdefault('status', status),
    )
    controller = SimpleNamespace(
        _widget=widget,
        _gridLocalizationResult=object(),
        _napariSourceLayer=object(),
        _napariImageShape=(80, 110),
    )
    ulenses_controller_module.ULensesController._activeLayerChanged(controller)
    assert controller._gridLocalizationResult is None
    assert controller._napariSourceLayer is None
    assert controller._napariImageShape is None
    assert 'Active Napari layer changed' in events['status']
    assert len(events['points']['x']) == 0
