from slmcore import DEFAULT_REGISTRIES


def test_default_registration_composition_is_complete():
    assert tuple(DEFAULT_REGISTRIES.patterns) == (
        "lens_phase",
        "binary_grating",
        "sinusoidal_grating",
        "linear_phase",
        "vortex",
        "top_hat",
        "half_moon_x",
        "half_moon_y",
    )
    assert tuple(DEFAULT_REGISTRIES.aberrations) == ("zernike",)
    assert tuple(DEFAULT_REGISTRIES.targets) == (
        "multi_foci",
        "multi_foci_vector",
    )
    assert tuple(DEFAULT_REGISTRIES.algorithms) == (
        "direct_summation",
        "gerchberg_saxton",
    )
