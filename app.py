"""It gets easier, the app: the dashboard's helper, timed job searches and a
tray icon, in one process that starts when the user logs in.

    python app.py              tray icon (the installed app runs this)
    python app.py --headless   no tray icon: helper and timed searches only
    <app> --radar [...]        one radar.py run (how the packaged app runs them)

The tray shows the last and next search, errors, which AI login works, and
opens the dashboard. It replaces the two Windows scheduled tasks the project
used before ("JobRadar" and "JobRadar Helper"), which it removes on start.
Everything stays on this computer; the only requests the app makes itself
are to job boards (radar.py) and, once a day, to GitHub for updates.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def hide_child_consoles() -> None:
    """A windowless app on Windows would open a console window for every
    program it runs (radar, TeX, the AI command-line tools). New processes
    get CREATE_NO_WINDOW unless the caller asked for something else."""
    if sys.platform != "win32" or sys.stdout is not None and sys.stdout.isatty():
        return

    class Popen(subprocess.Popen):
        def __init__(self, *args, **kw):
            if not kw.get("creationflags"):
                kw["creationflags"] = CREATE_NO_WINDOW
            super().__init__(*args, **kw)

    subprocess.Popen = Popen


def fix_path() -> None:
    """An app started at login doesn't see the PATH a terminal has, so the
    codex and claude logins (and TeX) wouldn't be found. Add the usual
    places, and on macOS and Linux the login shell's own PATH."""
    home = Path.home()
    extra: list[str] = []
    if sys.platform == "win32":
        extra += [os.path.join(os.environ.get("APPDATA", ""), "npm"), str(home / ".local" / "bin")]
    else:
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            out = subprocess.run([shell, "-ilc", 'printf "%s" "$PATH"'], capture_output=True, text=True,
                                 timeout=5, stdin=subprocess.DEVNULL).stdout.strip().splitlines()
            extra += out[-1].split(os.pathsep) if out else []
        except (OSError, subprocess.TimeoutExpired):
            pass
        extra += [str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", str(home / ".npm-global" / "bin")]
    have = os.environ.get("PATH", "").split(os.pathsep)
    # Folders that don't exist yet stay in: a login set up while the app runs installs there.
    add = [p for p in dict.fromkeys(extra) if p and p not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join(have + add)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)      # noqa: S606 - the user's own data folder
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def wait_for_port(host: str, port: int, seconds: float) -> None:
    """After stopping the old helper, give Windows a moment to free its port."""
    import socket
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex((host, port)) != 0:
                return
        time.sleep(0.5)


class App:
    def __init__(self, headless: bool) -> None:
        import appinfo
        import autostart
        import config
        import oslock

        self.appinfo, self.autostart_mod = appinfo, autostart
        self.store = config.store()
        self.headless = headless
        self.lock = oslock.FileLock(self.store.home / "app.lock")
        self.icon = None
        self.quit_event = threading.Event()
        self.backends: dict = {}
        self.update: dict | None = None
        self.update_busy = ""
        self.login = autostart.Autostart()
        self.server = None

    # ---------------------------------------------------------------- start

    def run(self) -> int:
        if not self.lock.acquire():
            # Already running (the user opened it again): show the dashboard instead.
            self.open_dashboard()
            return 0
        import autostart
        removed = autostart.remove_legacy_tasks(self.store.home)
        import server                       # after the old helper is gone: it reads the same data folder
        self.server = server
        server.log(f"app {self.appinfo.VERSION} starting" + (f"; removed old scheduled tasks: {', '.join(removed)}"
                                                             if removed else ""))
        server.prepare()
        if removed:
            wait_for_port(server.HOST, server.PORT, 15)
            every = self.store.section("radar")["interval_minutes"]
            self.store.update({"notices": self.store.section("notices") + [
                "It gets easier now runs as an app with a tray icon, so the old JobRadar scheduled tasks were "
                f"removed. It searches for jobs every {every} minutes while you're logged in."]}, strict=False)
        self.first_launch()
        if removed and not self.login.enabled():
            self.login.enable()             # the old helper task started at logon; keep that
        try:
            httpd = server.bind()
        except OSError as e:
            server.log(f"could not bind {server.HOST}:{server.PORT}: {e}")
            server.APP_STATE["port_conflict"] = (f"Another program is using port {server.PORT}, so the dashboard "
                                                 "can't open. Quit any other copy of It gets easier and start it again.")
            httpd = None
        if httpd:
            threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
        server.SCHEDULER.automatic = True
        server.SCHEDULER.on_change = self.refresh
        server.start_background()
        server.APP_STATE["autostart"] = self.login.enabled()
        threading.Thread(target=self._background, name="app-status", daemon=True).start()
        if httpd and not self.store.section("setup")["done"]:
            self.open_dashboard()           # a new install opens on its setup steps
        try:
            if self.headless or not self.run_tray():
                self.quit_event.wait()
        except KeyboardInterrupt:
            pass
        server.SCHEDULER.stop()
        server.log("app stopped")
        return 0

    def first_launch(self) -> None:
        """The installed app turns on Start at login the first time it runs;
        after that the tray's checkbox decides."""
        flag = self.store.home / ".autostart-set"
        if getattr(sys, "frozen", False) and not flag.exists():
            try:
                self.login.enable()
                flag.write_text(time.strftime("%Y-%m-%d"), encoding="utf-8")
            except OSError as e:
                self.server.log(f"could not turn on start at login: {e}")

    def _background(self) -> None:
        """AI login state every minute (each check runs a program) and the
        update check once a day."""
        import tailor
        import updates
        last_update_check = 0.0
        while not self.quit_event.is_set():
            try:
                self.backends = tailor.backend_status()
            except Exception as e:
                self.server.log(f"app: login check failed: {e}")
            app = self.store.section("app")
            if app["check_updates"] and self.appinfo.GITHUB_REPO and time.time() - last_update_check > 86400:
                last_update_check = time.time()
                try:
                    found = updates.check()
                    self.update = found if found and found["version"] != app["skip_version"] else None
                    self.server.APP_STATE["update"] = self.update
                    if self.update:
                        self.notify(f"{self.appinfo.NAME} {self.update['version']} is available.")
                except Exception as e:      # offline, rate limited: try again tomorrow
                    self.server.log(f"app: update check failed: {e}")
            self.refresh()
            self.quit_event.wait(60)

    # ---------------------------------------------------------------- actions

    def url(self) -> str:
        from server import HOST, PORT
        return f"http://{HOST}:{PORT}/"

    def open_dashboard(self, *_) -> None:
        webbrowser.open(self.url())

    def search_now(self, *_) -> None:
        if not self.server.SCHEDULER.run_now("tray"):
            self.notify("A search is already running.")

    def toggle_login(self, *_) -> None:
        try:
            self.login.disable() if self.login.enabled() else self.login.enable()
        except OSError as e:
            self.notify(f"Couldn't change Start at login: {e}")
        self.server.APP_STATE["autostart"] = self.login.enabled()
        self.refresh()

    def install_update(self, *_) -> None:
        import updates
        up = self.update
        if not up:
            return
        if sys.platform != "win32" or not up.get("installer"):
            webbrowser.open(up["page"])
            return
        if self.update_busy:
            return

        def work():
            self.update_busy = "Downloading the update…"
            self.refresh()
            try:
                path = updates.download(up)
                updates.check_signature(path)
                updates.run_installer(path)
                self.quit()                 # the installer replaces the app and starts the new one
            except Exception as e:
                self.server.log(f"app: update failed: {e}")
                self.notify(str(e))
            finally:
                self.update_busy = ""
                self.refresh()
        threading.Thread(target=work, name="update", daemon=True).start()

    def skip_update(self, *_) -> None:
        if self.update:
            self.store.update({"app": {"skip_version": self.update["version"]}}, strict=False)
            self.update = None
            self.server.APP_STATE["update"] = None
            self.refresh()

    def quit(self, *_) -> None:
        self.quit_event.set()
        if self.icon:
            self.icon.stop()

    def notify(self, text: str) -> None:
        if self.icon:
            try:
                self.icon.notify(text, self.appinfo.NAME)
            except Exception:
                pass

    # ---------------------------------------------------------------- tray

    def status_lines(self) -> list[str]:
        """The menu's status lines, also used by tests and --headless logs."""
        return status_lines(self.server.SCHEDULER.status(), self.backends, self.server.APP_STATE)

    def run_tray(self) -> bool:
        """Runs the tray icon until Quit. False when this system has no tray."""
        try:
            import pystray
            from PIL import Image
        except ImportError:
            self.server.log("app: no tray icon (pystray isn't installed); running without one")
            return False
        self.base_image = Image.open(Path(__file__).resolve().parent / "extension" / "icons" / "icon-128.png")
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR

        def line(i):
            return item(lambda _: (self.status_lines() + [""] * 5)[i], None, enabled=False,
                        visible=lambda _: len(self.status_lines()) > i)

        menu = pystray.Menu(
            item("Open dashboard", self.open_dashboard, default=True),
            sep, *(line(i) for i in range(5)), sep,
            item("Search for jobs now", self.search_now, enabled=lambda _: not self.server.SCHEDULER.run),
            item("Start at login", self.toggle_login, checked=lambda _: bool(self.server.APP_STATE.get("autostart"))),
            item("Open data folder", lambda *_: open_folder(self.store.home)),
            item(lambda _: self.update_busy or (f"Install update {self.update['version']}" if sys.platform == "win32"
                                                and self.update and self.update.get("installer")
                                                else f"Download update {self.update['version']}" if self.update else ""),
                 self.install_update, visible=lambda _: bool(self.update)),
            item("Don't offer this update again", self.skip_update, visible=lambda _: bool(self.update)),
            sep,
            item(f"Quit {self.appinfo.NAME}", self.quit),
        )
        self.icon = pystray.Icon(self.appinfo.SLUG, self.image(), self.appinfo.NAME, menu)
        try:
            self.icon.run(setup=lambda icon: setattr(icon, "visible", True))
        except Exception as e:      # no tray on this desktop (some Linux sessions)
            self.server.log(f"app: tray icon unavailable ({e}); running without one")
            self.icon = None
            return False
        return True

    def image(self):
        """The icon, with a dot: amber while searching, red after an error."""
        from PIL import ImageDraw
        img = self.base_image.copy().convert("RGBA")
        st = self.server.SCHEDULER.status()
        color = "#f2a900" if st["running"] else "#d93025" if (st["last"] or {}).get("outcome") == "error" \
            or self.server.APP_STATE.get("port_conflict") else None
        if color:
            d = ImageDraw.Draw(img)
            w = img.width
            d.ellipse((w * 0.58, w * 0.58, w * 0.98, w * 0.98), fill=color, outline="white", width=max(2, w // 32))
        return img

    def refresh(self) -> None:
        if not self.icon:
            return
        try:
            self.icon.icon = self.image()
            self.icon.title = " · ".join([self.appinfo.NAME, *self.status_lines()[:2]])[:127]
            self.icon.update_menu()
        except Exception:
            pass


def _clock(t: float) -> str:
    lt = time.localtime(t)
    today = time.localtime()
    hm = time.strftime("%I:%M %p", lt).lstrip("0")
    return hm if lt.tm_yday == today.tm_yday and lt.tm_year == today.tm_year else time.strftime("%b %d, ", lt) + hm


BACKEND_NAMES = {"chatgpt": "ChatGPT", "claude_code": "Claude", "claude_api": "Claude API key",
                 "openai_api": "OpenAI API key"}


def status_lines(st: dict, backends: dict, app_state: dict) -> list[str]:
    out = []
    if app_state.get("port_conflict"):
        out.append("Dashboard can't open: port in use")
    run, last = st.get("running"), st.get("last") or {}
    if run:
        done, total = run.get("done"), run.get("total")
        out.append(f"Searching: {done} of {total} boards" if total else f"Searching: {run['label'].lower()}")
    elif last.get("outcome") == "error":
        out.append(f"Last search failed at {_clock(last['finished'])}")
        out.append((last.get("error") or "")[:70])
    elif last.get("finished"):
        bits = [f"{last['matched']} jobs" if last.get("matched") is not None else "",
                f"{last['new']} new" if last.get("new") else ""]
        out.append(f"Last search {_clock(last['finished'])}" + (": " + ", ".join(b for b in bits if b) if any(bits) else ""))
    else:
        out.append("No search yet")
    if not st.get("setup_done"):
        out.append("Searches start once setup is done")
    elif st.get("next_run") and not run:
        out.append(f"Next search {_clock(st['next_run'])}")
    ok = [BACKEND_NAMES.get(k, k) for k, v in (backends or {}).items() if v is True]
    if backends:
        out.append("AI: " + (", ".join(ok) + " ready" if ok else "not signed in (dashboard, Settings)"))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--radar"]:
        import radar
        return radar.run(argv[1:])
    hide_child_consoles()
    fix_path()
    return App(headless="--headless" in argv).run()


if __name__ == "__main__":
    sys.exit(main())
