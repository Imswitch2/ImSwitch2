# ports: out
# Replace hot pixels without blurring anything else.
# A pixel is replaced by the median of its 3 x 3 neighbourhood only if it is
# brighter than that median by more than k noise widths; every other pixel is
# left exactly as it was.
# Why a script: Filter's median blurs every pixel; this touches only the outliers.
from scipy import ndimage

k = 8.0                                        # noise widths above the local median that count as hot
#                                                (raise it for very dim images, where shot noise is skewed)
planes = data.astype(np.float32)
local = ndimage.median_filter(planes, size=(1,) * (data.ndim - 2) + (3, 3))
residual = planes - local
noise = 1.4826 * np.median(np.abs(residual), axis=(-2, -1), keepdims=True)   # robust width, per plane
noise = np.maximum(noise, 1e-3 * np.ptp(planes) + 1e-6)                      # a flat plane has no width
hot = residual > k * noise
out = np.where(hot, local, planes)
print(f"replaced {hot.sum()} of {hot.size} pixels ({hot.mean():.4%})")
