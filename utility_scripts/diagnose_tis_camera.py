"""
TIS camera diagnostic script.

Run this standalone (outside of ImSwitch2) to isolate whether the issue is
system-level (driver/USB/process lock) or ImSwitch code-level.

Usage:
    python utility_scripts/diagnose_tis_camera.py
"""

import sys
import ctypes
import ctypes.util

print("=== TIS Camera Diagnostic ===\n")

# Step 1: Find the DLL
print("[1] Locating tisgrabber DLL...")
if sys.maxsize > 2**32:
    dll_name = 'tisgrabber_x64.dll'
else:
    dll_name = 'tisgrabber.dll'

dll_path = ctypes.util.find_library(dll_name)
print(f"    find_library('{dll_name}') => {dll_path!r}")
if dll_path is None:
    print("    ERROR: DLL not found on PATH. Make sure TIS IC Imaging Control is installed.")
    sys.exit(1)

# Step 2: Load the DLL
print("\n[2] Loading DLL...")
try:
    dll = ctypes.windll.LoadLibrary(dll_path)
    print(f"    OK — loaded {dll_path}")
except Exception as e:
    print(f"    ERROR loading DLL: {e}")
    sys.exit(1)

# Step 3: Init library
print("\n[3] IC_InitLibrary(NULL)...")
dll.IC_InitLibrary.restype = ctypes.c_int
dll.IC_InitLibrary.argtypes = (ctypes.c_char_p,)
result = dll.IC_InitLibrary(None)
print(f"    Return code: {result}  (1 = IC_SUCCESS, anything else = error)")
if result != 1:
    print("    ERROR: Library initialisation failed. Try reinstalling IC Imaging Control.")
    sys.exit(1)

# Step 4: Count devices
print("\n[4] IC_GetDeviceCount()...")
dll.IC_GetDeviceCount.restype = ctypes.c_int
count = dll.IC_GetDeviceCount()
print(f"    Devices found: {count}")
if count < 0:
    print(f"    ERROR: Negative count ({count}) means an internal DLL error.")
    sys.exit(1)
if count == 0:
    print("    No cameras detected by the TIS DLL.")
    print("    -> Check USB cable and port.")
    print("    -> Check Windows Device Manager: camera should appear under 'Imaging devices'")
    print("       with TIS driver (not 'Unknown device' or generic UVC).")
    print("    -> Close IC Capture or any other application that may have the camera locked.")
    sys.exit(1)

# Step 5: List device names
print(f"\n[5] Listing {count} device(s)...")
dll.IC_GetUniqueNamefromList.restype = ctypes.c_char_p
dll.IC_GetUniqueNamefromList.argtypes = (ctypes.c_int,)
for i in range(count):
    name = dll.IC_GetUniqueNamefromList(i)
    print(f"    [{i}] {name.decode('ascii') if name else '<NULL>'}")

print("\n=== All OK — camera(s) visible to TIS DLL ===")
print("If ImSwitch still fails, the issue is in ImSwitch's initialisation path,")
print("not in the driver/USB layer.")
