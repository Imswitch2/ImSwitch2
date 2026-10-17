@echo off
REM Build the Windows ImSwitch2 bundle and wrap it in a Setup .exe.
REM
REM   release\windows\create_installer_windows.bat
REM
REM Requirements:
REM   * 64-bit Python 3.10+ on PATH.
REM   * Inno Setup 6, installed to its default location.
REM     https://jrsoftware.org/isdl.php
REM
REM The resulting installer is unsigned, so SmartScreen will warn on download.
REM See docs\packaging.rst.
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set REPO_ROOT=%SCRIPT_DIR%..\..
set BUILD_ROOT=%REPO_ROOT%\build\windows
set VENV_DIR=%BUILD_ROOT%\venv
set DIST_DIR=%BUILD_ROOT%\dist
set WORK_DIR=%BUILD_ROOT%\work
set APP_NAME=ImSwitch2
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

REM ---------------------------------------------------------------------------
REM Step 0: check everything up front.
REM ---------------------------------------------------------------------------
REM A missing prerequisite that only surfaces after a ten-minute PyInstaller
REM run is a bad way to learn about it.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10+ required on PATH.
    exit /b 1
)
if not exist %ISCC% (
    echo ERROR: Inno Setup 6 not found at %ISCC%
    echo        Install it from https://jrsoftware.org/isdl.php
    exit /b 1
)

for /f %%i in ('python -c "import pathlib; ns={}; exec(pathlib.Path(r'%REPO_ROOT%\imswitch\__init__.py').read_text(), ns); print(ns['__version__'])"') do set IMSWITCH_VERSION=%%i
if "%IMSWITCH_VERSION%"=="" (
    echo ERROR: could not read the version from imswitch\__init__.py
    exit /b 1
)
echo ^>^>^> Building %APP_NAME% %IMSWITCH_VERSION%

REM ---------------------------------------------------------------------------
REM Step 1: a throwaway build environment.
REM ---------------------------------------------------------------------------
REM Deliberately NOT the development environment: a dev env carries pytest,
REM sphinx, debugpy and whatever a past experiment left behind, and PyInstaller
REM will pull a good deal of it into the bundle.  Installing the built wheel
REM rather than "pip install -e ." also avoids the editable-install import hook,
REM which PyInstaller's module graph handles poorly, and it is the only way the
REM bundle ends up with correct `imswitch2` distribution metadata.
echo ^>^>^> Creating clean build environment
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
mkdir "%BUILD_ROOT%"
python -m venv "%VENV_DIR%" || exit /b 1
call "%VENV_DIR%\Scripts\activate.bat"
python -m pip install --upgrade pip build "pyinstaller>=6.11" || exit /b 1

echo ^>^>^> Building wheel
REM setuptools keeps intermediates in <repo>\build\lib and reuses them, so a
REM file deleted since the last in-tree build comes back from the dead.  Plain
REM "python -m build" (no --wheel) sidesteps that: it makes an sdist first and
REM builds the wheel from *that*, unpacked in a temp dir.  Same as the PyPI
REM workflow, which is why what ships to PyPI is clean.
if exist "%REPO_ROOT%\build\lib" rmdir /s /q "%REPO_ROOT%\build\lib"
python -m build --outdir "%BUILD_ROOT%\wheel" "%REPO_ROOT%" || exit /b 1
for %%w in ("%BUILD_ROOT%\wheel\*.whl") do python -m pip install "%%w" || exit /b 1

REM ---------------------------------------------------------------------------
REM Step 2: freeze.
REM ---------------------------------------------------------------------------
echo ^>^>^> Running PyInstaller
pyinstaller "%REPO_ROOT%\imswitch.spec" ^
    --noconfirm --clean ^
    --distpath "%DIST_DIR%" ^
    --workpath "%WORK_DIR%" || exit /b 1

if not exist "%DIST_DIR%\%APP_NAME%\%APP_NAME%.exe" (
    echo ERROR: PyInstaller did not produce %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Step 3: self-test.
REM ---------------------------------------------------------------------------
REM Catches the quiet failures -- above all napari plugin metadata that did not
REM make it into the bundle.  The viewer check needs a GL context; it reports
REM itself skipped when QT_QPA_PLATFORM says there is no display.
echo ^>^>^> Self-testing the bundle
"%DIST_DIR%\%APP_NAME%\%APP_NAME%.exe" --bundle-selftest || exit /b 1

call deactivate

REM ---------------------------------------------------------------------------
REM Step 4: installer.
REM ---------------------------------------------------------------------------
echo ^>^>^> Building installer
%ISCC% /DAPP_VERSION=%IMSWITCH_VERSION% /DSOURCE_DIR="%DIST_DIR%\%APP_NAME%" /DOUTPUT_DIR="%BUILD_ROOT%" "%SCRIPT_DIR%imswitch_innoinstaller.iss" || exit /b 1

echo.
echo =============================================
echo  Build complete
echo  Bundle:    %DIST_DIR%\%APP_NAME%
echo  Installer: %BUILD_ROOT%\%APP_NAME%-%IMSWITCH_VERSION%-win64-setup.exe
echo.
echo  The installer is unsigned.  SmartScreen will show an "unrecognized app"
echo  warning on download; users click More info -^> Run anyway.
echo =============================================
endlocal
