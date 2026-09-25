*************
api.imcontrol
*************

.. class:: api.imcontrol

   These functions are available in the api.imcontrol object. 

   .. method:: getDetectorNames() -> List[str]

      Returns the device names of all detectors. These device names can
      be passed to other detector-related functions.

   .. method:: getDetectorParameter(detectorName: str, parameterName: str) -> Any

      Returns the value of the specified detector-specific parameter, in
      the parameter's own units -- the value the Settings widget shows.
      Parameter names and units differ from detector to detector;
      getDetectorParameters lists them. Raises AttributeError for a name
      the detector does not have.

   .. method:: getDetectorParameters(detectorName: str) -> Dict[str, Dict[str, Any]]

      Returns all detector-specific parameters of the specified detector
      as {parameter name: {'value', 'units', 'editable', 'options'}}. 'units'
      is None for a parameter that picks from a list of 'options', and
      'options' is None for a numerical one. Only an editable parameter can
      be changed with setDetectorParameter.

      The exposure time, for example, is ``'exposure'`` in ms on the
      simulated camera of the mock setups, ``'Exposure'`` in µs on a
      Thorlabs camera and ``'Set exposure time'`` in s on a Hamamatsu
      camera. To change a parameter and put it back afterwards::

         before = api.imcontrol.getDetectorParameter(camera, 'exposure')
         try:
             api.imcontrol.setDetectorParameter(camera, 'exposure', 10)
             ...
         finally:
             api.imcontrol.setDetectorParameter(camera, 'exposure', before)

   .. method:: getLaserNames() -> List[str]

      Returns the device names of all lasers. These device names can be
      passed to other laser-related functions. 

   .. method:: getPositionerNames() -> List[str]

      Returns the device names of all positioners. These device names can
      be passed to other positioner-related functions. 

   .. method:: getPositionerPositions() -> Dict[str, Dict[str, float]]

      Returns the positions of all positioners. 

   .. method:: loadScanParamsFromFile(filePath: str) -> None #comment from Simone: I tink originally from basecontroller, now also scancontrollerpointscan (I guess make sure only one is in the setup)

      Loads scanning parameters from the specified file.

   .. method:: changeScanCenterPos(positionerName: str, centerPos: float) -> None #new from Simone

      set the center of the scan 

   .. method:: changed3StepDelayPar(d3StepDelayPar: float) -> None #new from Simone

      set the d3Stepdelay parameter (additional parameter rom the pointscan widget, mostly relevant for the polarization during scan)

   .. method:: changeScanPower(laserName: str, laserValue: Union[int, float]) -> None

      Sets the value of the specified laser, in the units that the laser
      uses (alias of setLaserValue kept for existing scripts). Runs on the
      UI thread because it updates the laser widget. 

   .. method:: changeScanSize(positionerName: str, size: float) -> None

      change scan size of positioner

   .. method:: movePositioner(positionerName: str, axis: str, dist: float) -> None

      Moves the specified positioner axis by the specified number of
      micrometers. 

   .. method:: getRecFileFormat() -> str

      Returns the file format recordings are saved in: 'HDF5', 'TIFF' or
      'ZARR'. 

   .. method:: getRecFolder() -> str

      Returns the folder recordings and snaps are saved in. 

   .. method:: getScanRequestStatus(requestId: str) -> dict

      Status of a scan started with runScan, as ``{requestId, source,
      state, message, exact}`` with state ``pending``, ``succeeded`` or
      ``failed``. Raises KeyError for an unknown or evicted request id. 

   .. method:: getScanSourceNames() -> List[str]

      Widget keys of every scan controller that runScan(source=...) can
      target on this setup. 

   .. method:: isRecording() -> bool

      Whether a recording is currently active. 

   .. method:: runScan(source: Optional[str] = None) -> ScanRunHandle

      Starts one scan with the parameters set in the scan widget and
      returns a handle for its completion.
      
      The request is pre-flighted before any lifecycle signal is published;
      a refused start (a scan is already running, the previous one is still
      finishing, ...) raises ``ScanRequestRejectedError`` (a RuntimeError)
      and nothing else happens. On rigs with several scanners ``source``
      selects one by its widget key (see getScanSourceNames); without it the
      canonical Scan controller or a lone capable controller is used, and
      ambiguity raises. Repeat is switched off for the scan.
      
      The returned handle resolves for exactly this scan whatever the order
      of any waiter: ``handle.wait(timeout)`` (from a script), ``handle.done``
      / ``handle.successful`` / ``handle.message``, or
      ``getScanRequestStatus(handle.requestId)`` (remote clients receive the
      handle as ``{requestId, source, state, message}``). 

   .. method:: saveScanParamsToFile(filePath: str) -> None

      Saves the set scanning parameters to the specified file. 

   .. method:: setDetectorBinning(detectorName: str, binning: int) -> None

      Sets binning value for the specified detector. 

   .. method:: setDetectorParameter(detectorName: str, parameterName: str, value: Any) -> None

      Sets the specified detector-specific parameter to the specified
      value. 

   .. method:: setDetectorROI(detectorName: str, frameStart: Tuple[int, int], shape: Tuple[int, int]) -> None

      Sets the ROI for the specified detector. frameStart is a tuple
      (x0, y0) and shape is a tuple (width, height). 

   .. method:: setDetectorToRecord(detectorName: Union[List[str], str, int], multiDetectorSingleFile: bool = False) -> None

      Sets which detectors to record. One can also pass -1 as the
      argument to record the current detector, or -2 to record all detectors.
      

   .. method:: setLaserActive(laserName: str, active: bool) -> None

      Sets whether the specified laser is powered on. 

   .. method:: setLaserValue(laserName: str, value: Union[int, float]) -> None

      Sets the value of the specified laser, in the units that the laser
      uses. 

   .. method:: setLiveViewActive(active: bool) -> None

      Sets whether the LiveView is active and updating. 

   .. method:: setLiveViewCrosshairVisible(visible: bool) -> None

      Sets whether the LiveView crosshair is visible. 

   .. method:: setLiveViewGridVisible(visible: bool) -> None

      Sets whether the LiveView grid is visible. 

   .. method:: setPositioner(positionerName: str, axis: str, position: float) -> None

      Moves the specified positioner axis to the specified position. 

   .. method:: setPositionerStepSize(positionerName: str, stepSize: float) -> None

      Sets the step size of the specified positioner to the specified
      number of micrometers. 

   .. method:: setRecFileFormat(fileFormat: str) -> None

      Sets the file format recordings are saved in: 'HDF5', 'TIFF' or
      'ZARR' (any case) -- the Recording widget's "File format". Raises
      ValueError for any other name, and RuntimeError while snaps are set
      to go to the image display, which fixes the format to TIFF. 

   .. method:: setRecFilename(filename: Optional[str]) -> None

      Sets the name of the file to record to. This only sets the name of
      the file, not the full path. One can also pass None as the argument to
      use a default time-based filename. 

   .. method:: setRecFolder(folderPath: str) -> None

      Sets the folder to save recordings into. 

   .. method:: setRecModeScanOnce() -> None

      Sets the recording mode to record a single scan. 

   .. method:: setRecModeScanTimelapse(lapsesToRec: int, freqSeconds: float, timelapseSingleFile: bool = False) -> None

      Sets the recording mode to record a timelapse of scans. 

   .. method:: setRecModeSpecFrames(numFrames: int) -> None

      Sets the recording mode to record a specific number of frames. 

   .. method:: setRecModeSpecTime(secondsToRec: Union[int, float]) -> None

      Sets the recording mode to record for a specific amount of time.
      

   .. method:: setRecModeUntilStop() -> None

      Sets the recording mode to record until recording is manually
      stopped. 

   .. method:: signals() -> Mapping[str, imswitch.imcommon.framework.qt.Signal]

      Returns signals that can be used with e.g. the getWaitForSignal
      action. Currently available signals are:
      
      - acquisitionStarted
      - acquisitionStopped
      - recordingStarted
      - recordingEnded (the recording is finished and its files are
        written; for a scan-once or scan-timelapse recording, once after
        the last scan)
      - recordingFailed
      - scanStarting (the run-level start, before hardware arms)
      - scanStarted (the execution backend started the iteration)
      - scanDone (an iteration finished)
      - scanEnded (the run is over, on every terminal path)
      - scanRejected(reason) (a start request was refused; no scanEnded
        will follow for it)
      
      They can be accessed like this: api.imcontrol.signals().scanEnded
      

   .. method:: snapImage(output: bool = False) -> Optional[Dict[str, numpy.ndarray]]

      Take a snap. With output=True, return it as {detector name: image}
      without saving; otherwise save it in the snap format at the set file
      path. 

   .. method:: setSnapModeSave(mode: str = 'tiff') -> None

      Sets the file format snaps are saved in: 'HDF5', 'TIFF' or 'ZARR'
      (any case). Raises ValueError for any other name. 

   .. method:: startRecording() -> None

      Starts recording with the set settings to the set file path. 

   .. method:: stepPositionerDown(positionerName: str, axis: str) -> None

      Moves the specified positioner axis in negative direction by its
      set step size. 

   .. method:: stepPositionerUp(positionerName: str, axis: str) -> None

      Moves the specified positioner axis in positive direction by its
      set step size. 

   .. method:: stopRecording() -> bool

      Stops recording. Idempotent: returns True if a recording was
      active and its stop was requested (``recordingEnded`` or
      ``recordingFailed`` will follow), False if nothing was recording (no
      signal will follow, so do not wait for one). 
   
   .. method:: setMask(maskMode: str) -> None 
      
      Sets SLM Mask to Gaussian or Donut or etc. Available: Donut, TopHat, Half, Gauss, Hex, Quad, Split, Black

   .. method:: loadParams() -> None

      Loads saved SLM parameters from file
   
   .. method:: toggleSLMDisplay(bool) -> None

      Enable SLM display end thereby turn on

   .. method:: moveAbs(name: str, pos: str) -> None or float?

      Get rotator with name to move to posisition pos

   .. method:: changeRotationParameters(rotationPars: List[str])

      change rotation step, start angle and stop angle

   .. method:: loadCalibration(calibname: str) -> None

      load rotation calibration

   .. method:: activateRotScan(activate: bool) -> None

      activate rotation scan on d3 scan axis

   .. method:: getScanParameters() -> None

      From etSTEd controller.
      Load the scan parameters of the scan widget for etSTED
