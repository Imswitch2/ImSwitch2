Scripts shipped with ImSwitch's Scripting module. They are copied into
~/ImSwitchConfig/scripts (Documents\ImSwitchConfig\scripts on Windows) on
first start and run from the Scripting tab -- not as standalone Python.

  tutorial/basic      Step-by-step introduction: snapping, camera settings,
                      recording, safe clean-up, sharing code, several
                      cameras, stages. Runs on the mock setups.
  tutorial/scanning   Scans that trigger a camera, point detectors, scan
                      timelapses, laser power series. Runs on the scan mocks.
  workflows           Complete acquisitions for real rigs (WidefieldSTARSS,
                      time-resolved detection). Need the named hardware.

Every tutorial names its mock setup in its header ("Mock setup: ...", with
"It simulates:" and "Your own microscope:" below it), and
imswitch/imscripting/_test/test_shipped_tutorials.py runs each one against
that setup, so a tutorial that stops working fails the test suite.

At every start a user's untouched copy of an older version is updated,
and an untouched copy of a script removed here is moved to the trash;
edited copies are kept (imcommon/model/dirtools.py, syncUserDefaults).
After changing, adding, moving or removing any file under user_defaults,
run  python tools/update_user_defaults_history.py  and commit the result --
imcommon/_test/test_user_defaults_sync.py fails until you do.
