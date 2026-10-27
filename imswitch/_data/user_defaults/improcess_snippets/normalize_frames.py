# ports: out
# Flatten frame-to-frame brightness changes (lamp flicker, bleaching).
# Each Y/X plane is divided by its own median and brought back to the stack's
# mean level.
# Why a script: Math applies one constant to the whole result; this needs a
# different factor for every plane.
data32 = data.astype(np.float32)               # uint16 arithmetic would wrap around
level = np.median(data32, axis=(-2, -1), keepdims=True)    # one number per plane
level = np.maximum(level, 1e-6)                # a black plane stays black, not NaN
out = data32 / level * level.mean()
print(f"plane levels ran from {level.min():.1f} to {level.max():.1f}; all are now {level.mean():.1f}")
