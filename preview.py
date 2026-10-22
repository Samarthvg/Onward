"""See changes made in this folder before releasing them: the dashboard and
helper run from this code, on their own port, with a copy of the installed
app's data, next to the installed app.

    python preview.py           rebuild the page from this code and serve it
    python preview.py --fresh   copy the installed app's data again first

It serves http://127.0.0.1:8766/ until Ctrl+C. The copy is the installed
app's data folder as it was when copied, in a folder of its own, so nothing
done in the preview reaches the real one. It doesn't search on a schedule
(Search now still works) and its apply queue is off, so it uses none of the
day's AI runs. The extension keeps talking to the installed app on 8765.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import webbrowser
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
PORT = 8766
COPIED = ("settings.json", "resume", "data")


def preview_home() -> Path:
    return config.platform_data_dir().with_name(config.platform_data_dir().name + "-preview")


def copy_data(src: Path, dst: Path) -> None:
    """The installed app's data, as of now; the database through SQLite's
    backup, so a search writing to it meanwhile can't leave a torn copy."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in COPIED:
        p = src / name
        if p.is_dir():
            shutil.copytree(p, dst / name)
        elif p.exists():
            shutil.copy2(p, dst / name)
    if (src / "jobs.db").exists():
        a, b = sqlite3.connect(src / "jobs.db"), sqlite3.connect(dst / "jobs.db")
        try:
            a.backup(b)
        finally:
            a.close()
            b.close()
    # The preview never prepares jobs on its own and never offers updates.
    st = config.Store(dst / "settings.json", dst)
    st.update({"queue": {"enabled": False}, "app": {"check_updates": False}}, strict=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fresh", action="store_true", help="copy the installed app's data again")
    ap.add_argument("--no-open", action="store_true", help="don't open the page")
    args = ap.parse_args(argv)

    src, home = config.platform_data_dir(), preview_home()
    if args.fresh or not (home / "settings.json").exists():
        if not (src / "settings.json").exists():
            print(f"No installed app's data in {src} to copy. Install Onward, or set ONWARD_HOME to a data folder.")
            return 1
        print(f"Copying the installed app's data from {src} ...")
        copy_data(src, home)
    env = {**{k: v for k, v in os.environ.items() if k not in config.HOME_ENV},
           config.HOME_ENV[0]: str(home), "JOBRADAR_PORT": str(PORT)}
    print("Building the page from this folder's code ...")
    subprocess.run([sys.executable, str(ROOT / "radar.py"), "--render"], cwd=ROOT, env=env, check=True)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Preview at {url} (data: {home}). Ctrl+C stops it.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        return subprocess.run([sys.executable, str(ROOT / "server.py")], cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
