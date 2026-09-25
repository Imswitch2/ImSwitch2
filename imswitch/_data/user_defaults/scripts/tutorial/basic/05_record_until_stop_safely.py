"""Tutorial basic 05 -- Record until you stop, and always stop cleanly.

You will learn
  * ``setRecModeUntilStop()`` with ``stopRecording()``
  * ``try``/``finally``: the clean-up that runs even when you press Stop
  * ``getWaitForSignal()``: create a waiter first, act, then wait
  * ``setSessionNote()``: a note that is saved into the recording

Setup
  Mock setup:   example_mock.json
  It simulates: one camera ("Camera", 800 x 800 pixels) that streams noise
                images.
  Your own microscope: needs a camera and the Recording widget.

  Try pressing Stop while it records: the recording still ends and is saved.

Next: 06_share_code_between_scripts.py
"""

signals = api.imcontrol.signals()
log = getLogger()

# A session note is stored in the metadata of every file recorded from now
# on -- the same text as in Tools > Session notes... in Hardware Control.
api.imcontrol.setSessionNote('Scripting tutorial 05: until-stop recording')
# "Until stop": the recording runs until stopRecording() is called.
api.imcontrol.setRecModeUntilStop()

try:
    # Wait for recordingStarted, so the loop below only begins once frames
    # are really being saved.
    callAndWaitForSignal(signals.recordingStarted, api.imcontrol.startRecording,
                         timeout=30)
    log.info('Recording. Showing the live image for a while...')
    mainWindow.setCurrentModule('imcontrol')
    for second in range(5):
        # In a real experiment, this is where you move a stage, change a
        # laser or wait for the sample. sleep() lets Stop interrupt it.
        sleep(1)
        log.info(f'{second + 1} s recorded')
finally:
    # Whether the loop finished, failed, or you pressed Stop: the recording
    # must end, or it would keep filling the disk.
    #
    # getWaitForSignal() creates a waiter for recordingEnded *before*
    # stopRecording() is called, for the same reason as in tutorial 04: the
    # signal can come before a later waiter exists. stopRecording() returns
    # True if a recording was running. If not, no recordingEnded will come,
    # so the script must not wait for one -- close() drops the waiter.
    waitForEnd = getWaitForSignal(signals.recordingEnded, timeout=60)
    if api.imcontrol.stopRecording():
        waitForEnd()                   # returns once the file is written
        log.info('Recording stopped and saved.')
    else:
        waitForEnd.close()
    api.imcontrol.setSessionNote('')   # an empty note clears it
    mainWindow.setCurrentModule('imscripting')
