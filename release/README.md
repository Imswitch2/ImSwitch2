# Installer build tooling

Scripts that turn a checkout into a standalone ImSwitch2 bundle. The full
procedure, the limits of a frozen build, and the code-signing situation are
documented in [`docs/packaging.rst`](../docs/packaging.rst) — this directory is
just the moving parts.

Publishing a GitHub release runs all of this in CI and attaches the installers
to the release, so these scripts are for local testing and for building off a
branch.

| Path | What it is |
|---|---|
| `pyinstaller/imswitch_bundle_entry.py` | Bundle-only entry point. Sets `IMSWITCH_IS_BUNDLE` and adds `--bundle-selftest`. |
| `windows/create_installer_windows.bat` | Windows: clean venv → wheel → PyInstaller → self-test → Inno Setup `.exe`. |
| `windows/imswitch_innoinstaller.iss` | Inno Setup script. Driven by the `.bat`, not run directly. |
| `macos/create_macos_dmg.sh` | macOS: clean venv → wheel → PyInstaller → ad-hoc sign → self-test → `.dmg`. |
| `macos/ImSwitch2.icns` | App icon, generated from `imswitch/_data/icon.png`. |

The PyInstaller spec itself is [`imswitch.spec`](../imswitch.spec) at the repo
root, shared by both platforms.

```bash
bash release/macos/create_macos_dmg.sh            # macOS
release\windows\create_installer_windows.bat      # Windows
```

Both build a throwaway virtualenv and install a freshly built wheel into it,
rather than using your development environment — without real `.dist-info`
metadata the bundle's update check and device plugin registry come up empty.
See *Why the scripts build a wheel* in the docs.

Outputs land in `build/windows/` and `build/macos/`, both gitignored.
