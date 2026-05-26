*********
Imswitch2
*********

.. image:: https://joss.theoj.org/papers/10.21105/joss.03394/status.svg
   :target: https://doi.org/10.21105/joss.03394

``Imswitch2`` is a Python application for flexible, modular microscope
control.  It is a clean-slate continuation of the original
`ImSwitch <https://github.com/ImSwitch/ImSwitch>`_ project, built
around a model-view-presenter (MVP) architecture and a
configuration-driven manager system: **switching hardware means
editing a JSON file, not the code.**

The constant development of novel microscopy methods with an increased
number of dedicated hardware devices poses significant challenges to
software development.  Imswitch2 is designed to be compatible with
many different microscope modalities and customizable to the specific
design of individual custom-built microscopes, all while using the
same software.  We would like to involve the community in further
developing Imswitch2 in this direction, believing that it is possible
to integrate current state-of-the-art solutions into one unified
piece of software.

In this documentation page you will find all information you need
about the installation, usage and development of Imswitch2, both from
the user perspective (GUI description and use cases) as well as for
developers (scripting and API modules, and hardware control and JSON
config files).

.. toctree::
    :hidden:
    :caption: Software info

    installation
    developer-onboarding
    agent-task-templates
    changelog
    contributing
    current-state-and-known-issues

.. toctree::
    :hidden:
    :caption: Usage

    gui
    use-cases
    scripting
    scripting-wfs-workflows

.. toctree::
    :hidden:
    :caption: Hardware reference

    imcontrol-setups
    setupinfo-reference
    devices/detectors
    devices/lasers
    devices/positioners
    devices/rotators
    Hdf5datafile

.. toctree::
    :hidden:
    :caption: Extending Imswitch2

    adding-device-support
    no-hardware-validation
    how-to/wire-teensy
    how-to/add-pulse-generator-backend
    how-to/port-from-third-party
    how-to/auto-screenshots

.. toctree::
    :glob:
    :hidden:
    :caption: Scripting API reference

    modules
    api/*
