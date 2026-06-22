"""Filesystem defaults for the legacy EtSnouty controller/widget."""

import os

from imswitch.imcommon.model import dirtools


ETSNOUTY_ROOT_ENV = "IMSWITCH_ETSNOUTY_ROOT"


def getEtSnoutyRoot():
    """Return the configurable EtSnouty working directory."""
    configuredRoot = os.environ.get(ETSNOUTY_ROOT_ENV)
    if configuredRoot:
        return os.path.expanduser(configuredRoot)
    return os.path.join(dirtools.UserFileDirs.Root, "imcontrol_etsnouty")


def getEtSnoutyPath(*parts):
    """Return a path below the EtSnouty working directory."""
    return os.path.join(getEtSnoutyRoot(), *parts)
