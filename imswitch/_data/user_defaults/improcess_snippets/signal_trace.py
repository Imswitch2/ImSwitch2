# ports: level, area
# Follow the bright signal through a recording.
# A frame's background is its median; signal is whatever lies more than k robust
# standard deviations above it. 'level' is the mean signal above background
# relative to the first frame (1.0 = unchanged, 0.5 = bleached to half); 'area' is
# how many pixels count as signal. Both are curves, drawn in the Graph panel.
# Why a script: Multi Measure reads one fixed region on every frame; this follows the
# signal wherever it moves or grows, which no region drawn once can do.
k = 5.0

if data.ndim != 3:
    raise ValueError(f"expects a (frame, Y, X) recording, got axes {axes}; split other axes off first")
frames = data.astype(np.float32).reshape(len(data), -1)
background = np.median(frames, axis=1)
sigma = 1.4826 * np.median(np.abs(frames - background[:, None]), axis=1)
is_signal = frames > (background + k * sigma)[:, None]
area = is_signal.sum(axis=1)
above = np.array([frame[mask].mean() - bg if mask.any() else np.nan
                  for frame, mask, bg in zip(frames, is_signal, background)])
if not np.isfinite(above[0]) or above[0] <= 0:
    raise ValueError("the first frame has no signal above its background to compare the others with")
level = above / above[0]
print(f"signal area: {area[0]} px in the first frame, {area[-1]} px in the last")
print(f"signal level in the last frame: {level[-1]:.2f} of the first")
outputs = {
    "level": make_result(level, axes=axes[:1], scales=scales[:1]),
    "area": make_result(area, axes=axes[:1], scales=scales[:1]),
}
