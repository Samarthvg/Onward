"""Update check against the project's GitHub Releases.

Once a day (when Settings allow it and appinfo.GITHUB_REPO is set) the app
asks GitHub for the latest release. The request carries nothing about the
user: no id, no settings, only the app's version in the User-Agent.

A release holds one installer per platform, named by installer_name(), and
SHA256SUMS listing their hashes. On Windows the update downloads in the
background, is checked against SHA256SUMS (and, once the owner signs builds,
against the publisher in appinfo.WINDOWS_PUBLISHER), then runs silently and
starts the new version. On macOS and Linux the release page opens, because
replacing a signed .app or an AppImage in place is better left to the user.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

import appinfo

API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 20
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); pre-release suffixes are ignored."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return bool(a) and a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def installer_name(version: str, platform: str = sys.platform) -> str:
    v = version.lstrip("v")
    if platform == "win32":
        return f"{appinfo.SLUG}-{v}-windows-setup.exe"
    if platform == "darwin":
        return f"{appinfo.SLUG}-{v}-macos.dmg"
    return f"{appinfo.SLUG}-{v}-x86_64.AppImage"


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept,
                                               "User-Agent": f"{appinfo.SLUG}/{appinfo.VERSION}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def check(repo: str = appinfo.GITHUB_REPO, current: str = appinfo.VERSION, platform: str = sys.platform,
          get: Callable[[str], bytes] = _get) -> dict | None:
    """The latest release when it is newer than `current`, else None."""
    if not repo:
        return None
    rel = json.loads(get(API.format(repo=repo)))
    if rel.get("draft") or rel.get("prerelease") or not newer(rel.get("tag_name", ""), current):
        return None
    version = rel["tag_name"].lstrip("v")
    assets = {a["name"]: a for a in rel.get("assets") or []}
    want = assets.get(installer_name(version, platform))
    return {"version": version, "page": rel.get("html_url") or f"https://github.com/{repo}/releases/latest",
            "notes": (rel.get("body") or "")[:4000],
            "installer": {"name": want["name"], "url": want["browser_download_url"], "size": want.get("size")}
            if want else None,
            "sums": assets["SHA256SUMS"]["browser_download_url"] if "SHA256SUMS" in assets else None}


def expected_hash(sums: str, name: str) -> str | None:
    """The hash for `name` in a sha256sum-style listing ("<hash>  <name>")."""
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _save(url: str, path: Path) -> str:
    """Streams url into path; returns its SHA-256."""
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                                               "User-Agent": f"{appinfo.SLUG}/{appinfo.VERSION}"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, path.open("wb") as f:
        while chunk := r.read(1 << 16):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def download(update: dict, dest: Path | None = None, get: Callable[..., bytes] = _get,
             save: Callable[[str, Path], str] = _save) -> Path:
    """Downloads the installer and checks its SHA-256. Raises UpdateError."""
    inst = update.get("installer")
    if not inst or not update.get("sums"):
        raise UpdateError("This release has no installer for this computer.")
    want = expected_hash(get(update["sums"], "application/octet-stream").decode("utf-8", "replace"), inst["name"])
    if not want:
        raise UpdateError("The release doesn't list a checksum for its installer.")
    dest = Path(dest or tempfile.mkdtemp(prefix=f"{appinfo.SLUG}-update-"))
    path = dest / inst["name"]
    if save(inst["url"], path) != want:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded installer doesn't match its checksum, so it wasn't used.")
    return path


def check_signature(path: Path, publisher: str = appinfo.WINDOWS_PUBLISHER) -> None:
    """With a publisher configured, the installer must carry a valid
    Authenticode signature whose subject has exactly that CN."""
    if not publisher:
        return
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
              "@{status = [string]$s.Status; subject = [string]$s.SignerCertificate.Subject} | ConvertTo-Json")
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
                          capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    try:
        sig = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise UpdateError("Couldn't check the installer's signature.") from e
    cn = re.search(r"(?:^|,\s*)CN=(\"[^\"]*\"|[^,]*)", sig.get("subject") or "")
    if sig.get("status") != "Valid" or not cn or cn.group(1).strip('"') != publisher:
        raise UpdateError("The installer isn't signed by the expected publisher, so it wasn't used.")


def run_installer(path: Path) -> None:
    """Starts the Windows installer silently. It closes this app, installs
    over it and starts the new version (see packaging/windows/installer.iss)."""
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"],
                     creationflags=NO_WINDOW, close_fds=True)
