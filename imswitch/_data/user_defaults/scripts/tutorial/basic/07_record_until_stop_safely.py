"""Tutorial basic 07 -- Record until you stop, and always stop cleanly.

You will learn
  * ``setRecModeUntilStop()`` with ``stopRecording()``
  * ``try``/``finally``: the clean-up that runs even when you press Stop
  * ``getWaitForSignal()``: create a waiter first, act, then wait
  * ``setSessionNote()``: a note that is saved into the recording

Setup
  Mock setup: example_mock.json
  Needs:      a camera and the Recording widget.

  Press Stop while it records: the recording still ends and is saved.

Next: 08_share_code_between_scripts.py
"""

signals = api.imcontrol.signals()
log = getLogger()

# The note is stored in the metadata of every file recorded from now on,
# the same text as in Tools > Session notes...
api.imcontrol.setSessionNote('Scripting tutorial 07: until-stop recording')
api.imcontrol.setRecModeUntilStop()

try:
    callAndWaitForSignal(signals.recordingStarted, api.imcontrol.startRecording,
                         timeout=30)
    log.info('Recording. Showing the live image for a while...')
    mainWindow.setCurrentModule('imcontrol')
    for second in range(5):
        # Anything can happen here: move a stage, change a laser, wait for a
        # sample. sleep() lets the Stop button interrupt it.
        sleep(1)
        log.info(f'{second + 1} s recorded')
finally:
    # Stop has been pressed, the loop failed, or it simply finished: in each
    # case the recording must end. The waiter is created *before*
    # stopRecording(), which returns whether a recording was running -- if
    # not, no recordingEnded will come, so do not wait for one.
    waitForEnd = getWaitForSignal(signals.recordingEnded, timeout=60)
    if api.imcontrol.stopRecording():
        waitForEnd()
        log.info('Recording stopped and saved.')
    else:
        waitForEnd.close()
    api.imcontrol.setSessionNote('')
    mainWindow.setCurrentModule('imscripting')
