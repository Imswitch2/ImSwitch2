; Inno Setup script for the ImSwitch2 Windows installer.
;
; Not invoked directly -- create_installer_windows.bat passes the three defines
; below after PyInstaller has produced the bundle:
;
;   ISCC.exe /DAPP_VERSION=0.2.0 /DSOURCE_DIR=...\dist\ImSwitch2 /DOUTPUT_DIR=...
;
; See docs/packaging.rst.

#ifndef APP_VERSION
  #define APP_VERSION "0.0.0"
#endif
#ifndef SOURCE_DIR
  #define SOURCE_DIR "..\..\build\windows\dist\ImSwitch2"
#endif
#ifndef OUTPUT_DIR
  #define OUTPUT_DIR "..\..\build\windows"
#endif

#define APP_NAME "ImSwitch2"
#define APP_PUBLISHER "The ImSwitch2 developers"
#define APP_URL "https://github.com/Imswitch2/Imswitch2"
#define APP_EXE "ImSwitch2.exe"

[Setup]
; A distinct AppId from any upstream ImSwitch installer, so the two can coexist
; and neither one's uninstaller removes the other.
AppId={{8F2A6C1E-4B7D-4E93-9A31-2C0D5E7B41A6}
AppName={#APP_NAME}
AppVersion={#APP_VERSION}
AppVerName={#APP_NAME} {#APP_VERSION}
AppPublisher={#APP_PUBLISHER}
AppPublisherURL={#APP_URL}
AppSupportURL={#APP_URL}/issues
AppUpdatesURL={#APP_URL}/releases
DefaultDirName={autopf}\{#APP_NAME}
DefaultGroupName={#APP_NAME}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OUTPUT_DIR}
OutputBaseFilename={#APP_NAME}-{#APP_VERSION}-win64-setup
SetupIconFile=..\..\imswitch\_data\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; The bundle is 64-bit only; PyInstaller does not produce a fat binary.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Let the user pick between a machine-wide install under Program Files and a
; per-user one.  Rig PCs are usually administered, but shared microscope
; facilities frequently are not, and a per-user install is better than none.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SOURCE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#APP_NAME}"; Filename: "{app}\{#APP_EXE}"
Name: "{group}\{cm:UninstallProgram,{#APP_NAME}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#APP_NAME}"; Filename: "{app}\{#APP_EXE}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#APP_EXE}"; Description: "{cm:LaunchProgram,{#StringChange(APP_NAME, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller extracts nothing outside {app}, but Python leaves __pycache__
; directories behind that Inno's file manifest does not know about.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

; NOTE: user configuration lives in %USERPROFILE%\Documents\ImSwitchConfig and
; is deliberately NOT removed on uninstall -- it holds the setup JSON files that
; describe the user's microscope, which are often the only copy.
