"""Builds this platform's installer into dist/.

    python packaging/build.py

  Windows  dist/onward-<v>-windows-setup.exe   (needs Inno Setup 6: iscc on PATH)
  macOS    dist/onward-<v>-macos.dmg
  Linux    dist/onward-<v>-x86_64.AppImage     (needs appimagetool on PATH)

and dist/SHA256SUMS, which the update check verifies downloads against.
Upload both to a GitHub release tagged v<version> (.github/workflows/release.yml
does all of this on a tag push).

Signing is optional here and uses the owner's own certificates, read from the
environment; packaging/README.md has the steps.
  Windows  SIGNTOOL_CMD   e.g. signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f cert.pfx /p ...
  macOS    MACOS_SIGN_IDENTITY ("Developer ID Application: Name (TEAMID)"), and for notarizing
           APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import appinfo  # noqa: E402
import updates  # noqa: E402

BUILD, DIST = ROOT / "build", ROOT / "dist"
ICON_PNG = ROOT / "extension" / "icons" / "icon-128.png"


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(shlex.quote(str(c)) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def icons() -> None:
    from PIL import Image
    BUILD.mkdir(exist_ok=True)
    img = Image.open(ICON_PNG).convert("RGBA")
    big = img.resize((256, 256), Image.LANCZOS)
    big.save(BUILD / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin":
        big.resize((512, 512), Image.LANCZOS).save(BUILD / "icon.icns")
    big.save(BUILD / "icon.png")


def version_file() -> None:
    """Windows file properties: the name and version Explorer shows."""
    v = tuple(int(x) for x in appinfo.VERSION.split(".")) + (0,) * (4 - len(appinfo.VERSION.split(".")))
    (BUILD / "version.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', {appinfo.NAME!r}),
    StringStruct('ProductName', {appinfo.NAME!r}),
    StringStruct('FileVersion', {appinfo.VERSION!r}),
    StringStruct('ProductVersion', {appinfo.VERSION!r}),
    StringStruct('OriginalFilename', {appinfo.NAME + '.exe'!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8")


def pyinstaller() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST,
         "--workpath", BUILD / "pyinstaller", ROOT / "packaging" / "onward.spec"])


def sign_windows(path: Path) -> None:
    cmd = os.environ.get("SIGNTOOL_CMD")
    if cmd:
        # A Windows command line as the owner wrote it, quotes and all.
        print(f"+ signing {path.name}", flush=True)
        subprocess.run(f'{cmd} "{path}"', shell=True, check=True)


def windows() -> Path:
    exe = DIST / appinfo.NAME / f"{appinfo.NAME}.exe"
    sign_windows(exe)
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    out = DIST / updates.installer_name(appinfo.VERSION, "win32")
    run([iscc, f"/DAppVersion={appinfo.VERSION}", f"/DAppName={appinfo.NAME}", f"/DOutputDir={DIST}",
         f"/DOutputName={out.stem}", f"/DSourceDir={DIST / appinfo.NAME}", f"/DIconFile={BUILD / 'icon.ico'}",
         ROOT / "packaging" / "windows" / "installer.iss"])
    sign_windows(out)
    return out


def macos() -> Path:
    app = DIST / f"{appinfo.NAME}.app"
    ident = os.environ.get("MACOS_SIGN_IDENTITY")
    if ident:
        run(["codesign", "--force", "--deep", "--options", "runtime", "--timestamp", "--sign", ident, app])
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run(["ditto", app, stage / app.name])
    (stage / "Applications").symlink_to("/Applications")
    out = DIST / updates.installer_name(appinfo.VERSION, "darwin")
    run(["hdiutil", "create", "-volname", appinfo.NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", out])
    if ident:
        run(["codesign", "--sign", ident, "--timestamp", out])
    if ident and os.environ.get("APPLE_ID"):
        run(["xcrun", "notarytool", "submit", out, "--apple-id", os.environ["APPLE_ID"],
             "--team-id", os.environ["APPLE_TEAM_ID"], "--password", os.environ["APPLE_APP_PASSWORD"], "--wait"])
        run(["xcrun", "stapler", "staple", out])
    return out


def linux() -> Path:
    appdir = BUILD / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(DIST / appinfo.SLUG, appdir / "usr" / "lib" / appinfo.SLUG)
    run_sh = appdir / "AppRun"
    run_sh.write_text(f'#!/bin/sh\nexec "$APPDIR/usr/lib/{appinfo.SLUG}/{appinfo.SLUG}" "$@"\n', encoding="utf-8")
    run_sh.chmod(0o755)
    (appdir / f"{appinfo.SLUG}.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName={appinfo.NAME}\nComment={appinfo.TAGLINE}\n"
        f"Exec={appinfo.SLUG}\nIcon={appinfo.SLUG}\nCategories=Office;\nTerminal=false\n", encoding="utf-8")
    shutil.copyfile(BUILD / "icon.png", appdir / f"{appinfo.SLUG}.png")
    out = DIST / updates.installer_name(appinfo.VERSION, "linux")
    run(["appimagetool", appdir, out], env={**os.environ, "ARCH": "x86_64"})
    return out


def checksums(files: list[Path]) -> None:
    lines = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}" for f in files]
    (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    icons()
    if sys.platform == "win32":
        version_file()
    pyinstaller()
    out = {"win32": windows, "darwin": macos}.get(sys.platform, linux)()
    checksums([out])
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
