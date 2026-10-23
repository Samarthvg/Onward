"""Packages the Autofill extension for release.

    python packaging/extension.py
    python packaging/extension.py --check <previous tag>   fail if the extension changed without a version bump

  dist/onward-autofill-<v>.xpi        Firefox, signed by Mozilla (unlisted, self-distributed)
  dist/onward-autofill-<v>-chrome.zip  for uploading to the Chrome Web Store
  dist/updates.json                    what Firefox reads to update the installed add-on

The signed .xpi is also copied into extension/web-ext-artifacts/, which the
app's installers bundle, so the setup steps can install it in one click.

Signing needs the owner's addons.mozilla.org API keys in AMO_JWT_ISSUER and
AMO_JWT_SECRET (packaging/README.md). Mozilla signs each version number
once, so a version that an earlier release already signed is reused from
that release (REUSE_XPI, a path, set by the release workflow) instead of
being sent again. Without keys or a reusable file, only the Chrome zip and
no .xpi are made, and the setup steps fall back to Load Temporary Add-on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
DIST = ROOT / "dist"
ARTIFACTS = EXT / "web-ext-artifacts"
sys.path.insert(0, str(ROOT))
import appinfo  # noqa: E402

IGNORE = ["test", "test/**", "web-ext-artifacts", "web-ext-artifacts/**", "README.md"]


def manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))


def names(version: str) -> tuple[str, str]:
    base = f"{appinfo.SLUG}-autofill-{version}"
    return base + ".xpi", base + "-chrome.zip"


def web_ext(*args: str) -> None:
    """web-ext from extension/test's node_modules (npm ci there first)."""
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx not found: install Node.js, then run npm ci in extension/test")
    cmd = [npx, "web-ext", *args, "--source-dir", str(EXT), *[a for i in IGNORE for a in ("--ignore-files", i)]]
    print("+ web-ext", args[0], flush=True)
    subprocess.run(cmd, cwd=EXT / "test", check=True)


def chrome_zip(version: str) -> Path:
    out = DIST / names(version)[1]
    with tempfile.TemporaryDirectory() as tmp:
        web_ext("build", "--artifacts-dir", tmp, "--filename", out.name, "--overwrite-dest")
        shutil.move(str(Path(tmp) / out.name), out)
    return out


def sign(version: str) -> Path | None:
    out = DIST / names(version)[0]
    reuse = os.environ.get("REUSE_XPI")
    if reuse and Path(reuse).is_file():
        print(f"reusing the signed {Path(reuse).name} from an earlier release")
        shutil.copyfile(reuse, out)
        return out
    key, secret = os.environ.get("AMO_JWT_ISSUER"), os.environ.get("AMO_JWT_SECRET")
    if not (key and secret):
        print("AMO_JWT_ISSUER / AMO_JWT_SECRET not set: no signed .xpi this time")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        # The keys go through the environment, so they never show in a command line.
        os.environ.update(WEB_EXT_API_KEY=key, WEB_EXT_API_SECRET=secret)
        try:
            web_ext("sign", "--channel", "unlisted", "--artifacts-dir", tmp, "--approval-timeout", "900000")
        except subprocess.CalledProcessError:
            # A run that lost its connection while waiting for approval leaves the
            # version uploaded but not downloaded, and Mozilla refuses the same
            # version twice; fetch what it signed instead.
            ext_id = manifest()["browser_specific_settings"]["gecko"]["id"]
            print(f"web-ext sign failed; asking addons.mozilla.org for {version}", flush=True)
            if not fetch_signed(ext_id, version, key, secret, out):
                raise
            return out
        signed = sorted(Path(tmp).glob("*.xpi"))
        if not signed:
            raise SystemExit("web-ext sign returned no .xpi")
        shutil.move(str(signed[-1]), out)
    return out


AMO = "https://addons.mozilla.org/api/v5"


def amo_token(key: str, secret: str) -> str:
    """The short-lived JWT addons.mozilla.org's API takes (HS256 over the issuer and secret)."""
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    now = int(time.time())
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"iss": key, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}).encode())
    sig = b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def amo_get(url: str, key: str, secret: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": "JWT " + amo_token(key, secret), "User-Agent": "onward-release"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_signed(ext_id: str, version: str, key: str, secret: str, out: Path,
                 wait: float = 900, every: float = 15) -> bool:
    """Downloads the signed file of a version already on addons.mozilla.org,
    waiting for its signing to finish. False when there's no such version."""
    listing = f"{AMO}/addons/addon/{urllib.parse.quote(ext_id, safe='')}/versions/?filter=all_with_unlisted&page_size=50"
    end = time.time() + wait
    while True:
        try:
            found = next((v for v in json.loads(amo_get(listing, key, secret)).get("results", [])
                          if v.get("version") == version), None)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"addons.mozilla.org didn't answer ({e}); trying again", flush=True)
            found = {}
        if found is None:
            return False
        f = (found or {}).get("file") or {}
        if f.get("status") == "public" and f.get("url"):
            out.write_bytes(amo_get(f["url"], key, secret))
            print(f"downloaded the signed {version} from addons.mozilla.org", flush=True)
            return True
        if time.time() > end:
            raise SystemExit(f"addons.mozilla.org hasn't finished signing {version} (status {f.get('status')!r}); run the release again later")
        time.sleep(every)


def updates_json(version: str, xpi: Path, ext_id: str, repo: str) -> Path:
    """Firefox's update manifest, served from the latest release."""
    link = f"https://github.com/{repo}/releases/download/v{appinfo.VERSION}/{xpi.name}"
    data = {"addons": {ext_id: {"updates": [{
        "version": version, "update_link": link,
        "update_hash": "sha256:" + hashlib.sha256(xpi.read_bytes()).hexdigest()}]}}}
    out = DIST / "updates.json"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return out


def unbumped(prev_tag: str) -> str | None:
    """A problem when the extension changed since prev_tag but kept its
    version: Mozilla won't sign a version number twice."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    old = git("show", f"{prev_tag}:extension/manifest.json")
    if old.returncode != 0:
        return None                         # no extension in that release
    changed = git("diff", "--quiet", prev_tag, "HEAD", "--", "extension",
                  ":(exclude)extension/test", ":(exclude)extension/README.md").returncode != 0
    version = manifest()["version"]
    if changed and json.loads(old.stdout)["version"] == version:
        return (f"The extension changed since {prev_tag} but is still version {version}. "
                "Bump \"version\" in extension/manifest.json.")
    return None


def main() -> int:
    if sys.argv[1:2] == ["--check"]:
        problem = unbumped(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
        print(problem or "extension version ok")
        return 1 if problem else 0
    m = manifest()
    version, ext_id = m["version"], m["browser_specific_settings"]["gecko"]["id"]
    DIST.mkdir(exist_ok=True)
    made = [chrome_zip(version)]
    xpi = sign(version)
    if xpi:
        ARTIFACTS.mkdir(exist_ok=True)
        for old in ARTIFACTS.glob("*.xpi"):
            old.unlink()
        shutil.copyfile(xpi, ARTIFACTS / xpi.name)
        made.append(xpi)
        if appinfo.GITHUB_REPO:
            made.append(updates_json(version, xpi, ext_id, appinfo.GITHUB_REPO))
    for f in made:
        print("made", f.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
