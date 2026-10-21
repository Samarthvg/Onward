"""Which program turns a .tex resume into a PDF.

1. An installed TeX (MiKTeX, TeX Live, MacTeX): xelatex or pdflatex, the
   engine the resume asks for (resume_tex.engine_for). Found on PATH or in
   the folders those distributions install to, since an app started at
   login may not see the user's PATH.
2. Otherwise Tectonic, a single program that compiles the same .tex files
   (it is XeTeX-based and fetches the LaTeX packages a resume uses the first
   time, then works from its cache). It is downloaded only when the user
   agrees, from Tectonic's own GitHub release, pinned to one version and
   checked against its SHA-256, into the data folder's tools/.

What each path supports, in words for the dashboard, is FORMATS.
"""

from __future__ import annotations

import glob
import hashlib
import os
import platform
import shutil
import stat
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import appinfo
import config

TECTONIC_VERSION = "0.17.0"
TECTONIC_URL = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40{v}/{name}"
# (sys.platform, machine) -> (asset, sha256, bytes), from the 0.17.0 release.
TECTONIC_ASSETS = {
    ("win32", "amd64"): ("tectonic-0.17.0-x86_64-pc-windows-msvc.zip",
                         "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f", 21060223),
    ("darwin", "x86_64"): ("tectonic-0.17.0-x86_64-apple-darwin.tar.gz",
                           "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80", 21790177),
    ("darwin", "arm64"): ("tectonic-0.17.0-aarch64-apple-darwin.tar.gz",
                          "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667", 21704674),
    ("linux", "x86_64"): ("tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz",
                          "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7", 10151914),
    ("linux", "aarch64"): ("tectonic-0.17.0-aarch64-unknown-linux-musl.tar.gz",
                           "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8", 9926834),
}

# Lines Tectonic needs before a resume written for pdflatex: XeTeX has no
# \pdfgentounicode and friends, which templates like "Jake's resume" set.
# Only the copy being compiled gets them; the master stays as it is.
PDFTEX_SHIM = "\n".join([
    r"\ifdefined\pdfgentounicode\else\newcount\pdfgentounicode\fi",
    r"\ifdefined\pdfglyphtounicode\else\def\pdfglyphtounicode#1#2{}\fi",
    r"\ifdefined\pdfminorversion\else\newcount\pdfminorversion\fi",
    r"\ifdefined\pdfcompresslevel\else\newcount\pdfcompresslevel\fi",
    r"\ifdefined\pdfobjcompresslevel\else\newcount\pdfobjcompresslevel\fi",
    r"\ifdefined\pdfsuppresswarningpagegroup\else\newcount\pdfsuppresswarningpagegroup\fi",
]) + "\n"

FORMATS = {
    "tex": "Your .tex resume is tailored in place: its layout, fonts and macros stay exactly as they are, and only "
           "bullet wording, tool lists and skills rows change.",
    "converted": "A PDF or text resume is first turned into a .tex master with a plain built-in layout. You check it "
                 "once, and from then on it is tailored the same way.",
    "none": "Without a PDF engine, match scores, the apply queue's picks and the Apply kit still work with PDF, .tex "
            "and .txt resumes. Only tailored PDFs need one.",
    "word": "Word files aren't read yet. Save the resume as PDF from Word and upload that.",
}


def _home() -> Path:
    return config.store().home


def tools_dir() -> Path:
    return _home() / "tools"


def _machine() -> str:
    """The machine name TECTONIC_ASSETS uses on this platform."""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64" if sys.platform == "win32" else "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64" if sys.platform == "darwin" else "aarch64"
    return m


def tectonic_asset() -> tuple[str, str, int] | None:
    return TECTONIC_ASSETS.get((sys.platform, _machine()))


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def tex_dirs() -> list[Path]:
    """Where TeX distributions put their programs, newest first."""
    env = os.environ
    if sys.platform == "win32":
        pats = [Path(env.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64",
                Path(env.get("ProgramFiles", r"C:\Program Files")) / "MiKTeX" / "miktex" / "bin" / "x64",
                *map(Path, sorted(glob.glob(r"C:\texlive\*\bin\win*"), reverse=True))]
    elif sys.platform == "darwin":
        pats = [Path("/Library/TeX/texbin"), *map(Path, sorted(glob.glob("/usr/local/texlive/*/bin/*"), reverse=True))]
    else:
        pats = [*map(Path, sorted(glob.glob("/usr/local/texlive/*/bin/*"), reverse=True)), Path("/usr/bin")]
    return [p for p in pats if str(p) not in ("", ".")]


def find_tex(engine: str) -> str | None:
    """An installed xelatex or pdflatex."""
    hit = shutil.which(engine)
    if hit:
        return hit
    for d in tex_dirs():
        p = d / _exe(engine)
        if p.is_file():
            return str(p)
    return None


def tectonic_path() -> str | None:
    """Tectonic downloaded by the app, else one the user installed."""
    own = tools_dir() / _exe("tectonic")
    return str(own) if own.is_file() else shutil.which("tectonic")


def tectonic_env() -> dict:
    """Tectonic keeps the packages it fetches with the app's data, so
    uninstalling the data folder removes them too."""
    return {**os.environ, "TECTONIC_CACHE_DIR": str(tools_dir() / "tectonic-cache")}


def pick(engine: str) -> tuple[str, str] | None:
    """("tex", path) or ("tectonic", path) for compiling a resume that asks
    for `engine`, preferring an installed TeX."""
    tex = find_tex(engine)
    if tex:
        return "tex", tex
    tec = tectonic_path()
    return ("tectonic", tec) if tec else None


# --------------------------------------------------------------------------
# Download (only after the user agrees, from the dashboard)

class Download:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {"state": "idle", "done": 0, "total": 0, "error": ""}
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, opener=urllib.request.urlopen) -> bool:
        asset = tectonic_asset()
        with self.lock:
            if not asset or self.state["state"] == "running":
                return False
            self.state = {"state": "running", "done": 0, "total": asset[2], "error": ""}
        self._thread = threading.Thread(target=self._run, args=(asset, opener), name="tectonic-download", daemon=True)
        self._thread.start()
        return True

    def _run(self, asset: tuple[str, str, int], opener) -> None:
        name, want, size = asset
        dest = tools_dir()
        dest.mkdir(parents=True, exist_ok=True)
        part = dest / (name + ".part")
        try:
            req = urllib.request.Request(TECTONIC_URL.format(v=TECTONIC_VERSION, name=name),
                                         headers={"User-Agent": f"{appinfo.SLUG}/{appinfo.VERSION}"})
            h = hashlib.sha256()
            with opener(req, timeout=60) as r, part.open("wb") as f:
                while chunk := r.read(1 << 16):
                    h.update(chunk)
                    f.write(chunk)
                    with self.lock:
                        self.state["done"] += len(chunk)
            if h.hexdigest() != want:
                raise ValueError("the download doesn't match Tectonic's published checksum")
            exe = _exe("tectonic")
            if name.endswith(".zip"):
                with zipfile.ZipFile(part) as z:
                    data = z.read(exe)
            else:
                with tarfile.open(part, "r:gz") as t:
                    member = next(m for m in t.getmembers() if Path(m.name).name == exe and m.isfile())
                    data = t.extractfile(member).read()
            tmp = dest / (exe + ".tmp")
            tmp.write_bytes(data)
            tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            tmp.replace(dest / exe)
            with self.lock:
                self.state.update(state="done", finished=time.time())
        except Exception as e:
            with self.lock:
                self.state.update(state="error", error=f"Couldn't download Tectonic: {e}")
        finally:
            part.unlink(missing_ok=True)


DOWNLOAD = Download()


def status() -> dict:
    """For the dashboard: what compiles resumes here and what that supports."""
    tex = {e: find_tex(e) for e in ("xelatex", "pdflatex")}
    tec = tectonic_path()
    asset = tectonic_asset()
    active = "tex" if any(tex.values()) else "tectonic" if tec else None
    return {
        "active": active, "tex": tex, "tectonic": tec,
        "download": {**DOWNLOAD.snapshot(), "available": bool(asset), "size": asset[2] if asset else 0,
                     "version": TECTONIC_VERSION},
        "formats": FORMATS,
    }
