# Building and releasing the installers

One file per platform, built by [PyInstaller](https://pyinstaller.org) and wrapped in the platform's usual
installer format:

| Platform | File                                         | Wrapper                                            |
| -------- | -------------------------------------------- | -------------------------------------------------- |
| Windows  | `onward-<version>-windows-setup.exe` | [Inno Setup 6](https://jrsoftware.org/isinfo.php)  |
| macOS    | `onward-<version>-macos.dmg`         | `hdiutil`                                          |
| Linux    | `onward-<version>-x86_64.AppImage`   | [appimagetool](https://github.com/AppImage/appimagetool) |

## Why PyInstaller

- **Fits the code as it is.** It packages the existing flat set of modules and data files. Briefcase expects a
  BeeWare project layout (a `src/` package configured in `pyproject.toml`), so the repository would have to be
  restructured for it. Briefcase is also built around the Toga UI toolkit, which this app doesn't use.
- **Tray icon.** The app's tray icon is [pystray](https://github.com/moses-palmer/pystray). It uses the Win32
  API through ctypes, AppKit on macOS and Xlib/AppIndicator on Linux, and PyInstaller bundles each of those
  backends (`onward.spec` names them).
- **Dashboard window.** `Onward --window` shows the dashboard with [pywebview](https://pywebview.flowrl.com):
  Edge WebView2 through pythonnet on Windows, WebKit on macOS. It runs as its own process, started from the
  tray. The Linux AppImage doesn't bundle a web view, so there the dashboard opens in the browser.
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

1. Bump `VERSION` in `appinfo.py`, commit, and tag it: `git tag v0.1.4 && git push origin main v0.1.4`.
2. `.github/workflows/release.yml` builds all three installers, writes one `SHA256SUMS` for them and publishes
   the GitHub release. The app's update check reads exactly that release; pre-releases are skipped, so a test
   build can be published as one without reaching anyone.
3. `GITHUB_REPO` in `appinfo.py` names the repository (`Samarthvg/Onward`). Empty, the update check is off.

Before tagging, check the changes with `python preview.py` (see the main README) and run the tests.

The release also carries the Autofill extension (`packaging/extension.py`, run first by the workflow):

- `onward-autofill-<v>.xpi`: the Firefox add-on, signed by Mozilla for self-distribution. It is bundled inside
  every installer, so the setup steps' **Install in Firefox** button installs it permanently, with no
  developer settings. Firefox then keeps it up to date from `updates.json` in the latest release (the
  `update_url` in `extension/manifest.json`).
- `onward-autofill-<v>-chrome.zip`: ready to upload to the Chrome Web Store. Chrome only installs extensions
  from its store, so until a listing exists, Chrome and Edge users use Load unpacked.

Mozilla signs each version number once. Bump `version` in `extension/manifest.json` whenever the extension
changes; the workflow fails with a clear message if you forget, and reuses the earlier signed file when the
extension didn't change.

When the extension is listed on addons.mozilla.org or the Chrome Web Store, put the listing URLs in `appinfo.py`
(`FIREFOX_ADDON_URL`, `CHROME_STORE_URL`) and the setup steps link to them instead.

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

### Firefox add-on

Mozilla signs add-ons for free, including ones distributed outside its store ("unlisted"):

1. Sign in at <https://addons.mozilla.org/developers/> with your own Firefox account. Under **Tools > Manage API
   Keys**, create API credentials: a JWT issuer and a JWT secret.
2. CI builds: add them as repository secrets `AMO_JWT_ISSUER` and `AMO_JWT_SECRET`.
3. Local builds: set the same two environment variables, run `npm ci` once in `extension/test`, then run
   `python packaging/extension.py`. Mozilla's automated review usually takes a minute or two.

The first signed version creates the add-on under your account, with the id in `extension/manifest.json`.
Keep that id: installed copies only update from an add-on with the same id.

### Linux

AppImages aren't signed by default. If you want to, sign the AppImage with your own GPG key
(`appimagetool --sign`) and publish the key.

## What was tested

- **Windows:** the release installers (0.1.1 to 0.1.3) have been installed and upgraded in place on Windows 11,
  silently with `/SILENT /RELAUNCH=1` as the updater runs them, keeping the data folder. The frozen app serves
  the dashboard, opens its window, copies the extension to its data folder, finds TeX and the ChatGPT login,
  turns on Start at login, and runs searches through its own `--radar` child process.
- **macOS and Linux:** the release workflow builds both on GitHub's runners on every release, but neither has
  been installed and run on a real machine yet.
