Scripts shipped with ImSwitch's Scripting module. They are copied into
~/ImSwitchConfig/scripts (Documents\ImSwitchConfig\scripts on Windows) on
first start and run from the Scripting tab -- not as standalone Python.

  tutorial/basic      Step-by-step introduction: cameras, snapping, camera
                      settings, recording, several cameras, stages, safe
                      clean-up, sharing code. Runs on the mock setups.
  tutorial/scanning   Scans that trigger a camera, point detectors, scan
                      timelapses, laser power series. Runs on the scan mocks.
  workflows           Complete acquisitions for real rigs (WidefieldSTARSS,
                      time-resolved detection). Need the named hardware.

Every tutorial names its mock setup in its header ("Mock setup: ...") and
imswitch/imscripting/_test/test_shipped_tutorials.py runs each one against
that setup, so a tutorial that stops working fails the test suite.

Only missing files are copied: an existing user folder keeps its own
copies, and files removed here stay there.
