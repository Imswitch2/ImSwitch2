import numpy as np
from scipy.stats import multivariate_normal

import time


class MockCameraTIS:
    def __init__(self):
        self.properties = {
            'image_height': 800,
            'image_width': 800,
            'subarray_vpos': 0,
            'subarray_hpos': 0,
            'exposure_time': 0.1,
            'exposure': 100,
            'gain': 1,
            'brightness': 1,
            'subarray_vsize': 800,
            'subarray_hsize': 800,
            'SensorHeight': 1024,
            'SensorWidth': 1280
        }
        self.exposure = 100
        self.gain = 1
        self.brightness = 1
        self.model = 'mock'
        self.SensorHeight = 500
        self.SensorWidth = 500
        self.shape = (self.SensorHeight,self.SensorWidth)

    def start_live(self):
        pass

    def stop_live(self):
        pass

    def suspend_live(self):
        pass

    def prepare_live(self):
        pass

    def setROI(self, hpos, vpos, hsize, vsize):
        self.shape = (vsize, hsize)
        self.properties['image_width'] = hsize
        self.properties['image_height'] = vsize

    def setBinning(self, binning):
        pass

    def grabFrame(self, **kwargs):
        mocktype = "random_peak"
        if mocktype=="focus_lock":
            img = np.zeros(self.shape)
            beamCenter = [int(np.random.randn() * 1 + 250), int(np.random.randn() * 30 + 300)]
            img[beamCenter[0] - 10:beamCenter[0] + 10, beamCenter[1] - 10:beamCenter[1] + 10] = 4000
        elif mocktype=="random_peak":
            imgsize = self.shape
            peakmax = 3000
            noisemean = 100
            # generate image
            img = np.zeros(imgsize)
            # add a random gaussian peak sometimes
            if np.random.rand() > 0.8:
                x, y = np.meshgrid(np.linspace(0,imgsize[1],imgsize[1]), np.linspace(0,imgsize[0],imgsize[0]))
                pos = np.dstack((x, y))
                xc = (np.random.rand()*2-1)*imgsize[0]/2 + imgsize[0]/2
                yc = (np.random.rand()*2-1)*imgsize[1]/2 + imgsize[1]/2
                rv = multivariate_normal([xc, yc], [[50, 0], [0, 50]])
                img = np.random.rand()*peakmax*317*rv.pdf(pos)
                img = img + 0.01*np.random.poisson(img)
            # add Poisson noise
            img = img + np.random.poisson(lam=noisemean, size=imgsize)
        elif mocktype=="random_beads":
            imgsize = self.shape
            x, y = np.meshgrid(np.linspace(0,imgsize[1],imgsize[1]), np.linspace(0,imgsize[0],imgsize[0]))
            pos = np.dstack((x, y))
            peakmax = 3000
            noisemean = 100
            beads = 13
            fixedrand1 = np.random.RandomState(1234537890)
            fixedrand2 = np.random.RandomState(2345678901)
            bead_positions = [(fixedrand1.rand(beads)*2-1)*imgsize[0]/2 + imgsize[0]/2,(fixedrand2.rand(beads)*2-1)*imgsize[1]/2 + imgsize[1]/2]
            # generate image
            img = np.zeros(imgsize)
            # add static beads with random max signal
            for i in range(beads):
                xc = bead_positions[0][i]
                yc = bead_positions[1][i]
                rv = multivariate_normal([xc, yc], [[50, 0], [0, 50]])
                img_curr = np.random.default_rng().uniform(0.7,1.0)*peakmax*317*rv.pdf(pos)
                img = img + img_curr
            # add Poisson noise
            img = img + np.random.poisson(lam=noisemean, size=imgsize)
        else:
            img = np.zeros(self.shape)
            beamCenter = [int(np.random.randn() * 30 + 250), int(np.random.randn() * 30 + 300)]
            img[beamCenter[0] - 10:beamCenter[0] + 10, beamCenter[1] - 10:beamCenter[1] + 10] = 1
            img = np.random.randn(img.shape[0],img.shape[1]) * 100 + 500
        # Real TIS cameras deliver integer frames and the recording path uses
        # the manager-declared uint16 dtype, so emit uint16 (values scaled to
        # a plausible 12-bit range) rather than raw float64.
        return np.clip(img, 0, 65535).astype(np.uint16)

    def getLast(self, is_resize=False):
        return self.grabFrame()
    
    def getLastChunk(self):
        return np.expand_dims(self.grabFrame(),0)
    
    def setPropertyValue(self, property_name, property_value):
        if property_name in {'exposure', 'gain', 'brightness'}:
            setattr(self, property_name, property_value)
        self.properties[property_name] = property_value
        return property_value

    def getPropertyValue(self, property_name):
        return self.properties.get(property_name, 0)

    def openPropertiesGUI(self):
        pass
    
    def close(self):
        pass
    
    def flushBuffer(self):
        pass 

# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
