#!/usr/bin/env python3
"""wigle-to-wdgwars: push WiGLE-1.6 CSVs to the WDGoWars wardriving leaderboard.

WDGoWars (https://wdgwars.pl/) is a community wardriving leaderboard / game.
This tool takes any WiGLE-format CSV (Wi-Fi + BLE observations with GPS) and
posts it to the WDGoWars ingest endpoint. It also supports pushing aircraft
records to the signed JSON endpoint when given an aircraft JSON file.

Auth: header `X-API-Key: <key>`. Bearer auth is rejected by the server.

The key is read from (in order):
    1. --key CLI flag
    2. $WDGWARS_API_KEY environment variable
    3. ~/.config/wigle-to-wdgwars/wdgwars.key  (mode 600 recommended)

Endpoints touched:
    GET  /api/me           : validate key, read stats/badges/gang
    POST /api/upload-csv   : bulk Wi-Fi/BLE ingest, multipart/form-data
    POST /api/upload/      : signed JSON ingest (aircraft, mesh, etc.)

Quickstart:
    # Validate your key
    python3 wigle_to_wdgwars.py --whoami

    # Push a WiGLE CSV (let the tool chunk it under the Cloudflare 524 cap)
    python3 wigle_to_wdgwars.py wardrive-2026-05-23.csv --chunk-size 10000

    # Push aircraft JSON to the signed endpoint
    python3 wigle_to_wdgwars.py --aircraft-json aircraft.json

See README.md for the full WDGoWars API reference, cron recipes, and a
walkthrough for producing WiGLE CSVs from common capture stacks (WiGLE
Android app, Kismet, hcxdumptool).
"""
from __future__ import annotations

__version__ = "1.4.0"
GITHUB_REPO = "HiroAlleyCat/wigle-to-wdgwars"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"

import argparse
import collections
import gzip
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import gungnir

_SSL_CTX = ssl.create_default_context()

# ───────────────────────────── Endpoints ─────────────────────────────────────

ENDPOINT = "https://wdgwars.pl/api/upload-csv"
SIGNED_ENDPOINT = gungnir.DEFAULT_API_URL  # https://wdgwars.pl/api/upload/
ME_ENDPOINT = gungnir.ME_API_URL

# WiGLE: pull your own uploaded observations back out as CSV.
# Auth is HTTP Basic with the pre-encoded token from https://wigle.net/account
# ("Encoded for use", used verbatim after "Basic "). Contract mirrors the
# community tool joelkoen/wigledl.
WIGLE_TRANSACTIONS = "https://api.wigle.net/api/v2/file/transactions"
WIGLE_CSV = "https://api.wigle.net/api/v2/file/csv/{transid}"

USER_AGENT = f"wigle-to-wdgwars/{__version__} (+{GITHUB_URL})"

# CLI tool — keep the v1.0 stderr-line-per-event behavior so cron logs
# look familiar. Library consumers can override.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

# Single Client for the process. Bundles per-tool identity so gungnir's
# whoami/send paths emit `wigle-to-wdgwars/1.1.0 (+...)` as the UA.
_client = gungnir.Client(
    tool="wigle-to-wdgwars",
    version=__version__,
    user_agent_extra=GITHUB_URL,
)

# ───────────────────────────── Config paths ──────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "wigle-to-wdgwars"
DEFAULT_KEY_FILE = CONFIG_DIR / "wdgwars.key"
WIGLE_KEY_FILE = CONFIG_DIR / "wigle.key"
COOLDOWN_FILE = CONFIG_DIR / "cooldown.json"
HWM_FILE = CONFIG_DIR / "hwm.json"

# ───────────────────────────── Self-update / version check ──────────────────
#
# Ported from Heimdall/Muninn for family parity. Same shape: a daily-cached
# GitHub releases probe + an in-place updater that prefers `git pull` when
# the script is in a checkout and falls back to fetching raw GitHub when it
# isn't. requirements.txt gets refreshed too so a future release that bumps
# a pinned dep self-heals without a wrapper-script revision.

def _check_for_update() -> str | None:
    """Quick non-blocking version check against the GitHub releases API.
    Cached for 24h in the user's config dir so we do not hammer the API.
    Returns the latest tag if newer than __version__, else None."""
    cache = CONFIG_DIR / "version-check.json"
    try:
        if cache.exists():
            blob = json.loads(cache.read_text())
            if time.time() - blob.get("checked_at", 0) < 86400:
                latest = blob.get("latest")
                return latest if latest and latest != __version__ else None
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"User-Agent": f"wigle-to-wdgwars/{__version__}"})
        with urllib.request.urlopen(req, timeout=3, context=_SSL_CTX) as r:
            data = json.loads(r.read())
            latest = (data.get("tag_name") or "").lstrip("v")
    except Exception:
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"checked_at": time.time(), "latest": latest}))
    except Exception:
        pass
    return latest if latest and latest != __version__ else None


def _run_update() -> int:
    """Update wigle-to-wdgwars in place. Uses `git pull` when this is a git
    checkout; otherwise downloads wigle_to_wdgwars.py from raw GitHub. Both
    paths refresh requirements.txt + re-run pip install, so dep bumps don't
    leave the user with an updated script importing a missing module."""
    script_dir = Path(__file__).resolve().parent
    git_dir = script_dir / ".git"
    if git_dir.exists():
        print(f"[wigle-to-wdgwars] updating via git pull in {script_dir}",
              file=sys.stderr)
        try:
            r = subprocess.run(
                ["git", "-C", str(script_dir), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30)
            print(r.stdout.strip(), file=sys.stderr)
            if r.returncode != 0:
                print(r.stderr.strip(), file=sys.stderr)
                return r.returncode
            _pip_install_requirements(script_dir)
            print(f"[wigle-to-wdgwars] now on v{__version__} (re-run with "
                  f"--version to confirm latest)", file=sys.stderr)
            return 0
        except FileNotFoundError:
            print("[wigle-to-wdgwars] git not found in PATH. Install git, or "
                  "download wigle_to_wdgwars.py manually.", file=sys.stderr)
            return 1
    return _update_from_raw(script_dir)


def _fetch_raw(path: str, dest: Path) -> bool:
    """Fetch a file from the repo's main branch to dest atomically.
    Returns True on success, False on failure (logs the reason)."""
    raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{path}"
    print(f"[wigle-to-wdgwars] fetching {path} from {raw_url}", file=sys.stderr)
    try:
        req = urllib.request.Request(raw_url, headers={
            "User-Agent": f"wigle-to-wdgwars/{__version__}"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            body = r.read()
    except Exception as e:
        print(f"[wigle-to-wdgwars] download of {path} failed: {e}",
              file=sys.stderr)
        return False
    tmp = dest.with_suffix(dest.suffix + ".new")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, dest)
    except OSError as e:
        print(f"[wigle-to-wdgwars] couldn't write {dest}: {e}",
              file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _pip_install_requirements(script_dir: Path) -> None:
    """Best-effort `python -m pip install -r requirements.txt` against the
    interpreter currently running wigle-to-wdgwars. Never fails the caller —
    prints a clear hint if pip is missing or the install errors, so the
    update return code still reflects the script update itself."""
    req = script_dir / "requirements.txt"
    if not req.exists():
        return
    has_deps = any(
        line.strip() and not line.lstrip().startswith("#")
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines()
    )
    if not has_deps:
        return
    print(f"[wigle-to-wdgwars] installing/refreshing deps from {req.name} "
          f"(python -m pip install --upgrade -r requirements.txt)",
          file=sys.stderr)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                            "-r", str(req)], timeout=300)
    except FileNotFoundError:
        print("[wigle-to-wdgwars] python not found to invoke pip; run "
              "`python -m pip install -r requirements.txt` manually.",
              file=sys.stderr)
        return
    except subprocess.TimeoutExpired:
        print("[wigle-to-wdgwars] pip install timed out; run "
              "`python -m pip install -r requirements.txt` manually.",
              file=sys.stderr)
        return
    if r.returncode != 0:
        print(f"[wigle-to-wdgwars] pip install exited {r.returncode}; if "
              f"import errors below mention a missing module, run "
              f"`python -m pip install -r requirements.txt` manually.",
              file=sys.stderr)


def _update_from_raw(script_dir: Path) -> int:
    """Non-git fallback for --update: fetch wigle_to_wdgwars.py +
    requirements.txt from raw GitHub and replace the local files
    atomically, then refresh deps. Works for ZIP-downloaded installs."""
    target = script_dir / "wigle_to_wdgwars.py"
    raw_url = (f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/"
               f"wigle_to_wdgwars.py")
    print(f"[wigle-to-wdgwars] not a git checkout. Fetching latest "
          f"wigle_to_wdgwars.py from {raw_url}", file=sys.stderr)
    try:
        req = urllib.request.Request(raw_url, headers={
            "User-Agent": f"wigle-to-wdgwars/{__version__}"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            new_text = r.read().decode("utf-8")
    except Exception as e:
        print(f"[wigle-to-wdgwars] download failed: {e}", file=sys.stderr)
        print(f"[wigle-to-wdgwars] manual download: "
              f"https://github.com/{GITHUB_REPO}/releases/latest",
              file=sys.stderr)
        return 1
    try:
        import ast
        ast.parse(new_text)
    except SyntaxError as e:
        print(f"[wigle-to-wdgwars] downloaded file failed to parse, "
              f"aborting: {e}", file=sys.stderr)
        return 1
    import re as _re
    m = _re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                   new_text, _re.MULTILINE)
    new_version = m.group(1) if m else "?"
    if new_version == __version__:
        print(f"[wigle-to-wdgwars] already on the latest (v{__version__}). "
              f"Refreshing requirements.txt in case a pinned dep moved.",
              file=sys.stderr)
        _fetch_raw("requirements.txt", script_dir / "requirements.txt")
        _pip_install_requirements(script_dir)
        return 0
    tmp = target.with_suffix(".py.new")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as e:
        print(f"[wigle-to-wdgwars] couldn't write {target}: {e}",
              file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass
        return 1
    print(f"[wigle-to-wdgwars] updated v{__version__} to v{new_version}",
          file=sys.stderr)
    _fetch_raw("requirements.txt", script_dir / "requirements.txt")
    _pip_install_requirements(script_dir)
    print(f"[wigle-to-wdgwars] re-run wigle-to-wdgwars to pick up the new "
          f"code (the current process is still running the old version).",
          file=sys.stderr)
    return 0


# ───────────────────────────── Cooldown persistence ──────────────────────────

def _cooldown_check_and_sleep() -> None:
    """Respect a server cooldown set by a previous 429 response.

    Delegates to gungnir.cooldown. Persists across invocations so a cron
    job running every N minutes does not hammer the server while a
    queued upload is still being processed.

    Note: gungnir uses its OWN config dir convention for the cooldown
    file (`<config_dir>/cooldown.json` for `tool="wigle-to-wdgwars"`).
    On POSIX this is `~/.config/wigle-to-wdgwars/cooldown.json` —
    byte-identical path to v1.0. On Windows this moves to
    `%APPDATA%/wigle-to-wdgwars/cooldown.json` — different from v1.0
    but cooldown state is ephemeral, so the migration is harmless.
    """
    gungnir.cooldown.check_and_sleep("wigle-to-wdgwars")


def _cooldown_record(seconds: float) -> None:
    gungnir.cooldown.record("wigle-to-wdgwars", seconds)


# ───────────────────────────── HWM tracking ──────────────────────────────────

def _hwm_record(payload: dict) -> None:
    """Persist last-successful-upload watermark for visibility / monitoring.

    Delegates to gungnir.hwm. Same config-dir caveat as
    :func:`_cooldown_check_and_sleep`."""
    gungnir.hwm.record("wigle-to-wdgwars", payload)


# ───────────────────────────── Key loading ───────────────────────────────────

def load_key(cli_key: str | None) -> str:
    """Resolve the API key per the documented precedence."""
    if cli_key:
        return cli_key.strip()
    env_key = os.environ.get("WDGWARS_API_KEY")
    if env_key:
        return env_key.strip()
    if DEFAULT_KEY_FILE.exists():
        return DEFAULT_KEY_FILE.read_text().strip()
    sys.exit(
        f"no API key: pass --key, set WDGWARS_API_KEY, or create {DEFAULT_KEY_FILE}\n"
        f"(mkdir -p {CONFIG_DIR} && echo YOUR_KEY > {DEFAULT_KEY_FILE} && chmod 600 {DEFAULT_KEY_FILE})"
    )


def load_wigle_token(cli_token: str | None) -> str:
    """Resolve the WiGLE API token (the pre-encoded one from your account page).

    Precedence: --wigle-key, then $WIGLE_API_KEY, then ~/.config/wigle-to-wdgwars/wigle.key.
    """
    if cli_token:
        return cli_token.strip()
    env = os.environ.get("WIGLE_API_KEY")
    if env:
        return env.strip()
    if WIGLE_KEY_FILE.exists():
        return WIGLE_KEY_FILE.read_text().strip()
    sys.exit(
        "no WiGLE token: pass --wigle-key, set WIGLE_API_KEY, or create "
        f"{WIGLE_KEY_FILE}\nGet the 'Encoded for use' token from https://wigle.net/account"
    )


# ───────────────────────────── Key saving (mode 600) ─────────────────────────

def _write_secret_file(path: Path, value: str) -> None:
    """Write `value` to `path` with mode 600 and a trailing newline.

    Refuses to write through a symlink (avoids the classic dotfile-symlink
    redirect-attack vector when the config dir is writable by another user).
    chmod is best-effort on Windows, where the bit may not stick — the
    config dir lives under the user's profile anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        sys.exit(f"refusing to write through symlink: {path}\n"
                 f"remove the symlink and re-run.")
    # Create with restrictive mode from the start to avoid a race where
    # the file exists briefly at the umask default.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value.strip() + "\n")
    except Exception:
        if path.exists():
            path.unlink()
        raise
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def save_key(key: str) -> None:
    """Save the WDGoWars API key to the user config dir (mode 600)."""
    _write_secret_file(DEFAULT_KEY_FILE, key.strip())
    print(f"[wigle-to-wdgwars] saved WDGoWars API key to {DEFAULT_KEY_FILE}",
          file=sys.stderr)
    print(f"[wigle-to-wdgwars] (file mode 600 — only your user can read it)",
          file=sys.stderr)


def save_wigle_token(token: str) -> None:
    """Save the WiGLE 'Encoded for use' token to the user config dir (mode 600)."""
    _write_secret_file(WIGLE_KEY_FILE, token.strip())
    print(f"[wigle-to-wdgwars] saved WiGLE token to {WIGLE_KEY_FILE}",
          file=sys.stderr)
    print(f"[wigle-to-wdgwars] (file mode 600 — only your user can read it)",
          file=sys.stderr)


# ───────────────────────────── Key validation ────────────────────────────────

def check_whoami(key: str) -> int:
    """GET /api/me to validate a WDGoWars key. Returns 0 on success, 1 on fail.

    Read-only — safe to call during setup without touching the upload
    queue or the user's leaderboard counts.
    """
    return _client.whoami(key)


def check_wigle_token(token: str, timeout: float = 30) -> int:
    """Validate a WiGLE 'Encoded for use' token by listing one transaction.

    Hits /api/v2/file/transactions?pagestart=0&pageend=1. Returns 0 on
    HTTP 200, 1 otherwise. Read-only — no uploads, no state changes.
    """
    try:
        status, body = _wigle_get(
            f"{WIGLE_TRANSACTIONS}?pagestart=0&pageend=1", token,
            timeout=timeout,
        )
    except urllib.error.URLError as e:
        print(f"[wigle] network error validating token: {e}", file=sys.stderr)
        return 1
    if status == 200:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            print("[wigle] transactions response was not JSON", file=sys.stderr)
            return 1
        if not payload.get("success", True):
            # WiGLE sometimes returns 200 with success=false on bad token
            print(f"[wigle] token rejected: "
                  f"{payload.get('message', 'no message')}", file=sys.stderr)
            return 1
        print("[wigle] token OK", file=sys.stderr)
        return 0
    if status == 401:
        print("[wigle] HTTP 401: token rejected. Use the 'Encoded for use' "
              "string from https://wigle.net/account", file=sys.stderr)
        return 1
    print(f"[wigle] unexpected HTTP {status} validating token: "
          f"{body[:200].decode('utf-8', 'replace')}", file=sys.stderr)
    return 1


# ───────────────────────────── Interactive prompts ───────────────────────────

def _prompt_yes_no(question: str, default: bool = True) -> bool:
    """Ask a y/n question on stderr. Returns True for yes, False for no.

    On EOF / Ctrl+C, returns the default so non-interactive runs don't hang.
    Always emits a newline after the answer so the next section header
    doesn't collide with the prompt line when stdin is piped (interactive
    TTY input gets its own newline from the terminal — but piped input
    doesn't, leaving section headers glued onto the prompt)."""
    suffix = " [Y/n] " if default else " [y/N] "
    piped = not sys.stdin.isatty()
    while True:
        try:
            print(question + suffix, end="", flush=True, file=sys.stderr)
            line = sys.stdin.readline()
            if not line:
                print("", file=sys.stderr)
                return default
            ans = line.strip().lower()
            if piped:
                print("", file=sys.stderr)
        except (KeyboardInterrupt, EOFError):
            print("", file=sys.stderr)
            return default
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print(" (please answer y or n)", file=sys.stderr)


def _prompt_str(label: str, default: str = "") -> str:
    """Ask for a free-text value; return default on empty input."""
    if default:
        suffix = f" [{default}]: "
    else:
        suffix = ": "
    try:
        ans = input(label + suffix).strip()
    except (KeyboardInterrupt, EOFError):
        print("", file=sys.stderr)
        return default
    return ans or default


def _prompt_secret(label: str) -> str:
    """Read a secret value. Hidden via getpass on a TTY, plain input on a pipe.

    The plain-input fallback exists so non-interactive setup (echo'd from
    a wrapper script or piped from a CI runner) still works without
    hanging on a non-existent TTY."""
    if sys.stdin.isatty():
        import getpass
        try:
            return getpass.getpass(label).strip()
        except (KeyboardInterrupt, EOFError):
            print("", file=sys.stderr)
            return ""
    print(label, end="", flush=True, file=sys.stderr)
    try:
        return sys.stdin.readline().strip()
    except (KeyboardInterrupt, EOFError):
        return ""


def _prompt_int(label: str, default: int, *, min_val: int = 0,
                max_val: int = 1_000_000) -> int:
    while True:
        ans = _prompt_str(label, str(default))
        try:
            n = int(ans)
        except (TypeError, ValueError):
            print(f" enter a whole number between {min_val} and {max_val}",
                  file=sys.stderr)
            continue
        if min_val <= n <= max_val:
            return n
        print(f" enter a whole number between {min_val} and {max_val}",
              file=sys.stderr)


# ───────────────────────────── Setup wizard ──────────────────────────────────

def interactive_setup() -> int:
    """First-run setup. Prompts for both API keys, validates each, saves.

    Order: WDGoWars first (mandatory — without it, nothing uploads),
    WiGLE second (optional — only needed for --from-wigle pull mode).
    Then offers to install a daily timer. Each step is independently
    skippable; cancelling later steps does not undo earlier ones.

    Returns 0 on a clean run-through (including user-declined steps),
    1 only if the user actively cancelled mid-prompt without saving
    anything.
    """
    print("", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print(" wigle-to-wdgwars — first-time setup", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print("", file=sys.stderr)
    print(" This walks you through saving your two API keys and (optionally)", file=sys.stderr)
    print(" installing a recurring timer that pushes your WiGLE uploads", file=sys.stderr)
    print(" to WDGoWars without further input.", file=sys.stderr)
    print("", file=sys.stderr)
    print(f" Both keys are stored under: {CONFIG_DIR}", file=sys.stderr)
    print(" Each file is written with mode 600 (only your user can read it).", file=sys.stderr)
    print("", file=sys.stderr)

    saved_wdg = _setup_wdgwars_key()
    if saved_wdg < 0:
        return 1

    saved_wigle = _setup_wigle_token()
    if saved_wigle < 0:
        return 1

    if saved_wdg == 0:
        # WDGoWars key didn't get saved (declined or missing) — no point
        # offering a timer that can't upload.
        print("", file=sys.stderr)
        print(" Skipping schedule setup — no WDGoWars key saved.", file=sys.stderr)
        print(" Re-run --setup once you have one.", file=sys.stderr)
        return 0

    try:
        interactive_schedule_setup(have_wigle=bool(saved_wigle))
    except (KeyboardInterrupt, EOFError):
        print("\n[wigle-to-wdgwars] schedule setup skipped", file=sys.stderr)
    except Exception as e:
        # Don't let a scheduler hiccup discard the keys we already saved.
        print(f"\n[wigle-to-wdgwars] schedule setup error (skipped): {e}",
              file=sys.stderr)
    return 0


def _setup_wdgwars_key() -> int:
    """Sub-step: WDGoWars key. Returns 1 saved, 0 declined/skipped, -1 cancel."""
    print(" ── WDGoWars API key ─────────────────────────────────────────",
          file=sys.stderr)
    print(" Required for uploads. Get it from:", file=sys.stderr)
    print("   https://wdgwars.pl/account   →  Settings  →  API Key",
          file=sys.stderr)
    print("", file=sys.stderr)

    if DEFAULT_KEY_FILE.exists():
        print(f" A WDGoWars key is already saved at {DEFAULT_KEY_FILE}.",
              file=sys.stderr)
        if not _prompt_yes_no(" Replace it?", default=False):
            return 1  # treat existing key as "saved"

    if not _prompt_yes_no(" Save a WDGoWars API key now?", default=True):
        return 0

    while True:
        key = _prompt_secret(" Paste your WDGoWars API key: ")
        if not key:
            print(" (empty input — try again, or Ctrl+C to cancel)",
                  file=sys.stderr)
            if not _prompt_yes_no(" Keep trying?", default=True):
                return -1
            continue
        print(" Validating key against wdgwars.pl/api/me ...", file=sys.stderr)
        rc = check_whoami(key)
        if rc != 0:
            print(" That key was rejected. Try again, or Ctrl+C to cancel.",
                  file=sys.stderr)
            if not _prompt_yes_no(" Keep trying?", default=True):
                return -1
            continue
        save_key(key)
        print("", file=sys.stderr)
        return 1


def _setup_wigle_token() -> int:
    """Sub-step: WiGLE token. Returns 1 saved, 0 declined/skipped, -1 cancel."""
    print(" ── WiGLE token (optional) ───────────────────────────────────",
          file=sys.stderr)
    print(" Only needed if you want --from-wigle (pulls your latest WiGLE",
          file=sys.stderr)
    print(" upload and pushes it to WDGoWars, no file needed).", file=sys.stderr)
    print("", file=sys.stderr)
    print(" Get the 'Encoded for use' string from:", file=sys.stderr)
    print("   https://wigle.net/account", file=sys.stderr)
    print("", file=sys.stderr)

    if WIGLE_KEY_FILE.exists():
        print(f" A WiGLE token is already saved at {WIGLE_KEY_FILE}.",
              file=sys.stderr)
        if not _prompt_yes_no(" Replace it?", default=False):
            return 1

    if not _prompt_yes_no(" Save a WiGLE token now?", default=True):
        return 0

    while True:
        token = _prompt_secret(" Paste your WiGLE 'Encoded for use' token: ")
        if not token:
            print(" (empty input — try again, or Ctrl+C to cancel)",
                  file=sys.stderr)
            if not _prompt_yes_no(" Keep trying?", default=True):
                return -1
            continue
        print(" Validating token against api.wigle.net ...", file=sys.stderr)
        rc = check_wigle_token(token)
        if rc != 0:
            print(" That token was rejected. Try again, or Ctrl+C to cancel.",
                  file=sys.stderr)
            if not _prompt_yes_no(" Keep trying?", default=True):
                return -1
            continue
        save_wigle_token(token)
        print("", file=sys.stderr)
        return 1


# ───────────────────────────── CSV reading ───────────────────────────────────

def _read_csv_bytes(csv_path: Path) -> bytes:
    """Read a WiGLE CSV, transparently decompressing if it is gzip.

    The WiGLE Android app's share/export produces a `.wiglecsv.gz` (a single
    gzip member, often with the inner file named with no extension). Detect
    the gzip magic bytes and decompress so users do not have to gunzip first.
    """
    data = csv_path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


# ───────────────────────────── Preview ──────────────────────────────────────

def preview_csv(csv_path: Path, n: int = 6) -> int:
    """Print the first n data rows of a WiGLE-1.6 CSV as JSON-lines to
    stdout, then exit. Mirrors Heimdall + Muninn --preview.

    WiGLE-1.6 format: line 1 is the WigleWifi banner, line 2 is the column
    header (the one we use as keys), data rows follow. Gzip is decompressed
    transparently so this works on both `.csv` and `.wiglecsv.gz`."""
    import csv as _csv
    try:
        raw = _read_csv_bytes(csv_path)
    except OSError as e:
        print(f"[wigle] could not read {csv_path}: {e}", file=sys.stderr)
        return 1
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) < 2:
        print(f"[wigle] {csv_path} is too short to be a WiGLE CSV "
              f"(need banner + header + at least one row)", file=sys.stderr)
        return 1
    header_line = lines[1]
    data_lines = lines[2:]
    if not data_lines:
        print(f"[wigle] {csv_path} has no data rows after the header",
              file=sys.stderr)
        return 1
    reader = _csv.DictReader(
        [header_line] + data_lines[:n],
        skipinitialspace=False,
    )
    count = 0
    for row in reader:
        print(json.dumps(row, ensure_ascii=False))
        count += 1
        if count >= n:
            break
    print(f"[wigle] preview: showed {count} of {len(data_lines)} data rows. "
          f"No upload performed.", file=sys.stderr)
    return 0


# ───────────────────────────── CSV upload path ───────────────────────────────

def _post_one(csv_bytes: bytes, filename: str, key: str, field: str) -> tuple[int, str, float]:
    """POST a single multipart CSV chunk. Returns (status, body_text, duration_s)."""
    boundary = f"----wdgwars{uuid.uuid4().hex}"
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{field}"; '
        f'filename="{filename}"\r\n'
    ).encode()
    body += b"Content-Type: text/csv\r\n\r\n"
    body += csv_bytes
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=bytes(body),
        method="POST",
        headers={
            "X-API-Key": key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.monotonic() - t0


def _split_bytes(csv_bytes: bytes, chunk_rows: int) -> list[bytes]:
    """Split WiGLE CSV bytes into N-row chunks, preserving the 2-line header on each.

    Chunking is the workaround for the Cloudflare 524 (origin timeout) the
    WDGoWars proxy hits when a synchronous import takes >120 s. 10k rows per
    chunk lands comfortably under that cap.
    """
    raw = csv_bytes.decode("utf-8").splitlines(keepends=False)
    if len(raw) < 3:
        return [csv_bytes]
    h1, h2, *data_rows = raw
    if not chunk_rows or chunk_rows >= len(data_rows):
        return [csv_bytes]
    chunks: list[bytes] = []
    for i in range(0, len(data_rows), chunk_rows):
        slice_rows = data_rows[i:i + chunk_rows]
        body = h1 + "\n" + h2 + "\n" + "\n".join(slice_rows) + "\n"
        chunks.append(body.encode("utf-8"))
    return chunks


def _split_csv(csv_path: Path, chunk_rows: int) -> list[bytes]:
    """Read a CSV (gzip-aware) and split into chunks. See _split_bytes."""
    return _split_bytes(_read_csv_bytes(csv_path), chunk_rows)


PAYLOAD_TOO_LARGE_ERROR = "payload-too-large"


def _halve_chunk(chunk_bytes: bytes) -> tuple[bytes, bytes] | None:
    """Bisect a WiGLE CSV chunk into two row-count halves, header preserved
    on each. Returns None if the chunk has fewer than 2 data rows (cannot
    bisect further). Used to react to LOCOSP's 15 MB upload cap (2026-06-05):
    on HTTP 413 the offending chunk is halved and both halves are retried.
    """
    raw = chunk_bytes.decode("utf-8").splitlines(keepends=False)
    if len(raw) < 4:
        return None
    h1, h2, *data_rows = raw
    mid = len(data_rows) // 2
    if mid < 1:
        return None
    left = (h1 + "\n" + h2 + "\n" + "\n".join(data_rows[:mid]) + "\n").encode("utf-8")
    right = (h1 + "\n" + h2 + "\n" + "\n".join(data_rows[mid:]) + "\n").encode("utf-8")
    return left, right


def _aggregate(payloads: list[dict]) -> dict:
    """Merge per-chunk response envelopes into one summary."""
    keys = ("imported", "captured", "updated", "duplicates", "no_gps", "bad_rows", "merged_samples")
    out: dict = {k: 0 for k in keys}
    last_total = None
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for k in keys:
            v = p.get(k)
            if isinstance(v, (int, float)):
                out[k] += int(v)
        if "total" in p:
            last_total = p["total"]
    out["ok"] = all(p.get("ok") for p in payloads if isinstance(p, dict))
    out["chunks"] = len(payloads)
    if last_total is not None:
        out["total"] = last_total
    return out


def _upload_chunks(chunks: list[bytes], name: str, key: str, field: str,
                   dry_run: bool, cooldown_sec: float) -> int:
    """POST pre-split CSV chunks to WDGoWars. Returns shell exit code (0 ok).

    Resilient to HTTP 413 from LOCOSP's 15 MB upload cap (2026-06-05): any
    chunk that comes back with `{error: payload-too-large, max_bytes, received}`
    is bisected and both halves are pushed back onto the work queue. Recursion
    bottoms out when a chunk is one row and still 413 (recorded as a failure,
    other chunks continue).
    """
    total_kb = sum(len(c) for c in chunks) / 1024
    print(
        f"[wdgwars] POST {ENDPOINT} field={field} file={name} "
        f"chunks={len(chunks)} total={total_kb:.1f} KB",
        file=sys.stderr,
    )
    if dry_run:
        print("[wdgwars] dry-run: not sending", file=sys.stderr)
        return 0
    queue: collections.deque[bytes] = collections.deque(chunks)
    payloads: list[dict] = []
    attempt = 0
    splits = 0
    while queue:
        attempt += 1
        body = queue.popleft()
        try:
            status, raw, dur = _post_one(body, name, key, field)
        except urllib.error.URLError as e:
            sys.exit(f"[wdgwars] network error on attempt {attempt}: {e}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"ok": False, "error": "non-json response", "raw": raw[:300]}

        if status == 413 and data.get("error") == PAYLOAD_TOO_LARGE_ERROR:
            halves = _halve_chunk(body)
            max_b = data.get("max_bytes")
            recv = data.get("received")
            if halves is None:
                print(
                    f"[wdgwars] attempt {attempt} HTTP 413 with 1 row, cannot "
                    f"bisect further (max_bytes={max_b} received={recv}). "
                    f"Recording as failure, continuing.",
                    file=sys.stderr,
                )
                payloads.append(data)
                continue
            left, right = halves
            queue.appendleft(right)
            queue.appendleft(left)
            splits += 1
            print(
                f"[wdgwars] attempt {attempt} HTTP 413 "
                f"(max_bytes={max_b} received={recv}); bisecting and retrying "
                f"({len(left) // 1024}+{len(right) // 1024} KB halves)",
                file=sys.stderr,
            )
            time.sleep(cooldown_sec)
            continue

        print(
            f"[wdgwars] attempt {attempt} HTTP {status} in {dur:.1f}s "
            f"imported={data.get('imported')} dup={data.get('duplicates')} "
            f"merged={data.get('merged_samples')} bad={data.get('bad_rows')}",
            file=sys.stderr,
        )
        payloads.append(data)
        if status == 200 and data.get('ok'):
            _hwm_record(data)
        if status == 429:
            wait = float(data.get("retry_after") or cooldown_sec * 4)
            print(f"[wdgwars] 429 cooldown, sleeping {wait:.0f}s", file=sys.stderr)
            _cooldown_record(wait)
            time.sleep(wait)
        elif queue:
            time.sleep(cooldown_sec)
    if splits:
        print(
            f"[wdgwars] auto-split {splits} chunk(s) on 413 over the run",
            file=sys.stderr,
        )
    if len(payloads) == 1:
        print(json.dumps(payloads[0]))
        return 0 if payloads[0].get("ok") else 1
    agg = _aggregate(payloads)
    print(json.dumps(agg))
    return 0 if agg.get("ok") else 1


def upload_csv_bytes(csv_bytes: bytes, name: str, key: str, field: str,
                     dry_run: bool, chunk_rows: int = 0, cooldown_sec: float = 5.0) -> int:
    """Upload WiGLE CSV bytes (e.g. pulled from WiGLE) to WDGoWars."""
    _cooldown_check_and_sleep()
    chunks = _split_bytes(csv_bytes, chunk_rows) if chunk_rows else [csv_bytes]
    return _upload_chunks(chunks, name, key, field, dry_run, cooldown_sec)


def upload_csv(csv_path: Path, key: str, field: str, dry_run: bool,
               chunk_rows: int = 0, cooldown_sec: float = 5.0) -> int:
    """Upload a WiGLE CSV file (gzip-aware). Returns shell exit code (0 ok, 1 error)."""
    if not csv_path.is_file():
        sys.exit(f"csv not found: {csv_path}")
    _cooldown_check_and_sleep()
    chunks = _split_csv(csv_path, chunk_rows) if chunk_rows else [_read_csv_bytes(csv_path)]
    return _upload_chunks(chunks, csv_path.name, key, field, dry_run, cooldown_sec)


# ───────────────────────────── Signed JSON path ──────────────────────────────

def _post_signed(payload: dict, key: str) -> tuple[int, dict, float]:
    """POST a signed JSON payload to /api/upload/.

    .. deprecated:: 1.1.0
        Kept as a thin backward-compat shim. New code should call
        :func:`gungnir.transport.send_chunk` directly with the
        appropriate slot kwarg (``aircraft=``, ``meshcore_nodes=``,
        etc.) instead of pre-building the payload dict.

    Returns (status, parsed_response, duration_s). Status is 200 on
    success, otherwise the underlying gungnir rc (1) — the exact HTTP
    code is no longer surfaced for non-2xx responses (gungnir handles
    them internally with retry/backoff/cooldown).
    """
    aircraft = payload.get("aircraft") or None
    networks = payload.get("networks") or None
    meshcore_nodes = payload.get("meshcore_nodes") or None
    t0 = time.monotonic()
    # Build the slot kwargs dict, dropping empties (gungnir wants exactly one).
    slot_kwargs: dict = {}
    for name, lst in (("aircraft", aircraft), ("networks", networks),
                      ("meshcore_nodes", meshcore_nodes)):
        if lst:
            slot_kwargs[name] = lst
            break  # first-non-empty wins; matches v1.0 behavior in practice
    if not slot_kwargs:
        # Empty payload — treat as success-noop to mirror v1.0 semantics
        return 200, {"ok": True, "imported": 0}, time.monotonic() - t0
    inner_payload = gungnir.build_payload(**slot_kwargs)
    rc, data = gungnir.transport.send_chunk(
        "wigle-to-wdgwars", __version__, SIGNED_ENDPOINT, key,
        inner_payload,
        sent_count=len(next(iter(slot_kwargs.values()))),
        user_agent_extra=GITHUB_URL,
    )
    status = 200 if rc == 0 else 1
    return status, data, time.monotonic() - t0


def upload_aircraft_json(aircraft_path: Path, key: str, dry_run: bool = False,
                          batch: int = 500) -> int:
    """Push a JSON file of aircraft records to the signed endpoint.

    Expected file format: a JSON list of dicts, each with at minimum
    `icao`, `lat`, `lon`, `first_seen` (and ideally callsign / alt_ft /
    speed_kt). See README.md for the full aircraft record schema.

    Behavior (inherited from gungnir 0.1.x):

    - Retry 5xx + network errors with exponential backoff.
    - 429 stops the whole batch and persists a cooldown the next cron
      tick respects.
    - Silent-drop pattern (HTTP 200 ok:true with every counter zero)
      now returns rc=1 instead of just logging. v1.0 had no detection.
    - Inter-chunk cooldown of 1s.
    - User-Agent now includes the repo URL per RFC bot-UA.

    Default ``batch`` dropped from 1000 to 500 per locosp's
    recommendation (100-500 range).
    """
    if not aircraft_path.is_file():
        sys.exit(f"aircraft json not found: {aircraft_path}")
    try:
        aircraft = json.loads(aircraft_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"aircraft json parse error: {e}")
    if not isinstance(aircraft, list):
        sys.exit("aircraft json must be a JSON list of records")
    if not aircraft:
        print("[wdgwars] aircraft: 0 records to upload", file=sys.stderr)
        return 0
    print(
        f"[wdgwars] POST {SIGNED_ENDPOINT} aircraft={len(aircraft)} batch={batch}",
        file=sys.stderr,
    )
    if dry_run:
        print("[wdgwars] dry-run: not sending aircraft", file=sys.stderr)
        return 0
    return _client.send(key, aircraft=aircraft, batch_size=batch)


# ───────────────────────────── /api/me ───────────────────────────────────────

def whoami(key: str) -> int:
    """GET /api/me. Logs username + account stats on success. Return 0
    on success, 1 on any failure.

    .. versionchanged:: 1.1.0
        Now delegates to gungnir. v1.0 printed the raw JSON to stdout;
        v1.1 logs a structured summary to stderr (matches the rest of
        the v1.1 logging output)."""
    return _client.whoami(key)


# ───────────────────────────── WiGLE pull path ───────────────────────────────

def _wigle_get(url: str, token: str, timeout: float = 120) -> tuple[int, bytes]:
    """GET a WiGLE API URL with HTTP Basic auth. Returns (status, body_bytes)."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/csv",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wigle_list_transactions(token: str, limit: int) -> list[str]:
    """Return up to `limit` of your most recent WiGLE upload transaction IDs.

    WiGLE returns newest first. Endpoint + field name (`transid`) follow the
    /api/v2/file/transactions contract used by joelkoen/wigledl.
    """
    out: list[str] = []
    page = 0
    while len(out) < limit:
        url = f"{WIGLE_TRANSACTIONS}?pagestart={page * 100}&pageend={(page + 1) * 100}"
        status, body = _wigle_get(url, token)
        if status == 401:
            sys.exit("[wigle] HTTP 401: bad token. Use the 'Encoded for use' "
                     "token from https://wigle.net/account")
        if status != 200:
            sys.exit(f"[wigle] transactions list failed: HTTP {status}: "
                     f"{body[:200].decode('utf-8', 'replace')}")
        try:
            results = json.loads(body).get("results", [])
        except json.JSONDecodeError:
            sys.exit("[wigle] transactions response was not JSON")
        if not results:
            break
        for r in results:
            tid = r.get("transid")
            if tid:
                out.append(tid)
                if len(out) >= limit:
                    break
        if len(results) < 100:
            break
        page += 1
    return out


def wigle_download_csv(token: str, transid: str) -> bytes:
    """Download one WiGLE upload as CSV bytes."""
    status, body = _wigle_get(WIGLE_CSV.format(transid=transid), token, timeout=300)
    if status != 200:
        sys.exit(f"[wigle] CSV download failed for {transid}: HTTP {status}")
    return body


def pull_from_wigle_push_to_wdgwars(wigle_token: str, wdg_key: str, field: str,
                                    latest: int, dry_run: bool, chunk_rows: int,
                                    cooldown_sec: float) -> int:
    """Pull your latest WiGLE upload(s) and push each to WDGoWars."""
    transids = wigle_list_transactions(wigle_token, latest)
    if not transids:
        print("[wigle] no uploads found on your account", file=sys.stderr)
        return 0
    print(f"[wigle] pulling {len(transids)} most-recent upload(s): "
          f"{', '.join(transids)}", file=sys.stderr)
    rc = 0
    for tid in transids:
        csv_bytes = wigle_download_csv(wigle_token, tid)
        print(f"[wigle] {tid}: {len(csv_bytes) / 1024:.1f} KB -> WDGoWars",
              file=sys.stderr)
        r = upload_csv_bytes(csv_bytes, f"{tid}.csv", wdg_key, field,
                             dry_run, chunk_rows, cooldown_sec)
        rc = rc or r
    return rc


# ───────────────────────────── Scheduling (--schedule / --unschedule) ───────
#
# Mechanism per OS:
#   Linux with systemd  → user systemd units in ~/.config/systemd/user/
#                         (timer + service, OnCalendar daily)
#   Linux without systemd, macOS → user crontab
#   Windows             → schtasks /Create /SC DAILY /ST HH:MM
#
# Default schedule: daily at 03:00 local time, pulling the latest WiGLE
# upload and pushing it. WiGLE's per-account query budget makes a daily
# push the right cadence — hourly pulls eat your quota fast for no win.
# A timestamped marker comment goes into every unit so --unschedule can
# find and remove them cleanly.

SCHEDULE_MARKER = "managed-by-wigle-to-wdgwars"
SYSTEMD_UNIT_NAME = "wigle-to-wdgwars"  # .service + .timer share this stem
WINDOWS_TASK_NAME = "WigleToWDGoWars"
DEFAULT_SCHEDULE_TIME = "03:00"


def _python_exe() -> str:
    """Absolute path to the Python that's running us.

    Used in unit files so PATH changes (or systemd's minimal environment)
    can't pick a different interpreter than the one with our deps."""
    return sys.executable


def _script_path() -> Path:
    """Absolute path to this wigle_to_wdgwars.py file."""
    return Path(__file__).resolve()


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _has_systemd() -> bool:
    """True only on a Linux host that actually runs systemd as PID 1
    and has systemctl on PATH. Avoids the WSL false-positive where
    systemctl is installed but `/run/systemd/system` is absent."""
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which("systemctl") is None:
        return False
    return Path("/run/systemd/system").exists()


def _schedule_mechanism() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux") and _has_systemd():
        return "systemd"
    return "cron"  # macOS + Linux-without-systemd


def _validate_hhmm(s: str) -> str:
    """Parse 'HH:MM' (24h). Returns canonical 'HH:MM' or raises ValueError."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {s!r}")
    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"time out of range: {s!r}")
    return f"{hh:02d}:{mm:02d}"


def _schedule_argv(use_from_wigle: bool, chunk_size: int) -> list[str]:
    """Build the wigle_to_wdgwars argv that the scheduler will run.

    Always uses the saved keys (no --key / --wigle-key on the command
    line — that would leak them into the unit file / crontab / schtasks
    output, all of which are readable by other processes on the box).
    """
    cmd = [_python_exe(), str(_script_path())]
    if use_from_wigle:
        cmd += ["--from-wigle", "--wigle-latest", "1"]
    cmd += ["--chunk-size", str(chunk_size)]
    return cmd


# ── Pure renderers (no side effects, easy to unit-test) ─────────────────────

def render_systemd_units(time_hhmm: str, use_from_wigle: bool,
                         chunk_size: int, python_exe: str,
                         script_path: Path,
                         dry_run: bool = False) -> dict[str, str]:
    """Render (service, timer) unit text. Pure — does not touch disk."""
    time_hhmm = _validate_hhmm(time_hhmm)
    argv = _schedule_argv(use_from_wigle, chunk_size)
    # Swap in the python_exe + script_path arguments so callers can
    # inject test paths without monkeypatching sys.executable / __file__.
    argv[0] = python_exe
    argv[1] = str(script_path)
    if dry_run:
        argv.append("--dry-run")
    exec_start = " ".join(_shell_quote(a) for a in argv)
    desc_suffix = " [DRY-RUN]" if dry_run else ""
    service = (
        "[Unit]\n"
        f"Description=wigle-to-wdgwars daily push{desc_suffix}\n"
        f"# {SCHEDULE_MARKER}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=Run wigle-to-wdgwars daily at {time_hhmm}\n"
        f"# {SCHEDULE_MARKER}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {time_hhmm}:00\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {"service": service, "timer": timer}


def render_cron_line(time_hhmm: str, use_from_wigle: bool,
                     chunk_size: int, python_exe: str,
                     script_path: Path,
                     dry_run: bool = False) -> str:
    """Render the cron line for the daily run. Pure."""
    time_hhmm = _validate_hhmm(time_hhmm)
    hh, mm = time_hhmm.split(":")
    argv = _schedule_argv(use_from_wigle, chunk_size)
    argv[0] = python_exe
    argv[1] = str(script_path)
    if dry_run:
        argv.append("--dry-run")
    cmd = " ".join(_shell_quote(a) for a in argv)
    log = "$HOME/.wigle-to-wdgwars-cron.log"
    return (f"{int(mm)} {int(hh)} * * * {cmd} "
            f">> {log} 2>&1  # {SCHEDULE_MARKER}\n")


def render_schtasks_create(time_hhmm: str, use_from_wigle: bool,
                           chunk_size: int, python_exe: str,
                           script_path: Path,
                           dry_run: bool = False) -> list[str]:
    """Render the `schtasks /Create` argv for Windows. Pure.

    No `cmd /c "... >> log 2>&1"` wrap: `schtasks /TR` hard-caps the
    action string at 261 characters, and the wrap form blows past that
    once the full venv-python path is included. Users see daily-run
    outcome via Task Scheduler's "Last Result" column instead, or by
    running the same command manually from PowerShell to inspect stderr.
    The README's troubleshooting section walks through both paths.
    """
    time_hhmm = _validate_hhmm(time_hhmm)
    argv = _schedule_argv(use_from_wigle, chunk_size)
    argv[0] = python_exe
    argv[1] = str(script_path)
    if dry_run:
        argv.append("--dry-run")
    action = " ".join(f'"{a}"' if " " in a else a for a in argv)
    return ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME,
            "/TR", action, "/SC", "DAILY", "/ST", time_hhmm,
            "/RL", "LIMITED", "/F"]


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting for systemd ExecStart and cron lines.

    systemd's ExecStart parser handles unquoted args fine when they
    contain no whitespace; quote only when necessary so the unit file
    stays readable in `systemctl cat`.
    """
    if not s:
        return "''"
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@%_-+=:,./"
    if all(c in safe for c in s):
        return s
    # Single-quote and escape any embedded single quotes
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ── Installers (write files, run system commands) ───────────────────────────

def install_systemd_user(time_hhmm: str, use_from_wigle: bool,
                         chunk_size: int, dry_run: bool = False) -> int:
    units = render_systemd_units(time_hhmm, use_from_wigle, chunk_size,
                                 _python_exe(), _script_path(),
                                 dry_run=dry_run)
    unit_dir = _systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.service"
    timer_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.timer"
    service_path.write_text(units["service"])
    print(f"[schedule] wrote {service_path}", file=sys.stderr)
    timer_path.write_text(units["timer"])
    print(f"[schedule] wrote {timer_path}", file=sys.stderr)
    target = f"{SYSTEMD_UNIT_NAME}.timer"
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", target]):
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[schedule] '{' '.join(cmd)}' returned {rc}",
                  file=sys.stderr)
            return rc
    print(f"[schedule] enabled and started {target}", file=sys.stderr)
    print(f"[schedule] status:  systemctl --user status {target}",
          file=sys.stderr)
    print(f"[schedule] logs:    journalctl --user -u {target} -f",
          file=sys.stderr)
    return 0


def uninstall_systemd_user() -> int:
    unit_dir = _systemd_user_dir()
    found = False
    for name in (f"{SYSTEMD_UNIT_NAME}.timer",
                 f"{SYSTEMD_UNIT_NAME}.service"):
        unit = unit_dir / name
        if unit.exists():
            found = True
            subprocess.call(["systemctl", "--user", "stop", name],
                            stderr=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL)
            subprocess.call(["systemctl", "--user", "disable", name],
                            stderr=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL)
            unit.unlink()
            print(f"[schedule] removed {unit}", file=sys.stderr)
    if found:
        subprocess.call(["systemctl", "--user", "daemon-reload"])
    else:
        print("[schedule] no wigle-to-wdgwars systemd units found",
              file=sys.stderr)
    return 0


def install_cron(time_hhmm: str, use_from_wigle: bool, chunk_size: int,
                 dry_run: bool = False) -> int:
    if shutil.which("crontab") is None:
        print("[schedule] crontab not found on PATH", file=sys.stderr)
        return 1
    new_line = render_cron_line(time_hhmm, use_from_wigle, chunk_size,
                                _python_exe(), _script_path(),
                                dry_run=dry_run)
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current = r.stdout if r.returncode == 0 else ""
    except FileNotFoundError:
        return 1
    cleaned = "\n".join(l for l in current.splitlines()
                        if SCHEDULE_MARKER not in l)
    combined = (cleaned.rstrip() + "\n" + new_line) if cleaned.strip() else new_line
    proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE,
                            text=True)
    proc.communicate(combined)
    if proc.returncode != 0:
        print(f"[schedule] crontab write failed (rc={proc.returncode})",
              file=sys.stderr)
        return proc.returncode
    print(f"[schedule] added cron entry (marker: {SCHEDULE_MARKER})",
          file=sys.stderr)
    print(f"[schedule] view: crontab -l", file=sys.stderr)
    print(f"[schedule] log:  tail -f ~/.wigle-to-wdgwars-cron.log",
          file=sys.stderr)
    return 0


def uninstall_cron() -> int:
    if shutil.which("crontab") is None:
        return 0
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode != 0:
            return 0
        current = r.stdout
    except FileNotFoundError:
        return 0
    cleaned = "\n".join(l for l in current.splitlines()
                        if SCHEDULE_MARKER not in l)
    if cleaned == current.rstrip("\n"):
        print("[schedule] no wigle-to-wdgwars cron entries found",
              file=sys.stderr)
        return 0
    proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE,
                            text=True)
    proc.communicate(cleaned)
    print("[schedule] removed wigle-to-wdgwars cron entries", file=sys.stderr)
    return 0


def install_windows_task(time_hhmm: str, use_from_wigle: bool,
                         chunk_size: int, dry_run: bool = False) -> int:
    cmd = render_schtasks_create(time_hhmm, use_from_wigle, chunk_size,
                                 _python_exe(), _script_path(),
                                 dry_run=dry_run)
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc
    print(f"[schedule] created task: {WINDOWS_TASK_NAME}", file=sys.stderr)
    print(f"[schedule] view:    schtasks /Query /TN {WINDOWS_TASK_NAME}",
          file=sys.stderr)
    print(f"[schedule] run now: schtasks /Run /TN {WINDOWS_TASK_NAME}",
          file=sys.stderr)
    print(f"[schedule] (Task Scheduler doesn't capture stdout — to see "
          f"what a run did, fire it from PowerShell directly.)",
          file=sys.stderr)
    return 0


def uninstall_windows_task() -> int:
    rc = subprocess.call(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME,
                          "/F"], stderr=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL)
    if rc == 0:
        print(f"[schedule] removed scheduled task: {WINDOWS_TASK_NAME}",
              file=sys.stderr)
    else:
        print(f"[schedule] no scheduled task named {WINDOWS_TASK_NAME} found",
              file=sys.stderr)
    return 0


def interactive_schedule_setup(have_wigle: bool = False) -> int:
    """Walk the user through installing a daily timer.

    `have_wigle=True` lets the wizard offer --from-wigle as the default;
    `False` falls back to instructing the user to point cron at a file
    path they refresh themselves (no installer for that — too many
    user-specific assumptions about where the file lives)."""
    print("", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print(" wigle-to-wdgwars — schedule setup", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print("", file=sys.stderr)

    mech = _schedule_mechanism()
    mech_label = {"systemd": "systemd user timer",
                  "cron": "user crontab",
                  "windows": "Windows Task Scheduler"}[mech]
    print(f" Detected scheduler: {mech_label}", file=sys.stderr)

    if not have_wigle:
        print("", file=sys.stderr)
        print(" No WiGLE token saved. The auto-installer only handles the",
              file=sys.stderr)
        print(" --from-wigle mode (pull latest from WiGLE, push to WDGoWars).",
              file=sys.stderr)
        print(" For file-based scheduling (point cron at a local CSV), see",
              file=sys.stderr)
        print(" the 'Running on a schedule (timer)' section in README.md.",
              file=sys.stderr)
        return 0

    if not _prompt_yes_no(" Install a daily timer now?", default=True):
        print("", file=sys.stderr)
        print(" Skipped. You can configure later with:", file=sys.stderr)
        print("   python3 wigle_to_wdgwars.py --schedule", file=sys.stderr)
        return 0

    time_hhmm = _validate_hhmm(_prompt_str(" Run time (24h HH:MM)",
                                           DEFAULT_SCHEDULE_TIME))
    chunk_size = _prompt_int(" CSV chunk size (rows per POST)", 10000,
                             min_val=100, max_val=50_000)
    dry_run = _prompt_yes_no(
        " Install in dry-run first? (decodes + logs but never POSTs)",
        default=True)

    # Preview
    print("", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print(" The following will be installed:", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    if mech == "systemd":
        units = render_systemd_units(time_hhmm, True, chunk_size,
                                     _python_exe(), _script_path(),
                                     dry_run=dry_run)
        print(f"\n {_systemd_user_dir()}/{SYSTEMD_UNIT_NAME}.service:",
              file=sys.stderr)
        print(textwrap.indent(units["service"], "   "), file=sys.stderr)
        print(f" {_systemd_user_dir()}/{SYSTEMD_UNIT_NAME}.timer:",
              file=sys.stderr)
        print(textwrap.indent(units["timer"], "   "), file=sys.stderr)
        print(" Plus: systemctl --user daemon-reload && enable --now",
              file=sys.stderr)
    elif mech == "cron":
        line = render_cron_line(time_hhmm, True, chunk_size,
                                _python_exe(), _script_path(),
                                dry_run=dry_run)
        print("\n Appended to your user crontab:", file=sys.stderr)
        print(textwrap.indent(line, "   "), file=sys.stderr)
    elif mech == "windows":
        cmd = render_schtasks_create(time_hhmm, True, chunk_size,
                                     _python_exe(), _script_path(),
                                     dry_run=dry_run)
        print("\n schtasks command:", file=sys.stderr)
        print(f"   {' '.join(cmd)}", file=sys.stderr)
    if dry_run:
        print("", file=sys.stderr)
        print(" *** DRY-RUN: --dry-run is baked into the scheduled command.",
              file=sys.stderr)
        print(" *** Re-run --schedule and answer 'no' to dry-run to go live.",
              file=sys.stderr)
    print("", file=sys.stderr)

    if not _prompt_yes_no(" Install now?", default=True):
        print("", file=sys.stderr)
        print(" Skipped. To install non-interactively later:",
              file=sys.stderr)
        dry_flag = " --schedule-dry-run" if dry_run else ""
        print(f"   python3 wigle_to_wdgwars.py --schedule "
              f"--schedule-time {time_hhmm} "
              f"--schedule-chunk-size {chunk_size}{dry_flag}",
              file=sys.stderr)
        return 0

    if mech == "systemd":
        rc = install_systemd_user(time_hhmm, True, chunk_size,
                                  dry_run=dry_run)
    elif mech == "cron":
        rc = install_cron(time_hhmm, True, chunk_size, dry_run=dry_run)
    elif mech == "windows":
        rc = install_windows_task(time_hhmm, True, chunk_size,
                                  dry_run=dry_run)
    else:
        rc = 1

    if rc == 0:
        print("", file=sys.stderr)
        if dry_run:
            print(" ✓ Schedule installed in DRY-RUN (no uploads).",
                  file=sys.stderr)
            print("   Verify it works, then re-run --schedule and",
                  file=sys.stderr)
            print("   answer 'no' to the dry-run prompt to go live.",
                  file=sys.stderr)
        else:
            print(" ✓ Schedule installed (live uploads enabled).",
                  file=sys.stderr)
        print(" To remove later: python3 wigle_to_wdgwars.py --unschedule",
              file=sys.stderr)
    return rc


def cmd_schedule_headless(args) -> int:
    """Headless --schedule path. Reads time + chunk size + dry-run from args."""
    time_hhmm = _validate_hhmm(args.schedule_time or DEFAULT_SCHEDULE_TIME)
    chunk_size = args.schedule_chunk_size or 10000
    dry_run = bool(args.schedule_dry_run)
    # Default: --from-wigle. File-based scheduling isn't auto-installable
    # (we'd need to know where the user puts their CSV); README handles it.
    if not WIGLE_KEY_FILE.exists() and not os.environ.get("WIGLE_API_KEY"):
        sys.exit("--schedule needs a saved WiGLE token (run --setup first), "
                 "or write your own unit pointing at a local CSV — "
                 "see README.md.")
    mech = _schedule_mechanism()
    if mech == "systemd":
        return install_systemd_user(time_hhmm, True, chunk_size,
                                    dry_run=dry_run)
    if mech == "cron":
        return install_cron(time_hhmm, True, chunk_size, dry_run=dry_run)
    if mech == "windows":
        return install_windows_task(time_hhmm, True, chunk_size,
                                    dry_run=dry_run)
    sys.exit(f"unsupported platform for --schedule: {sys.platform}")


def cmd_unschedule() -> int:
    """Remove every wigle-to-wdgwars-managed schedule entry on this platform."""
    rcs = []
    if sys.platform == "win32":
        rcs.append(uninstall_windows_task())
    else:
        if _has_systemd():
            rcs.append(uninstall_systemd_user())
        rcs.append(uninstall_cron())
    return 0 if all(rc == 0 for rc in rcs) else 1


# ───────────────────────────── CLI ───────────────────────────────────────────

def main() -> int:
    global ENDPOINT
    ap = argparse.ArgumentParser(
        prog="wigle-to-wdgwars",
        description="Upload WiGLE-1.6 CSVs (and optionally aircraft JSON) to WDGoWars.",
        epilog="See README.md for the full WDGoWars API reference and cron recipes.",
    )
    ap.add_argument("csv", nargs="?",
                    help="path to a WiGLE-1.6 CSV (or .gz, gzip is auto-detected); "
                         "omit with --whoami, --aircraft-json, --preview, --update, "
                         "--setup, or --schedule")
    ap.add_argument("--update", action="store_true",
                    help="pull the latest version of wigle-to-wdgwars (uses "
                         "git pull if you cloned the repo, otherwise downloads "
                         "wigle_to_wdgwars.py from GitHub)")
    ap.add_argument("--field", default="file",
                    help="multipart field name (default: file)")
    ap.add_argument("--key", help="API key (overrides $WDGWARS_API_KEY and key file)")
    ap.add_argument("--api-url", metavar="URL",
                    help=f"override the CSV upload endpoint (default: "
                         f"{ENDPOINT}). Useful for staging hosts or local "
                         f"mocks; aircraft JSON uploads still use the signed "
                         f"endpoint at {SIGNED_ENDPOINT}.")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the request but do not POST")
    ap.add_argument("--preview", action="store_true",
                    help="print the first 6 rows of the WiGLE CSV as JSON to "
                         "stdout and exit. No upload, no network. Useful for "
                         "confirming the parser sees what you expect before "
                         "wiring into a schedule. Mirrors Heimdall + Muninn "
                         "--preview for cross-tool consistency.")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="suppress informational banners (errors still print)")
    ap.add_argument("--no-version-check", action="store_true",
                    help="skip the daily GitHub release check entirely")
    ap.add_argument("--chunk-size", type=int, default=0,
                    help="proactively split CSV into N-row chunks (0=single POST). "
                         "10000 is a safe default for large uploads. The tool also "
                         "reacts to LOCOSP's 15 MB upload cap (HTTP 413 envelope) "
                         "by bisecting any over-cap chunk automatically, so this "
                         "flag is mostly belt-and-suspenders.")
    ap.add_argument("--chunk-cooldown", type=float, default=5.0,
                    help="seconds to sleep between chunks (default: 5)")
    ap.add_argument("--whoami", action="store_true",
                    help="GET /api/me to validate the API key, then exit")
    ap.add_argument("--aircraft-json", metavar="FILE",
                    help="push a JSON list of aircraft records to the signed /api/upload/ endpoint")
    ap.add_argument("--aircraft-batch", type=int, default=500,
                    help="aircraft records per signed POST (default: 500)")
    ap.add_argument("--from-wigle", action="store_true",
                    help="pull your latest upload(s) straight from WiGLE and push them to "
                         "WDGoWars, no file needed. Uses your WiGLE token (--wigle-key).")
    ap.add_argument("--wigle-key", metavar="TOKEN",
                    help="WiGLE 'Encoded for use' token (overrides $WIGLE_API_KEY and key file). "
                         "Used with --from-wigle.")
    ap.add_argument("--wigle-latest", type=int, default=1, metavar="N",
                    help="with --from-wigle, how many most-recent WiGLE uploads to pull "
                         "(default: 1)")
    ap.add_argument("--setup", action="store_true",
                    help="interactive first-time setup — prompts for your "
                         "WDGoWars and WiGLE keys, validates them, saves them "
                         "to ~/.config/wigle-to-wdgwars/ (mode 600), and "
                         "optionally installs a daily timer.")
    ap.add_argument("--save-key", metavar="KEY",
                    help="non-interactive: save the given WDGoWars API key "
                         "to the user config dir. Prefer --setup for first-time install.")
    ap.add_argument("--save-wigle-key", metavar="TOKEN",
                    help="non-interactive: save the given WiGLE 'Encoded for "
                         "use' token. Prefer --setup for first-time install.")
    ap.add_argument("--schedule", action="store_true",
                    help="install or reconfigure a daily scheduled push. "
                         "Interactive when run alone; with --schedule-time or "
                         "--schedule-chunk-size it runs headless.")
    ap.add_argument("--unschedule", action="store_true",
                    help="remove every wigle-to-wdgwars-managed scheduled "
                         "task on this host (systemd user units, cron "
                         "entries, Windows scheduled tasks).")
    ap.add_argument("--schedule-time", metavar="HH:MM",
                    help=f"24-hour daily run time for --schedule headless "
                         f"mode (default: {DEFAULT_SCHEDULE_TIME})")
    ap.add_argument("--schedule-chunk-size", type=int, metavar="N",
                    help="--chunk-size to bake into the scheduled command "
                         "(default: 10000)")
    ap.add_argument("--schedule-dry-run", action="store_true",
                    help="install the schedule with --dry-run baked in. "
                         "Decodes + logs but never POSTs. Re-run --schedule "
                         "without this flag to go live.")
    ap.add_argument("--version", action="version",
                    version=f"wigle-to-wdgwars {__version__}")
    args = ap.parse_args()

    # --update is a top-level mode; runs before anything else.
    if args.update:
        return _run_update()

    # --api-url overrides the CSV upload endpoint (the primary path).
    # Aircraft JSON uploads still use the signed endpoint — override
    # there belongs in Muninn, not wigle.
    if args.api_url:
        ENDPOINT = args.api_url

    # Soft nudge: if a newer release is out, mention it (non-blocking,
    # daily-cached). Skip in CI-shaped invocations.
    if not args.quiet and not args.no_version_check:
        newer = _check_for_update()
        if newer:
            print(f"[wigle-to-wdgwars] note: v{newer} is available "
                  f"(you're on v{__version__}). Run `--update` to upgrade.",
                  file=sys.stderr)

    # --preview is a parser dry-run; needs the CSV path but no key.
    if args.preview:
        if not args.csv:
            ap.error("--preview needs a CSV path")
        return preview_csv(Path(args.csv))

    # Key management / scheduling modes — handle before requiring an input
    if args.setup:
        return interactive_setup()
    if args.save_key:
        save_key(args.save_key)
        return 0
    if args.save_wigle_key:
        save_wigle_token(args.save_wigle_key)
        return 0
    if args.unschedule:
        return cmd_unschedule()
    if args.schedule:
        headless = (args.schedule_time is not None
                    or args.schedule_chunk_size is not None
                    or args.schedule_dry_run)
        if headless:
            return cmd_schedule_headless(args)
        # Interactive: needs a saved WDGoWars key; offer to set up if not.
        if not DEFAULT_KEY_FILE.exists() and not os.environ.get("WDGWARS_API_KEY"):
            print("[wigle-to-wdgwars] no WDGoWars key saved — running --setup "
                  "first.", file=sys.stderr)
            return interactive_setup()
        return interactive_schedule_setup(have_wigle=WIGLE_KEY_FILE.exists()
                                          or bool(os.environ.get("WIGLE_API_KEY")))

    key = load_key(args.key)

    if args.whoami:
        return whoami(key)

    if args.from_wigle:
        wigle_token = load_wigle_token(args.wigle_key)
        return pull_from_wigle_push_to_wdgwars(
            wigle_token, key, args.field, args.wigle_latest,
            args.dry_run, args.chunk_size, args.chunk_cooldown)

    if args.aircraft_json:
        return upload_aircraft_json(Path(args.aircraft_json), key,
                                    dry_run=args.dry_run, batch=args.aircraft_batch)

    if not args.csv:
        ap.error("provide a CSV path, --from-wigle, --aircraft-json FILE, or --whoami")

    return upload_csv(Path(args.csv), key, args.field, args.dry_run,
                      chunk_rows=args.chunk_size, cooldown_sec=args.chunk_cooldown)


if __name__ == "__main__":
    sys.exit(main())
