# PyInstaller spec for the app: python -m PyInstaller packaging/onward.spec
# (packaging/build.py runs this, then wraps the result into the installer).
#
# Windows: dist/Onward/ (a folder, which the Inno Setup installer installs)
# macOS:   dist/Onward.app
# Linux:   dist/onward/ (a folder, which build.py turns into an AppImage)

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
import appinfo  # noqa: E402

NAME = appinfo.NAME if sys.platform != "linux" else appinfo.SLUG
ICON = {"win32": ROOT / "build" / "icon.ico", "darwin": ROOT / "build" / "icon.icns"}.get(sys.platform)


def extension_files():
    """The Autofill extension as it ships: no tests, no node_modules, only signed builds."""
    ext = ROOT / "extension"
    for f in ext.rglob("*"):
        rel = f.relative_to(ext)
        if not f.is_file() or rel.parts[0] in ("test", "node_modules"):
            continue
        if rel.parts[0] == "web-ext-artifacts" and f.suffix != ".xpi":
            continue
        yield str(f), str(Path("extension") / rel.parent)


datas = [(str(ROOT / name), ".") for name in ("dashboard_template.html", "companies.json", "skills.json")]
datas += list(extension_files())

hidden = {"win32": ["pystray._win32"], "darwin": ["pystray._darwin"],
          "linux": ["pystray._xorg", "pystray._appindicator", "pystray._gtk"]}.get(sys.platform, [])
hidden += ["keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService",
           "keyring.backends.libsecret", "keyring.backends.kwallet"]
# The dashboard window's backends: Edge WebView2 through pythonnet on Windows, WebKit on macOS.
hidden += {"win32": ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"],
           "darwin": ["webview.platforms.cocoa"]}.get(sys.platform, [])

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=False,                      # a tray app: no console window
    icon=str(ICON) if ICON and ICON.exists() else None,
    codesign_identity=None,             # macOS signing happens in build.py, after the bundle is made
    version=str(ROOT / "build" / "version.txt") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{appinfo.NAME}.app",
        icon=str(ICON) if ICON and ICON.exists() else None,
        bundle_identifier=appinfo.BUNDLE_ID,
        version=appinfo.VERSION,
        info_plist={
            "CFBundleName": appinfo.NAME,
            "CFBundleDisplayName": appinfo.NAME,
            "CFBundleShortVersionString": appinfo.VERSION,
            "CFBundleVersion": appinfo.VERSION,
            "LSUIElement": True,        # lives in the menu bar, no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
