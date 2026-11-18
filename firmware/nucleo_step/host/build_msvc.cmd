@echo off
rem The Nucleo's firmware as a fake board on this PC, with Visual Studio's C++
rem compiler (any edition with "Desktop development with C++").
rem Usage: build_msvc.cmd [output.exe]   (default: nucleo_host.exe here)
rem Then: set IMSWITCH_NUCLEO_HOST=<that exe> and run
rem       pytest imswitch/imcontrol/_test/unit/test_nucleo_firmware_host.py
setlocal
set "HERE=%~dp0"
set "OUT=%~1"
if "%OUT%"=="" set "OUT=%HERE%nucleo_host.exe"
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS=%%i"
if not defined VS (
  echo No Visual Studio with the C++ tools was found.
  exit /b 1
)
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>nul || exit /b 1
set "OBJ=%TEMP%\nucleo_host_obj"
if not exist "%OBJ%" mkdir "%OBJ%"
cl /nologo /std:c++14 /EHsc /O2 /W3 /D_CRT_SECURE_NO_WARNINGS ^
  /I"%HERE%..\NucleoStep" /I"%HERE%..\..\triggerscope_step\TriggerScopeStep" ^
  /Fo"%OBJ%\\" /Fe"%OUT%" "%HERE%nucleo_host.cpp" "%HERE%..\NucleoStep\nucleo_step.cpp"
