"""Names and version of the app, in one place for the code, the tray, the
installers (packaging/) and the update check."""

import os

NAME = "Onward"
SLUG = "onward"                 # file names, the Linux autostart entry, package ids
TAGLINE = "Fresh entry-level job postings, a tailored resume and an apply queue, on your own computer"
VERSION = "0.1.1"                       # bump with each release; tags are v<VERSION>

# Where the helper serves the dashboard. The extension talks to 8765;
# JOBRADAR_PORT is only for tests next to a running copy.
HOST, PORT = "127.0.0.1", int(os.environ.get("JOBRADAR_PORT") or 8765)
DASHBOARD_URL = f"http://{HOST}:{PORT}/"
BUNDLE_ID = "io.github.onward"    # macOS bundle and LaunchAgent label

# "owner/repo" on GitHub. Releases there are what the update check reads and
# what the installers are uploaded to. Empty turns the update check off.
GITHUB_REPO = "Samarthvg/Onward"
# The subject name on the Windows code-signing certificate (e.g. "Jane Doe").
# When set, a downloaded update installs only if Windows says it is signed
# by exactly this publisher.
WINDOWS_PUBLISHER = ""

# Store listings of the Autofill extension, once they exist. Until then the
# setup steps offer the signed .xpi shipped with the app (Firefox,
# packaging/extension.py) and Load unpacked (Chrome).
FIREFOX_ADDON_URL = ""
CHROME_STORE_URL = ""
