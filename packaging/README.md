# Building and releasing the installers

One file per platform, built by [PyInstaller](https://pyinstaller.org) and wrapped in the platform's usual
installer format:

| Platform | File                                         | Wrapper                                            |
| -------- | -------------------------------------------- | -------------------------------------------------- |
| Windows  | `it-gets-easier-<version>-windows-setup.exe` | [Inno Setup 6](https://jrsoftware.org/isinfo.php)  |
| macOS    | `it-gets-easier-<version>-macos.dmg`         | `hdiutil`                                          |
| Linux    | `it-gets-easier-<version>-x86_64.AppImage`   | [appimagetool](https://github.com/AppImage/appimagetool) |

## Why PyInstaller

- **Fits the code as it is.** It packages the existing flat set of modules and data files. Briefcase expects a
  BeeWare project layout (a `src/` package configured in `pyproject.toml`), so the repository would have to be
  restructured for it. Briefcase is also built around the Toga UI toolkit, which this app doesn't use.
- **Tray icon.** The app's tray icon is [pystray](https://github.com/moses-palmer/pystray). It uses the Win32
  API through ctypes, AppKit on macOS and Xlib/AppIndicator on Linux, and PyInstaller bundles each of those
  backends (`it-gets-easier.spec` names them).
- **Clean updates.** Neither tool updates an installed app, so updating lives in the app (`updates.py`):
  - It checks GitHub Releases once a day.
  - On Windows it downloads the new installer, verifies its SHA-256 against the release's `SHA256SUMS` (and,
    once the owner signs builds, the publisher in `appinfo.WINDOWS_PUBLISHER`), then runs it silently. Inno
    Setup upgrades in place under the same `AppId`, keeps the data folder, and starts the new version (`/RELAUNCH=1`).
  - On macOS and Linux it opens the release page, since a signed `.app` or an AppImage is best replaced by the user.
- **Per-user install on Windows.** PyInstaller's folder output goes straight into an Inno Setup installer that
  needs no administrator rights. That is also what lets the updater run it without a prompt.

## Build

On the platform you are building for, from the repository root:

```bash
python -m pip install -r packaging/requirements.txt
python packaging/build.py
```

The installer and `SHA256SUMS` land in `dist/`. Windows needs Inno Setup 6 (`iscc` on PATH, or its default
install folder). Linux needs `appimagetool` on PATH.

## Release

1. Bump `VERSION` in `appinfo.py`, commit, and tag it: `git tag v0.2.0 && git push --tags`.
2. `.github/workflows/release.yml` builds all three installers, writes one `SHA256SUMS` for them and publishes
   the GitHub release. The app's update check reads exactly that release.
3. Set `GITHUB_REPO` in `appinfo.py` to `owner/repo` once. Until then the update check is off.

When the extension is listed on addons.mozilla.org or the Chrome Web Store, put the listing URLs in `appinfo.py`
(`FIREFOX_ADDON_URL`, `CHROME_STORE_URL`) and the setup steps link to them. Until then they offer a signed
`.xpi` shipped with the app (`extension/web-ext-artifacts/*.xpi`, see `extension/README.md`) or Load unpacked.

## Code signing (the owner's steps)

Unsigned builds work, but Windows SmartScreen and macOS Gatekeeper warn about them. Signing uses your own
certificates. Nothing in this repository holds or creates one.

### Windows

1. Get an Authenticode code-signing certificate, from a certificate authority or through Azure Trusted
   Signing. It comes as a `.pfx` with a password.
2. Local builds: set `SIGNTOOL_CMD` before `build.py`. It signs the app's `.exe` and the installer:

   ```bash
   set SIGNTOOL_CMD=signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f C:\path\cert.pfx /p YOUR_PASSWORD
   ```

3. CI builds: add repository secrets `WINDOWS_CERT_PFX` (the `.pfx`, base64-encoded) and `WINDOWS_CERT_PASSWORD`.
4. Put the certificate's subject name (its CN) in `appinfo.WINDOWS_PUBLISHER`. From then on, the updater installs
   only installers signed by exactly that publisher.

### macOS

1. Join the Apple Developer Program and create a **Developer ID Application** certificate. Export it from
   Keychain Access as a `.p12`.
2. Local builds: with the certificate in your keychain, set `MACOS_SIGN_IDENTITY` to its name, for example
   `Developer ID Application: Jane Doe (TEAMID)`. To also notarize, create an app-specific password at
   appleid.apple.com and set `APPLE_ID`, `APPLE_TEAM_ID` and `APPLE_APP_PASSWORD`. `build.py` then signs with
   the hardened runtime, notarizes the `.dmg` and staples the ticket.
3. CI builds: add the secrets `MACOS_CERT_P12` (base64), `MACOS_CERT_PASSWORD`, `MACOS_SIGN_IDENTITY`,
   `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`.

### Linux

AppImages aren't signed by default. If you want to, sign the AppImage with your own GPG key
(`appimagetool --sign`) and publish the key.

## What was tested

- The Windows build from this spec: built with PyInstaller 6.22.3. The frozen app served the dashboard, copied
  the extension to its data folder, found TeX and the ChatGPT login, turned on Start at login, and ran a full
  search through its own `--radar` child process.
- Not yet run: the Inno Setup installer, and the macOS and Linux builds. Neither tool was available on the
  machine this was written on. The release workflow builds both on GitHub's runners.
