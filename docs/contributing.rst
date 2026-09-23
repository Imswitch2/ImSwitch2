*****************
How to contribute
*****************

We encourage users and developers to give active feedback on their experience
of using ImSwitch2 and also to contribute to the project.
Please follow the `Python Community guidelines <https://www.python.org/psf/conduct/>`_ if you wish to contribute to the project.

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
You can do that by starting a thread in the `discussion section <https://github.com/Imswitch2/ImSwitch2/discussions/>`_ on GitHub.


Suggest a new feature
---------------------
If you are missing features and want to develop ImSwitch2 further,
you can start a discussion and brainstorm your suggestions.
Feel free to open a pull request,
but we believe it’s essential to have feedback from the community if you want to add new functionality.


Improving documentation
-----------------------
We would like to include your use-case into the documentation!
Feel free to open a discussion thread about what you wish to include or improve.


Adding device support
---------------------
See :doc:`this page <adding-device-support>` for information on adding support for new devices.
You can start a discussion if you want to get advice and help to get started.


Automated tests
---------------
We want to keep including automated tests into the development process,
so if you have contributed to the project by fixing a bug or providing new functionality,
we encourage you to write code that tests your contribution as well.

ImSwitch2's pytests are located inside the ``_test/unit/`` and ``_test/ui/`` folders under each ImSwitch2 module's directory.
So if you have added a new widget to the hardware control module and want to write UI tests for it,
you should place the UI test files in the ``/imswitch/imcontrol/_test/ui/`` folder.

The test suite will automatically be run on commits and pull requests on GitHub.
You can also run it manually by executing the command ``python -m pytest --pyargs imswitch`` in the root directory of the repository.
