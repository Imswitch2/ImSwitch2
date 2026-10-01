# ports: out
# Remove bleed-through between channels with a measured mixing matrix.
# mixing[i][j] is how much of true channel j shows up in measured channel i; here
# 15 % of channel 0 leaks into channel 1. The true channels come from inverting it.
# Why a script: no processor mixes channels; Image calculator combines two results
# with one operation, not a matrix across all the channels at once.
ax = 0                                         # the channel axis; print(axes) lists the names
mixing = np.array([[1.00, 0.00],
                   [0.15, 1.00]])

if data.shape[ax] != len(mixing):
    raise ValueError(f"the matrix is for {len(mixing)} channels, the data has {data.shape[ax]} along axis {ax}")
measured = np.moveaxis(data.astype(np.float32), ax, 0)
true = np.tensordot(np.linalg.inv(mixing), measured, axes=(1, 0)).astype(np.float32)
out = np.clip(np.moveaxis(true, 0, ax), 0, None)       # a negative count is noise, not signal
