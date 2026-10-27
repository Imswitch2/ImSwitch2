# ports: out
# Assemble a tile scan into one image.
# The tiles arrive as a stack, row after row. In a serpentine ("snake") scan every
# other row was acquired right to left, so its tiles come in reversed order.
# Tiles are butted together: there is no overlap blending or registration.
# Why a script: Stack combine joins stacks along an axis; laying tiles out in a
# grid is not what it does.
rows, cols = 3, 4                              # the scan grid: rows * cols tiles
snake = True                                   # False for a raster scan (every row left to right)

if data.ndim != 3 or data.shape[0] != rows * cols:
    raise ValueError(f"expected {rows * cols} tiles as a (plane, Y, X) stack, got shape {data.shape}")
tiles = data.reshape(rows, cols, *data.shape[1:]).copy()
if snake:
    tiles[1::2] = tiles[1::2, ::-1].copy()     # put the right-to-left rows in left-to-right order
mosaic = np.concatenate([np.concatenate(list(row), axis=-1) for row in tiles], axis=-2)
out = make_result(mosaic, axes=axes[1:], scales=scales[1:])
print(f"{rows * cols} tiles of {data.shape[1:]} -> one {mosaic.shape} image")
