*********
Scripting
*********

Imswitch2 provides a scripting module that can be used to automate
tasks in the software.  This scripting module lets you write Python
code that interacts with Imswitch2 at runtime.

See the scripting API reference for the available API modules and
methods.  In addition to the module APIs, a set of global helper
functions is documented :doc:`here <api/_actions>`.

The API modules may provide signals – events that can be bound to via
e.g. the global ``getWaitForSignal`` scripting function.

There are example scripts under the scripting module to see how the
scripting functionality works in action.

Workflow Scripting Cookbooks
=============================

For headless, testable acquisition workflows that run independently of
the GUI, see:

* :doc:`scripting-wfs-workflows` — General pattern for scripting
  acquisition workflows, with WidefieldSTARSS as the worked example.
  Covers the facade pattern, parameter customization, device-name mapping,
  composite workflows, and mock testing.

* :doc:`scripting-time-resolved-workflows` — Time-resolved detector
  workflows for photon-counting products (TCSPC cubes, gated STED,
  tau-STED). Explains the generic time-resolved detector contract with
  Swabian TimeTagger as the worked example.
