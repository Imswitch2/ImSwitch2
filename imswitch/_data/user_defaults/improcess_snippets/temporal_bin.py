# ports: out
# Average every n frames into one (temporal binning).
# The frame axis gets n times the spacing, so a time axis stays calibrated.
# Frames left over after the last full group are dropped.
# Why a script: Resize works on Y/X, and Projection collapses an axis to one image;
# neither averages groups of neighbouring frames.
n = 4                                          # frames averaged into one
ax = 0                                         # the frame axis; print(axes) lists the names

moved = np.moveaxis(data.astype(np.float32), ax, 0)
usable = moved.shape[0] // n * n
binned = moved[:usable].reshape(usable // n, n, *moved.shape[1:]).mean(axis=1)
new_scales = list(scales)
new_scales[ax] = scales[ax] * n
out = make_result(np.moveaxis(binned, 0, ax), axes=axes, scales=new_scales)
print(f"{data.shape[ax]} frames -> {usable // n} (dropped {data.shape[ax] - usable})")
