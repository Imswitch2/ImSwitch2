**************
The essentials
**************

ImSwitch2 in two minutes: what it is, the five ideas you need, and how to
get it running.

What it is
==========

ImSwitch2 is one program that runs many different microscopes, including
custom-built ones.  It controls cameras, lasers, stages and scanners,
records data, and processes it afterwards.  The same software runs a
widefield, a confocal or a super-resolution setup; what differs is a
description of the hardware.

Five ideas
==========

**The setup file describes your microscope.**
    A JSON file lists the devices (this camera, that laser, these stages)
    and the panels to show for them.  Switching hardware means editing or
    picking another setup file, not changing the program.  ImSwitch2 asks
    for one at the first start; change it later with
    **Hardware > Pick hardware setup…**.

**Modules are the tabs on the left.**
    *Hardware Control* runs the microscope, *Image Processing* reconstructs
    and analyses data, *Scripting* automates both.  Choose which load under
    **Preferences > Set active modules…**.

**The setup decides the panels.**
    Laser, stage, scan and recording controls appear only when the setup
    file lists them.  If a panel you expect is missing, look at the setup,
    not at the program.

**Mock setups need no hardware.**
    Some shipped setups simulate every device: the camera returns images,
    the stages report where they moved, nothing is connected.  Use them to
    learn, to test a script, or to prepare an experiment away from the
    microscope.  On a real setup, **Hardware > Hardware status…** shows
    which devices are connected, simulated or missing.

**Recordings go to one folder.**
    By default ``ImSwitchConfig/recordings`` in your home folder (Documents
    on Windows), one subfolder per day.  Change it under
    **Preferences > Recordings folder…**.  Your setups, scripts and
    settings live in ``ImSwitchConfig`` too.

Install and first run
=====================

Without Python, download the installer for Windows or macOS from the
`releases page <https://github.com/Imswitch2/Imswitch2/releases>`_.  It cannot
load vendor drivers or device plugins, so for a microscope install with
Python 3.10 or newer instead:

.. code-block:: bash

   pip install imswitch2
   imswitch

The first start takes a minute while ImSwitch2 creates its folder.  In the
setup picker, keep ``example_no_hardware.json`` to look around without a
microscope.  :doc:`first-image` then takes you from there to a live image
and a saved recording, click by click.

Where next
==========

* **Using a microscope:** :doc:`first-image`, then :doc:`imcontrol`.
* **Setting up a microscope:** :doc:`installation` for drivers, then
  :doc:`imcontrol-setups` and the device pages.
* **Processing data:** :doc:`improcess`.
* **Automating:** :doc:`scripting` and the :doc:`tutorials`.
* **Developing:** :doc:`developer-onboarding` and :doc:`architecture`.
