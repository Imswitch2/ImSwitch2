#!/usr/bin/env python
# -*- coding: utf-8 -*-

import atexit
import threading

from .IC_GrabberDLL import IC_GrabberDLL
from .IC_Camera import IC_Camera
from .IC_Exception import IC_Exception


class IC_ImagingControl:
    """Small owner for TIS grabber handles over one process-global SDK.

    ``IC_InitLibrary`` is documented by the bundled TIS C API as a one-time
    process initialization and ``IC_CloseLibrary`` as an application-shutdown
    operation. Camera reconnect must therefore recycle grabber/device objects
    without closing and reinitializing the global library.
    """

    _library_initialized = False
    _atexit_registered = False
    _library_lock = threading.RLock()

    def init_library(self):
        """Initialize this wrapper and ensure the process-global SDK is ready."""
        self._unique_device_names = None
        self._devices = {}

        cls = type(self)
        with cls._library_lock:
            if not cls._library_initialized:
                err = IC_GrabberDLL.init_library(None)
                if err != 1:
                    raise IC_Exception(err)
                cls._library_initialized = True

                if not cls._atexit_registered:
                    atexit.register(cls.shutdown_library)
                    cls._atexit_registered = True

    def get_unique_device_names(self):
        """Gets unique names (i.e. model + label + serial) of devices."""
        if self._unique_device_names is None:
            self._unique_device_names = []

            # Must be called before get_unique_name_from_list(). Re-running
            # enumeration on a fresh wrapper is intentional: a camera may have
            # been plugged in since the previous reconnect attempt.
            num_devices = IC_GrabberDLL.get_device_count()
            if num_devices < 0:
                raise IC_Exception(num_devices)

            for i in range(num_devices):
                self._unique_device_names.append(
                    IC_GrabberDLL.get_unique_name_from_list(i).decode('ascii')
                )

        return self._unique_device_names

    def get_device(self, unique_device_name):
        """Return/create the grabber object for one enumerated camera."""
        if unique_device_name in self.get_unique_device_names():
            if unique_device_name not in self._devices:
                self._devices[unique_device_name] = IC_Camera(unique_device_name)
            return self._devices[unique_device_name]

        raise IC_Exception(-106)

    def release_resources(self):
        """Release grabbers owned by this wrapper; keep the SDK initialized.

        ``IC_CloseLibrary`` is process-global and belongs to application
        shutdown, not camera reconnect.
        """
        devices = self._devices or {}
        for camera in tuple(devices.values()):
            try:
                if camera.is_open():
                    camera.close()
            finally:
                IC_GrabberDLL.release_grabber(camera._handle)

        self._unique_device_names = None
        self._devices = None

    @classmethod
    def shutdown_library(cls):
        """Close the process-global TIS SDK once, at application shutdown."""
        with cls._library_lock:
            if not cls._library_initialized:
                return
            IC_GrabberDLL.close_library()
            cls._library_initialized = False


# MIT License
#
# Copyright (c) 2017 morefigs
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
