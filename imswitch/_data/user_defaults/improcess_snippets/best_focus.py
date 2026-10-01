# ports: out
# Pick the sharpest plane of a focus stack.
# Sharpness is the variance of each plane's Laplacian: a plane in focus has
# more fine detail. The plane numbers and their scores are printed.
# Why a script: no processor chooses a plane by looking at the data; Projection
# collapses the axis with a statistic (max, mean, ...) instead of picking one plane.
from scipy import ndimage

if data.ndim != 3:
    raise ValueError(f"expects a (plane, Y, X) stack, got axes {axes}; split other axes off first")
sharpness = np.array([ndimage.laplace(plane.astype(np.float32)).var() for plane in data])
best = int(np.argmax(sharpness))
print("sharpness per plane:", sharpness.round(1))
print("sharpest plane:", best)
out = make_result(data[best].copy(), axes=axes[1:], scales=scales[1:], name=f"{results[0].name} (plane {best})")
