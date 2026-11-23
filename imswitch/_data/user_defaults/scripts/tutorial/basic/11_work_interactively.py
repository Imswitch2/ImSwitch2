"""Tutorial basic 11 -- Work interactively: cells, the console and show().

You will learn
  * Run cell (Ctrl+Enter): run one ``# %%`` cell at a time. Its variables
    stay for the next cell, for Run selection and for the console
  * to change something and run only that cell again -- no new acquisition
  * ``show()``: put an array into ImControl's image view
  * the Variables panel, the console, Reset session -- and why Run all
    always starts fresh

Setup
  Mock setup:   example_no_hardware.json
  It simulates: two cameras ("Mock Camera" and "Mock ThorCam TSI") that
                return noise images, three stages and three rotation mounts.
  Your own microscope: needs a camera and the Recording and Image widgets.
                Change CAMERA to your camera's name.

How to run it: put the cursor in the first cell and press Shift+Enter
three times -- each runs the cell the cursor is in and moves to the next.
Then change THRESHOLD_SIGMA in cell 2 and press Ctrl+Enter there: cell 2
runs again on the frames cell 1 acquired. (Run all runs everything at once,
in a fresh namespace, as for any other script.)

Next: the scanning tutorials, starting with ../scanning/01_run_a_scan.py
"""

# %% 1. Acquire, once.
# The variables of this cell -- frames -- stay in the session: the
# Variables panel lists them with their shape and size, and in the console
# (on the right) you can type frames.shape or frames[3].mean().
CAMERA = 'Mock Camera'
frames = api.imcontrol.recordFramesToMemory(CAMERA, 10, timeout=60)
print(f'{frames.shape[0]} frames of {frames.shape[2]} x {frames.shape[1]} px')

# %% 2. Analyse. Change THRESHOLD_SIGMA and run only this cell again.
THRESHOLD_SIGMA = 2.0
mean = frames.mean(axis=0)
threshold = mean.mean() + THRESHOLD_SIGMA * mean.std()
bright = mean > threshold
print(f'{int(bright.sum())} pixels brighter than {threshold:.1f} '
      f'({THRESHOLD_SIGMA} standard deviations above the mean)')

# %% 3. Look at it.
# show() puts an array into ImControl's image view, as a layer of that name;
# showing under the same name again replaces the picture. A stack such as
# frames gets a slider. Double-clicking an array in the Variables panel
# does the same.
show(mean, 'mean')
show(bright, 'bright pixels')
show(frames, 'frames')

# The console works in the same variables, for looking: it is disabled
# while a cell runs, and acquiring belongs in the editor. Reset session
# (above the editor) forgets every variable when you want a clean slate.
