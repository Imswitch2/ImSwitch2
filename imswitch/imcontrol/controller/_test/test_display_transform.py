import numpy as np

from imswitch.imcontrol.controller.display_transform import (
    DisplayTransform,
    apply_display_transform,
    display_transform_from_properties,
)


def test_display_transform_rotation_swaps_last_axes_and_scale():
    image = np.array([[1, 2, 3], [4, 5, 6]])

    transformed, scale = apply_display_transform(
        image,
        [0.2, 0.5],
        DisplayTransform(rotation=90),
    )

    np.testing.assert_array_equal(transformed, np.array([[3, 6], [2, 5], [1, 4]]))
    assert scale == [0.5, 0.2]
    assert transformed.flags.c_contiguous


def test_display_transform_flips_after_rotation():
    image = np.array([[1, 2, 3], [4, 5, 6]])

    transformed, scale = apply_display_transform(
        image,
        [1.0, 1.0],
        DisplayTransform(rotation=180, flip_x=True, flip_y=True),
    )

    np.testing.assert_array_equal(transformed, image)
    assert scale == [1.0, 1.0]


def test_display_transform_applies_over_last_two_axes_for_stacks():
    image = np.arange(2 * 2 * 3).reshape(2, 2, 3)

    transformed, scale = apply_display_transform(
        image,
        [2.0, 0.2, 0.5],
        DisplayTransform(rotation=90, flip_x=True),
    )

    expected = np.flip(np.rot90(image, k=1, axes=(-2, -1)), axis=-1)
    np.testing.assert_array_equal(transformed, expected)
    assert scale == [2.0, 0.5, 0.2]


def test_display_transform_properties_support_flat_and_nested_config():
    flat = display_transform_from_properties(
        {"displayRotation": "270", "displayFlipX": "true", "displayFlipY": False}
    )
    nested = display_transform_from_properties(
        {"displayTransform": {"rotation": 90, "flipX": False, "flipY": "yes"}}
    )

    assert flat == DisplayTransform(rotation=270, flip_x=True, flip_y=False)
    assert nested == DisplayTransform(rotation=90, flip_x=False, flip_y=True)


def test_display_transform_rejects_non_right_angle_rotation():
    transform = display_transform_from_properties({"displayRotation": 45})

    assert transform == DisplayTransform()
