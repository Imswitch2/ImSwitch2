# ports: ratio, valid
# Ratio of two channels, left empty where the denominator is too dim to trust.
# Channel 0 divided by channel 1 along the channel axis. Where channel 1 is below
# `floor` times its own maximum the ratio is NaN (shown empty), and `valid` marks
# the pixels that were used.
# Why a script: Image calculator divides two whole results and writes 0 where it
# cannot; here the unusable pixels must stay out of the ratio, not read as 0.
ax = 0                                         # the channel axis; print(axes) lists the names
floor = 0.1                                    # fraction of channel 1's maximum below which a pixel is left out

top = np.take(data, 0, axis=ax).astype(np.float32)
bottom = np.take(data, 1, axis=ax).astype(np.float32)
valid = bottom > floor * bottom.max()
ratio = np.full(top.shape, np.nan, np.float32)
np.divide(top, bottom, out=ratio, where=valid)
print(f"{valid.mean():.1%} of pixels used; median ratio {np.nanmedian(ratio):.3f}")
kept = [label for i, label in enumerate(axes) if i != ax]
outputs = {
    "ratio": make_result(ratio, axes=kept),
    "valid": make_labels(valid.astype(np.uint8), axes=kept),
}
