"""Child process of the exit test: boot, live view, record 10 frames, close, fall off the end.

    python e2e_child.py SCRATCH_DIR

``HOME`` is set by the parent before this starts. The interpreter then finalizes
with the QApplication and whatever the app left behind still alive, which is
what must not crash (plan section 5, P0). The last output line is ``E2E-CHILD-OK <frames>``.
"""
import faulthandler
import sys
from pathlib import Path

import e2e_helpers as helpers

faulthandler.enable()

scratch = Path(sys.argv[1])
booted = helpers.boot(scratch, move_user_files=False)
booted.api.setLiveViewActive(True)
camera = booted.master.detectorsManager["Camera"]
camera.setParameter("Exposure", 5)
booted.wait_until(lambda: camera.getLatestFrame().any(), "live view frames")
path = helpers.record_frames(booted, scratch / "rec", "child", 10)
data, committed, complete = helpers.read_recording(path)
booted.close()
print(f"E2E-CHILD-OK {data.shape[0]} committed={committed} complete={complete}", flush=True)
# No app.quit(), no explicit teardown: the interpreter exits with the app objects alive.
