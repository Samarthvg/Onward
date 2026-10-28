"""Start Onward when the user logs in, and retire the old scheduled tasks.

Per user, no administrator rights needed:
  Windows  a value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
  macOS    ~/Library/LaunchAgents/<bundle id>.plist
  Linux    $XDG_CONFIG_HOME/autostart/onward.desktop (~/.config/autostart)

Before the app existed, two Windows scheduled tasks did its job: "JobRadar"
ran radar.py every 30 minutes and "JobRadar Helper" started server.py at
logon. The app replaces both; remove_legacy_tasks() stops and deletes them
when they fed the app's own data folder.
"""

from __future__ import annotations

import html
import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import appinfo
import config

LEGACY_TASKS = ("JobRadar Helper", "JobRadar")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# The login item's flag: started at login, the app stays in the tray rather
# than opening its window.
LOGIN_FLAG = "--login"


def launch_command() -> list[str]:
    """How the login item starts this install of the app."""
    if os.environ.get("APPIMAGE"):              # the AppImage file, not its temporary mount
        return [os.environ["APPIMAGE"], LOGIN_FLAG]
    if getattr(sys, "frozen", False):
        return [sys.executable, LOGIN_FLAG]
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")      # no console window at login
    return [str(exe), str(Path(__file__).resolve().parent / "app.py"), LOGIN_FLAG]


def _win_quote(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd)


class Autostart:
    """Login item for one platform. `name` and the folders are parameters so
    tests can use their own."""

    def __init__(self, name: str = appinfo.NAME, platform: str = sys.platform, home: Path | None = None) -> None:
        self.name, self.platform = name, platform
        self.home = Path(home) if home else Path.home()

    # ---------------------------------------------------------------- paths

    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{appinfo.BUNDLE_ID}.plist"

    def desktop_path(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or self.home / ".config")
        return base / "autostart" / f"{appinfo.SLUG}.desktop"

    # ---------------------------------------------------------------- API

    def enabled(self) -> bool:
        if self.platform == "win32":
            return self._win_get() is not None
        if self.platform == "darwin":
            return self.plist_path().exists()
        return self.desktop_path().exists()

    def command(self) -> str | None:
        """The registered command line, for checking it points at this install."""
        if self.platform == "win32":
            return self._win_get()
        if self.platform == "darwin":
            try:
                return " ".join(plistlib.loads(self.plist_path().read_bytes())["ProgramArguments"])
            except (OSError, KeyError, plistlib.InvalidFileException):
                return None
        try:
            for line in self.desktop_path().read_text(encoding="utf-8").splitlines():
                if line.startswith("Exec="):
                    return line[5:]
        except OSError:
            pass
        return None

    def enable(self, cmd: list[str] | None = None) -> None:
        cmd = cmd or launch_command()
        if self.platform == "win32":
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, self.name, 0, winreg.REG_SZ, _win_quote(cmd))
        elif self.platform == "darwin":
            p = self.plist_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(plistlib.dumps({"Label": appinfo.BUNDLE_ID, "ProgramArguments": cmd,
                                          "RunAtLoad": True, "ProcessType": "Interactive"}))
        else:
            p = self.desktop_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("[Desktop Entry]\nType=Application\n"
                         f"Name={appinfo.NAME}\nComment={appinfo.TAGLINE}\n"
                         f"Exec={' '.join(shlex.quote(c) for c in cmd)}\n"
                         "Terminal=false\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")

    def disable(self) -> None:
        if self.platform == "win32":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, self.name)
            except FileNotFoundError:
                pass
        else:
            p = self.plist_path() if self.platform == "darwin" else self.desktop_path()
            p.unlink(missing_ok=True)

    def _win_get(self) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                return winreg.QueryValueEx(k, self.name)[0]
        except FileNotFoundError:
            return None


def task_script(xml: str) -> Path | None:
    """The .py file a scheduled task's action runs, from schtasks /XML."""
    m = re.search(r"<Arguments>(.*?)</Arguments>", xml, re.S)
    found = re.search(r'"?([^"<>]+?\.pyw?)"?(?:\s|$)', html.unescape(m.group(1)).strip()) if m else None
    return Path(found.group(1)) if found else None


def data_home_of(code_dir: Path) -> Path:
    """The data folder code in code_dir used when started by a task, which
    sets no ONWARD_HOME (config.default_home)."""
    if (code_dir / config.SETTINGS_FILE).exists() or (code_dir / "jobs.db").exists():
        return code_dir
    return config.user_data_dir()


def remove_legacy_tasks(home: Path, names: tuple[str, ...] = LEGACY_TASKS, run=subprocess.run) -> list[str]:
    """Stops and deletes the old scheduled tasks that fed this data folder.
    Returns the names removed. Ending "JobRadar Helper" also stops the old
    server.py, which would otherwise keep the dashboard's port. A task that
    runs a copy with another data folder is left alone."""
    if sys.platform != "win32":
        return []
    kw = {"capture_output": True, "text": True, "creationflags": NO_WINDOW, "errors": "replace"}
    removed = []
    for name in names:
        try:
            q = run(["schtasks", "/Query", "/TN", name, "/XML"], **kw)
            if q.returncode != 0:
                continue
            script = task_script(q.stdout)
            if not script or data_home_of(script.parent).resolve() != Path(home).resolve():
                continue
            run(["schtasks", "/End", "/TN", name], **kw)
            if run(["schtasks", "/Delete", "/TN", name, "/F"], **kw).returncode == 0:
                removed.append(name)
        except OSError:
            break
    return removed
