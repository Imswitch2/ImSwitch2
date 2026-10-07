# ports: out
# Crop to the bright region, with a margin.
# The region is wherever any plane gets brighter than halfway between the darkest
# and the brightest pixel. A batch crops every file to its own region.
# Why a script: Stack subset needs the ranges typed in; this finds them in the data.
margin = 8                                     # pixels kept around the region

peak = data.max(axis=tuple(range(data.ndim - 2))) if data.ndim > 2 else data   # brightest value at each pixel
bright = peak > (peak.min() + peak.max()) / 2
if not bright.any():
    raise ValueError("nothing is brighter than the halfway level, so there is nothing to crop to")
rows, cols = np.nonzero(bright)
y0, y1 = max(rows.min() - margin, 0), min(rows.max() + margin + 1, peak.shape[0])
x0, x1 = max(cols.min() - margin, 0), min(cols.max() + margin + 1, peak.shape[1])
print(f"cropped to rows {y0}:{y1}, columns {x0}:{x1} of {peak.shape}")
out = data[..., y0:y1, x0:x1].copy()
