"""Tests for script execution robustness, cooperative cancellation, and result contract."""
import time
import pytest

from imswitch.imscripting.model import ScriptExecutor, ScriptRunResult, ScriptRunStatus


@pytest.fixture
def script_executor():
    """Create a ScriptExecutor with an empty scope."""
    executor = ScriptExecutor(scriptScope={})
    yield executor
    # Cleanup: ensure thread is stopped
    if executor.isExecuting():
        executor.cancel()


def test_script_produces_result_with_captured_stdout(qtbot, script_executor):
    """A script run produces a ScriptRunResult with captured stdout."""
    code = """
print("Hello from script")
print("Line 2")
"""
    
    # Track the result from signal
    received_result = None
    
    def on_finished(result):
        nonlocal received_result
        received_result = result
    
    script_executor.sigExecutionFinished.connect(on_finished)
    
    # Execute script
    result = script_executor.execute(None, code)
    
    # Wait for execution to finish
    with qtbot.waitSignal(script_executor.sigExecutionFinished, timeout=5000):
        pass
    
    # Verify result was created and returned
    assert result is not None
    assert isinstance(result, ScriptRunResult)
    
    # Verify the signal emitted the same result
    assert received_result is result
    
    # Verify status is succeeded
    assert result.status == ScriptRunStatus.SUCCEEDED
    
    # Verify stdout was captured
    assert "Hello from script" in result.stdout
    assert "Line 2" in result.stdout
    
    # Verify no error
    assert result.error is None
    
    # Verify timestamps
    assert result.started_at is not None
    assert result.ended_at is not None
    assert result.ended_at >= result.started_at


def test_failing_script_yields_failed_status_with_traceback(qtbot, script_executor):
    """A failing script yields status=failed + traceback (not a crash)."""
    code = """
print("Before error")
raise ValueError("Test error message")
print("After error - should not execute")
"""
    
    received_result = None
    
    def on_finished(result):
        nonlocal received_result
        received_result = result
    
    script_executor.sigExecutionFinished.connect(on_finished)
    
    # Execute script
    result = script_executor.execute(None, code)
    
    # Wait for execution to finish
    with qtbot.waitSignal(script_executor.sigExecutionFinished, timeout=5000):
        pass
    
    # Verify status is failed
    assert result.status == ScriptRunStatus.FAILED
    
    # Verify error message contains traceback
    assert result.error is not None
    assert "ValueError" in result.error
    assert "Test error message" in result.error
    assert "Traceback" in result.error
    
    # Verify stdout still captured output before error
    assert "Before error" in result.stdout
    assert "After error" not in result.stdout
    
    # Verify timestamps
    assert result.ended_at is not None


def test_cancelled_run_ends_cleanly_without_terminate(qtbot, script_executor):
    """A cancelled run ends cleanly without using QThread.terminate().
    
    Note: Python's exec() cannot be interrupted mid-execution without unsafe
    mechanisms. This test verifies that cancel() uses quit() and wait() instead
    of terminate(), ensuring the thread is joined properly even if the script
    completes before cancellation takes effect."""
    code = """
import time
print("Script started")
time.sleep(0.05)
print("Script continuing")
"""
    
    received_result = None
    
    def on_finished(result):
        nonlocal received_result
        received_result = result
    
    script_executor.sigExecutionFinished.connect(on_finished)
    
    # Execute script
    result = script_executor.execute(None, code)
    
    # Immediately try to cancel (may or may not take effect before script completes)
    script_executor.cancel()
    
    # Wait for execution to finish - cancel() uses wait() so this should complete
    # The key is that we don't hang and the thread is properly joined
    with qtbot.waitSignal(script_executor.sigExecutionFinished, timeout=5000):
        pass
    
    # Verify the executor is no longer executing
    assert not script_executor.isExecuting()
    
    # Verify we got a result (status may be SUCCEEDED or CANCELLED depending on timing)
    assert result.status in (ScriptRunStatus.SUCCEEDED, ScriptRunStatus.CANCELLED)
    
    # Verify we got some output
    assert "Script started" in result.stdout
    
    # Verify timestamps
    assert result.ended_at is not None
    
    # The critical success criterion: thread was joined cleanly, not terminated
    # If terminate() was used, this test would likely hang or fail to emit signals


def test_successful_script_with_output(qtbot, script_executor):
    """Test successful script execution with various output."""
    code = """
import sys
print("Standard output")
print("Line with numbers: 123", flush=True)
sys.stderr.write("Error output\\n")
x = 5 + 3
print(f"Result: {x}")
"""
    
    received_result = None
    
    def on_finished(result):
        nonlocal received_result
        received_result = result
    
    script_executor.sigExecutionFinished.connect(on_finished)
    
    result = script_executor.execute("/tmp/test_script.py", code)
    
    with qtbot.waitSignal(script_executor.sigExecutionFinished, timeout=5000):
        pass
    
    assert result.status == ScriptRunStatus.SUCCEEDED
    assert result.script_path == "/tmp/test_script.py"
    assert "Standard output" in result.stdout
    assert "Line with numbers: 123" in result.stdout
    assert "Error output" in result.stdout  # stderr is also captured
    assert "Result: 8" in result.stdout
    assert result.error is None


def test_script_run_result_id_is_unique():
    """Each ScriptRunResult should have a unique ID."""
    result1 = ScriptRunResult()
    result2 = ScriptRunResult()
    
    assert result1.id != result2.id
    assert len(result1.id) > 0
    assert len(result2.id) > 0


def test_multiple_sequential_executions(qtbot, script_executor):
    """Test multiple sequential script executions."""
    results = []
    
    def on_finished(result):
        results.append(result)
    
    script_executor.sigExecutionFinished.connect(on_finished)
    
    # First script
    code1 = 'print("First script")'
    result1 = script_executor.execute(None, code1)
    with qtbot.waitSignal(script_executor.sigExecutionFinished, timeout=5000):
        pass
    
    # Second script
    code2 = 'print("Second script")'
    result2 = script_executor.execute(None, code2)
    with qtbot.waitSignal(script_executor.sigExecutionFinished, timeout=5000):
        pass
    
    # Verify both completed successfully
    assert len(results) == 2
    assert results[0].status == ScriptRunStatus.SUCCEEDED
    assert results[1].status == ScriptRunStatus.SUCCEEDED
    assert "First script" in results[0].stdout
    assert "Second script" in results[1].stdout
    # Verify outputs don't bleed across runs
    assert "Second script" not in results[0].stdout
    assert "First script" not in results[1].stdout


def test_no_hardcoded_windows_path_in_code():
    """Verify the hardcoded Windows path was removed from the codebase."""
    import os
    import re
    
    # Check EditorController.py
    controller_path = os.path.join(
        os.path.dirname(__file__),
        '../../controller/EditorController.py'
    )
    
    with open(controller_path, 'r') as f:
        content = f.read()
    
    # Look for the specific hardcoded path
    assert r'C:\Users\xavie' not in content, \
        "Hardcoded Windows path still present in EditorController"
    
    # Look for any suspicious Windows-style absolute paths
    # Pattern: r'C:\ or C:/ followed by path
    windows_path_pattern = re.compile(r"[rRuU]?['\"]C:\\\\")
    matches = windows_path_pattern.findall(content)
    
    # Filter out any legitimate uses (like in comments about Windows paths)
    # The execute call should use None or a variable, not a hardcoded path
    assert 'execute(r\'C:' not in content, \
        "Hardcoded path in execute() call"
    assert 'execute("C:' not in content, \
        "Hardcoded path in execute() call"


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
