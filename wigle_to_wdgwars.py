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

__version__ = "1.1.0"
GITHUB_URL = "https://github.com/HiroAlleyCat/wigle-to-wdgwars"

import argparse
import gzip
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import gungnir

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
    """POST pre-split CSV chunks to WDGoWars. Returns shell exit code (0 ok)."""
    total_kb = sum(len(c) for c in chunks) / 1024
    print(
        f"[wdgwars] POST {ENDPOINT} field={field} file={name} "
        f"chunks={len(chunks)} total={total_kb:.1f} KB",
        file=sys.stderr,
    )
    if dry_run:
        print("[wdgwars] dry-run: not sending", file=sys.stderr)
        return 0
    payloads: list[dict] = []
    for idx, body in enumerate(chunks, 1):
        try:
            status, raw, dur = _post_one(body, name, key, field)
        except urllib.error.URLError as e:
            sys.exit(f"[wdgwars] network error on chunk {idx}/{len(chunks)}: {e}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"ok": False, "error": "non-json response", "raw": raw[:300]}
        print(
            f"[wdgwars] chunk {idx}/{len(chunks)} HTTP {status} in {dur:.1f}s "
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
        elif idx < len(chunks):
            time.sleep(cooldown_sec)
    if len(chunks) == 1:
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


# ───────────────────────────── CLI ───────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="wigle-to-wdgwars",
        description="Upload WiGLE-1.6 CSVs (and optionally aircraft JSON) to WDGoWars.",
        epilog="See README.md for the full WDGoWars API reference and cron recipes.",
    )
    ap.add_argument("csv", nargs="?",
                    help="path to a WiGLE-1.6 CSV (or .gz, gzip is auto-detected); "
                         "omit with --whoami or --aircraft-json")
    ap.add_argument("--field", default="file",
                    help="multipart field name (default: file)")
    ap.add_argument("--key", help="API key (overrides $WDGWARS_API_KEY and key file)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the request but do not POST")
    ap.add_argument("--chunk-size", type=int, default=0,
                    help="split CSV into N-row chunks to dodge Cloudflare 524s (0=single POST). "
                         "10000 is a safe default for large uploads.")
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
    args = ap.parse_args()

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
