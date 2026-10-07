# ports: out, sharpness
# Pick the sharpest plane of a focus stack.
# Sharpness is the variance of each plane's Laplacian: a plane in focus has
# more fine detail. 'out' is the sharpest plane; 'sharpness' is the score of
# every plane along the stack's axis, drawn in the Graph panel (its peak is the focus).
# Why a script: no processor chooses a plane by looking at the data; Projection
# collapses the axis with a statistic (max, mean, ...) instead of picking one plane.
from scipy import ndimage

if data.ndim != 3:
    raise ValueError(f"expects a (plane, Y, X) stack, got axes {axes}; split other axes off first")
scores = np.array([ndimage.laplace(plane.astype(np.float32)).var() for plane in data])
best = int(np.argmax(scores))
print("sharpest plane:", best)
outputs = {
    "out": make_result(data[best].copy(), axes=axes[1:], scales=scales[1:], name=f"{results[0].name} (plane {best})"),
    "sharpness": make_result(scores, axes=axes[:1], scales=scales[:1], name=f"{results[0].name} (sharpness)"),
}
