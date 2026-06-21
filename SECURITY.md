# Security Notes

## What this tool does

- Reads a local WiGLE-1.6 CSV file (or `.wiglecsv.gz`, gzip is
  auto-detected) and POSTs it to `https://wdgwars.pl/api/upload-csv`
  as multipart/form-data.
- With `--aircraft-json`, reads a local JSON list of aircraft records
  and POSTs them to `https://wdgwars.pl/api/upload/` as an HMAC-signed
  JSON envelope.
- With `--from-wigle`, fetches your most recent uploads from
  `https://api.wigle.net/api/v2/file/transactions` using your WiGLE
  "Encoded for use" token, then POSTs the CSV(s) to wdgwars.pl.
- With `--whoami`, hits `https://wdgwars.pl/api/me` to validate the
  WDGoWars key.
- With `--update`, fetches a fresh `wigle_to_wdgwars.py` (and
  `requirements.txt`) from `https://github.com/HiroAlleyCat/wigle-to-wdgwars`
  via `git pull` or raw GitHub.
- With `--schedule`, writes a daily timer to one of
  `~/.config/systemd/user/`, the user's crontab, or Windows Task
  Scheduler.

That's the entire outbound footprint.

## What this tool does not do

- No telemetry or analytics. The only outbound traffic is to
  `wdgwars.pl`, `api.wigle.net`, `api.github.com`, and
  `raw.githubusercontent.com` — each only on a flag the user invoked.
  The GitHub calls happen on `--update` and on the daily version-check
  nudge, both of which can be skipped with `--no-version-check`.
- No `eval`, no `exec`, no `os.system`, no `shell=True`. There are no
  command-injection paths from CSV content or API responses into the
  shell.
- No remote code execution at runtime. The only dependency is
  `gungnir`, pinned by tag in `requirements.txt` and installed via
  pip from the GitHub release tarball.
- No data sent anywhere except the configured WDGoWars and WiGLE
  endpoints when you explicitly opt in.

## API key handling

- WDGoWars key resolution: `--key` flag, then `$WDGWARS_API_KEY`,
  then `~/.config/wigle-to-wdgwars/wdgwars.key`.
- WiGLE token resolution: `--wigle-key` flag, then `$WIGLE_API_KEY`,
  then `~/.config/wigle-to-wdgwars/wigle.key`.
- Both files are written with mode `0600` (POSIX). The directory is
  created with `0700`. `--setup` and `--save-key`/`--save-wigle-key`
  are the only write paths.
- Keys are sent over HTTPS only: WDGoWars in the `X-API-Key` request
  header, WiGLE in the `Authorization: Basic <token>` header. The TLS
  context is Python's `ssl.create_default_context()` default — system
  trust store, hostname verification on, TLS 1.2+.
- Keys are never logged. Scheduled `--schedule` units read keys from
  the saved files at run-time; they are never baked into the unit
  file, cron entry, or schtasks command. `scripts/smoke.sh` asserts
  this on every CI run.

## What the keys can do

The WDGoWars API key authorises you to submit observations under your
account. If it leaks, an attacker could:

- Submit fake Wi-Fi / BLE / mesh / aircraft captures under your name.
- Read your account stats via `GET /api/me`.

It cannot (as far as we know):

- Change your password.
- Withdraw money or make purchases.
- Affect other users' accounts.

The WiGLE token (the "Encoded for use" string from
https://wigle.net/account) authorises you to list and download your
own past WiGLE uploads. If it leaks, an attacker could read your own
WiGLE upload history.

If you suspect either credential has leaked, rotate it on the issuing
site and re-run `--setup` to save the new value.

## HMAC envelope (aircraft JSON path)

Aircraft JSON uploads use the same HMAC envelope shape as Muninn and
Heimdall, via the shared `gungnir` library:

```python
payload   = {"networks": [], "aircraft": chunk, "meshcore_nodes": []}
body_json = json.dumps(payload, separators=(",", ":"))
data_b64  = base64.b64encode(body_json.encode()).decode()
nonce     = secrets.token_hex(8)
sig       = hmac.new(api_key.encode(),
                     (nonce + data_b64).encode(),
                     hashlib.sha256).hexdigest()
envelope  = {"data": data_b64, "nonce": nonce, "sig": sig}
```

`json.dumps(..., separators=(",", ":"))` and `ensure_ascii=True`
(Python default) are load-bearing — different whitespace or non-ASCII
handling produces a different signature.

## Static-analysis review

A review of this tool against the SonarCloud SAST finding classes (path
traversal, command/argument injection, insecure temp use, unsafe DB opens)
found nothing to remediate — the scheduler arguments are shell-quoted, secrets
never reach the command line, and the secret-file writer refuses symlinks and
uses mode 600. The full write-up is in
[SECURITY-FINDINGS.md](SECURITY-FINDINGS.md); the posture is locked by
`tests/test_security.py`.

## Reporting issues

Open a GitHub issue, or DM the maintainer on the WDGoWars community
channels. For anything potentially exploitable upstream (in WDGoWars
itself), please disclose privately to LOCOSP first rather than filing
a public issue here.
