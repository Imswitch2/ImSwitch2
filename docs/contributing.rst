*****************
How to contribute
*****************

We encourage users and developers to give active feedback on their experience
of using ImSwitch2 and also to contribute to the project.
Please follow the project's `code of conduct <https://github.com/Imswitch2/ImSwitch2/blob/main/CODE_OF_CONDUCT.md>`_ if you wish to contribute to the project.

Ways to contribute
==================


Reporting bugs
--------------
If you encounter a bug,
you can directly report it in the `issues section <https://github.com/Imswitch2/ImSwitch2/issues/>`_.
Please describe how to reproduce the bug
and include as much information as possible that can be helpful for fixing it.

Have you written code that fixes the bug?
You can open a new pull request or include your suggested fix in the issue.


User feedback
-------------
We would like to hear about your experience when using ImSwitch2 and suggestions for improvement.
You can do that by opening an `issue <https://github.com/Imswitch2/ImSwitch2/issues/>`_ on GitHub
labelled *question* or *enhancement*.


Suggest a new feature
---------------------
If you are missing features and want to develop ImSwitch2 further,
you can open an issue labelled *enhancement* and brainstorm your suggestions there.
Feel free to open a pull request,
but we believe it’s essential to have feedback from the community if you want to add new functionality.


Improving documentation
-----------------------
We would like to include your use-case into the documentation!
Feel free to open an issue labelled *documentation* about what you wish to include or improve.


Adding device support
---------------------
See :doc:`this page <adding-device-support>` for information on adding support for new devices.
You can open an issue if you want advice and help to get started.


Automated tests
---------------
We want to keep including automated tests into the development process,
so if you have contributed to the project by fixing a bug or providing new functionality,
we encourage you to write code that tests your contribution as well.

ImSwitch2's tests live in a ``_test/`` folder in each module's directory.  ImControl splits
them into ``_test/unit/`` and ``_test/ui/``: if you have added a new widget to the hardware
control module and want to write UI tests for it, place them in ``imswitch/imcontrol/_test/ui/``.

Every pull request runs the linter and the tests that need no display, in three lanes
(``unit``, ``improcess`` and ``core``; see ``.github/workflows/ci.yml``).  To run them the way
CI does, after ``pip install -e ".[test]"``:

.. code-block:: bash

   QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
   python -m pytest -p xdist.plugin -p pytestqt.plugin -p pytest_timeout -n auto --timeout=180 \
       imswitch/test_no_hardware_profile.py imswitch/imcontrol/_test/unit \
       imswitch/imcontrol/controller/_test imswitch/improcess/_test \
       imswitch/imcommon/_test imswitch/imscripting/_test \
       --ignore=imswitch/improcess/_test/test_snouty.py

A new test directory runs in CI only once it is added to one of the lanes.  The UI tests in
``imswitch/imcontrol/_test/ui`` need a display and run in the manually started
``imswitch-test`` workflow instead.
