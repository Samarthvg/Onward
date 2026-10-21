"""Desktop notifications, plus an optional push through ntfy.

Windows gets a toast through PowerShell's WinRT bridge (no extra packages);
macOS uses osascript and Linux notify-send. ntfy only runs when the user has
set a topic, and sends only the title and company names.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, time as dtime
from xml.sax.saxutils import escape, quoteattr

# Toasts need an app identity Windows already knows; PowerShell's is always registered.
WIN_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
WIN_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:JR_TOAST_XML)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:JR_TOAST_APP).Show(
  (New-Object Windows.UI.Notifications.ToastNotification $xml))
"""


def desktop(title: str, body: str, url: str = "") -> bool:
    try:
        if sys.platform == "win32":
            launch = f' activationType="protocol" launch={quoteattr(url)}' if url else ""
            xml = (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
                   f"<text>{escape(body)}</text></binding></visual></toast>")
            # Text goes through the environment, never into the script itself.
            env = {**os.environ, "JR_TOAST_XML": xml, "JR_TOAST_APP": WIN_APP_ID}
            proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_SCRIPT],
                                  env=env, capture_output=True, timeout=30,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return proc.returncode == 0
        if sys.platform == "darwin":
            script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
            return subprocess.run(["osascript", "-e", script, title, body], capture_output=True, timeout=30).returncode == 0
        if shutil.which("notify-send"):
            return subprocess.run(["notify-send", title, body], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def ntfy(server: str, topic: str, title: str, body: str) -> bool:
    if not topic:
        return False
    req = urllib.request.Request(f"{server.rstrip('/')}/{topic}", data=body.encode("utf-8"),
                                 headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "briefcase"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except OSError:
        return False


def _hm(s: str) -> dtime | None:
    try:
        h, m = str(s).split(":")
        return dtime(int(h), int(m))
    except ValueError:
        return None


def quiet(now: datetime, start: str, end: str) -> bool:
    """Whether now falls in the quiet window; the window may cross midnight."""
    a, b = _hm(start), _hm(end)
    if a is None or b is None or a == b:
        return False
    t = now.time()
    return a <= t < b if a < b else (t >= a or t < b)
