"""Boot ImControl headlessly on the plugin's demo setup template, and drive it.

The implementation lives in the package, ``imswitch_device_micromanager.headless``
(it also drives ``tools/compare_cameras.py`` on a rig); this module re-exports it
under the names the tests and ``e2e_child.py`` have always used. Read that module's
docstring for what is real (everything but the napari canvas) and for the user-files
rules (``HOME`` is isolated by ``conftest.py``; ``boot(scratch, move_user_files=True)``
moves the user-files folder when ``imswitch`` was already imported).

Shared by ``test_e2e_app.py`` (in this pytest process) and ``e2e_child.py`` (a
child interpreter, for the exit test).
"""
from imswitch_device_micromanager.headless import (
    DEADLINE_S,
    SETUP_FILE_NAME,
    TEMPLATE,
    BootedApp,
    boot,
    drain_threads,
    install_gui_stubs,
    load_template_text,
    read_recording,
    record_frames,
    template_json,
)

__all__ = [
    "DEADLINE_S", "SETUP_FILE_NAME", "TEMPLATE", "BootedApp", "boot", "drain_threads", "install_gui_stubs",
    "load_template_text", "read_recording", "record_frames", "template_json",
]
