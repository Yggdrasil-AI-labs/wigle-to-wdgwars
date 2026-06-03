# wigle-to-wdgwars

Push WiGLE-format Wi-Fi/BLE wardrive CSVs (and optionally aircraft JSON) to the
**[WDGoWars](https://wdgwars.pl/)** community wardriving leaderboard.

A small Python 3 CLI. One dependency: [gungnir](https://github.com/HiroAlleyCat/gungnir), the shared HMAC transport client used by every wdgwars.pl feeder in this family. Install it with `pip install -r requirements.txt` (no git on PATH required — pip fetches it as a tarball over plain HTTPS).

## Family

Sibling repos in the WDGoWars feeder family:

- [Muninn](https://github.com/HiroAlleyCat/adsb-to-wdgwars) — ADS-B feeder
- [Heimdall](https://github.com/HiroAlleyCat/meshcore-to-wdgwars) — MeshCore LoRa feeder
- [gungnir](https://github.com/HiroAlleyCat/gungnir) — shared HMAC transport library
- [wdgwars-api-tester](https://github.com/HiroAlleyCat/wdgwars-api-tester) — API surface probe

---

## Contents

- [What this is](#what-this-is)
- [Easiest install — guided setup](#easiest-install--guided-setup) — `./setup.sh` saves both keys and installs a daily timer
- [Quick start — one-off push without saving keys](#quick-start--one-off-push-without-saving-keys)
- [Installing](#installing) — manual venv + pip flow
- [Getting a WiGLE CSV in the first place](#getting-a-wigle-csv-in-the-first-place)
- [Running on a schedule (timer)](#running-on-a-schedule-timer) — what `--schedule` installs, plus hand-written recipes
- [WDGoWars API reference](#wdgowars-api-reference) — reverse-engineered, since the portal has no public docs
- [Aircraft JSON format (signed endpoint)](#aircraft-json-format-signed-endpoint)
- [Troubleshooting](#troubleshooting)
- [Related tools](#related-tools)
- [License](#license)

---

## What this is

[WDGoWars](https://wdgwars.pl/) ("Watch Dogs Go Wars") is a community
wardriving leaderboard / game. Players capture Wi-Fi networks, Bluetooth
devices, and aircraft, upload their observations, score points, earn badges,
and join gangs. It's small, friendly, and Polish-run.

The portal accepts uploads on three endpoints, but **does not publish API
docs**. Everyone who has built an uploader has reverse-engineered the
contract from network captures or open-source firmware. This tool:

1. Pushes a WiGLE-1.6 CSV to `/api/upload-csv` for Wi-Fi + BLE.
2. Pushes a JSON list of aircraft records to the signed `/api/upload/`
   endpoint.
3. Optionally **pulls your uploads straight from WiGLE** (`--from-wigle`) and
   pushes them, so you never touch a file.
4. Documents the wire format so the next person doesn't have to start over
   (see [WDGoWars API reference](#wdgwars-api-reference)).

It's designed to be readable, droppable into a cron job, and friendly to
new players who haven't published a wardrive before.

### Who this is for

- You wardrive with the **[WiGLE Android app](https://play.google.com/store/apps/details?id=net.wigle.wigleandroid)**
  or another tool that exports WiGLE-format CSV, and you want a second
  place to send your captures.
- You run a **Kismet** or **hcxdumptool** rig and have converted its output
  to WiGLE CSV.
- You want a **scheduled push** from a Pi/server that keeps a local DB of
  observations and produces CSVs.
- You're a tool author who needs a working reference for the WDGoWars
  ingest contract.

---

## Easiest install — guided setup

If you just want a daily push running and don't want to read the rest of this
README, this is the path. One script does the whole install: venv, deps, both
API keys validated, and a daily timer.

```bash
git clone https://github.com/HiroAlleyCat/wigle-to-wdgwars.git
cd wigle-to-wdgwars
./setup.sh                          # Linux / Mac / Pi
```

```bat
REM Windows: double-click setup.bat, or from a terminal:
setup.bat
```

What `setup.sh` does, in order:

1. Creates a project-local `.venv/` and installs `requirements.txt` into it
   (works on PEP 668 distros without `--break-system-packages`).
2. Prompts for your **WDGoWars API key**, validates it against `/api/me`,
   saves to `~/.config/wigle-to-wdgwars/wdgwars.key` (mode 600).
3. Prompts for your **WiGLE token** (the "Encoded for use" string from
   [wigle.net/account](https://wigle.net/account)), validates it by listing
   one transaction, saves to `~/.config/wigle-to-wdgwars/wigle.key` (mode 600).
   Skippable if you only want to push local CSVs.
4. Offers to install a **daily timer** (systemd user unit / cron entry /
   Windows scheduled task, depending on what your OS supports) that runs
   `--from-wigle` at 03:00 local time and uploads your latest WiGLE drive.
5. Defaults the timer to **dry-run** so the first scheduled tick decodes
   and logs but never POSTs. Re-run `./run.sh --schedule` and answer "no"
   to the dry-run prompt to flip it live.

After that, `./run.sh` (no args) does a one-off push, and the timer takes
care of the rest. To remove the schedule later: `./run.sh --unschedule`.

You can run `--setup` again at any point to rotate keys or reconfigure the
timer — it's idempotent and asks before replacing anything.

To do any of those steps without the bootstrap script (e.g. you already
have a venv), invoke the same flags directly:

```bash
.venv/bin/python wigle_to_wdgwars.py --setup        # full interactive flow
.venv/bin/python wigle_to_wdgwars.py --schedule     # just the timer step
.venv/bin/python wigle_to_wdgwars.py --unschedule   # remove the timer

# Non-interactive equivalents (for provisioning):
.venv/bin/python wigle_to_wdgwars.py --save-key YOUR_WDGWARS_KEY
.venv/bin/python wigle_to_wdgwars.py --save-wigle-key YOUR_WIGLE_TOKEN
.venv/bin/python wigle_to_wdgwars.py --schedule --schedule-time 03:00 \
    --schedule-chunk-size 10000 --schedule-dry-run
```

### What to expect after `./setup.sh`

A few things that can read as "is this broken?" the first time:

- **The first scheduled tick won't show up on your leaderboard.** `--setup`
  defaults the timer to dry-run — the tick decodes and writes a log but
  never POSTs. This is intentional so you can verify the install before
  flipping live. To go live, re-run `./run.sh --schedule` and answer "no"
  to the dry-run prompt.
- **A scheduled run can't read keys from your shell environment.** systemd /
  cron / schtasks all run in a stripped-down environment without your
  `$WDGWARS_API_KEY` / `$WIGLE_API_KEY` env vars. The scheduled command
  reads the saved key files (`~/.config/wigle-to-wdgwars/wdgwars.key` +
  `wigle.key`) instead. `--setup` saved both for you. If you skip `--setup`
  and only export env vars, the timer will fail at run time.
- **WiGLE rate-limits your own-account pulls.** The auto-installed timer
  runs `--from-wigle --wigle-latest 1` once daily, which stays comfortably
  under the WiGLE free-tier query budget. If you bump up `--wigle-latest`
  or run more often, you can hit a per-account quota and start seeing
  `HTTP 429` in the log.

### Checking it's running

You don't have to wait for the daily fire — verify the install end-to-end
right after `./setup.sh`:

```bash
# Linux (systemd user manager)
systemctl --user list-timers wigle-to-wdgwars.timer
systemctl --user start  wigle-to-wdgwars.service   # fire one tick now
journalctl --user -u wigle-to-wdgwars.service -n 30

# Linux/Mac (cron — installed when systemd isn't available)
crontab -l | grep wigle-to-wdgwars
tail -f ~/.wigle-to-wdgwars-cron.log

# Windows (schtasks)
schtasks /Query /TN WigleToWDGoWars /V /FO LIST          :: shows Last Run Result
schtasks /Run   /TN WigleToWDGoWars                      :: fire one tick now
# Task Scheduler does NOT capture stdout. To see what a run produces,
# fire the same command from PowerShell yourself:
.venv\Scripts\python wigle_to_wdgwars.py --from-wigle --wigle-latest 1 \
    --chunk-size 10000 --dry-run
```

A `--dry-run` tick that succeeded looks like (in the log / journal):

```
[wigle] pulling 1 most-recent upload(s): <transid>
[wigle] <transid>: <N> KB -> WDGoWars
[wdgwars] POST https://wdgwars.pl/api/upload-csv field=file file=<transid>.csv chunks=1 total=<N> KB
[wdgwars] dry-run: not sending
```

The `dry-run: not sending` is the safety stop — your data didn't ship to
the leaderboard yet, but everything up to that point worked. To flip live:

```bash
./run.sh --schedule          # interactive, answer "n" to the dry-run prompt
# or, headless:
.venv/bin/python wigle_to_wdgwars.py --schedule --schedule-time 03:00 \
    --schedule-chunk-size 10000     # no --schedule-dry-run = live
```

### Common surprises

- **`bash: ./setup.sh: Permission denied`** — you downloaded the ZIP instead
  of `git clone`, and the executable bit didn't survive. Run `bash setup.sh`
  instead, or `chmod +x *.sh scripts/*.sh` first.
- **`error: externally-managed-environment` from `pip install`** — Bookworm /
  Debian 12+ / Ubuntu 23.04+ / Homebrew Python enforce PEP 668 and refuse
  to install into system Python. The `./setup.sh` flow uses a project-local
  `.venv/` and works around this. If you've been pasting `python3 -m pip
  install -r requirements.txt` from an old README, switch to
  `./setup.sh` (or to the venv recipe in [Installing](#installing) below).
- **`Failed to create venv` from `./setup.sh`** — the `python3-venv` module
  isn't installed on Debian/Ubuntu/Pi by default. `sudo apt install -y
  python3-venv python3-full` and re-run.
- **`./run.sh` errors with `no API key`** — you skipped `--setup` (or it
  didn't get to the save step). Run `./run.sh --setup` to do the wizard.
- **Timer installed but nothing on the leaderboard the next day** — see the
  dry-run note above. You're seeing the safety stop, not a broken install.
- **`HTTP 429` in the log** — either WDGoWars is asking you to wait
  (server-side queue is processing your previous upload — the tool sleeps
  and retries on the next tick) or WiGLE is rate-limiting you for pulling
  too often. The cooldown file at `~/.config/wigle-to-wdgwars/cooldown.json`
  is honored across runs.

---

## Quick start — one-off push without saving keys

If you just want to push a single file right now without saving anything to
disk, paste the key on the command line. Use the venv from
[Installing](#installing) — pasting `python3 wigle_to_wdgwars.py` directly
against system Python errors out with `error: externally-managed-environment`
on Bookworm / Debian 12+ / Homebrew. The venv path is one extra line and
works on every distro.

```bash
# Inside the venv from the Installing section
.venv/bin/python wigle_to_wdgwars.py --whoami --key YOUR_WDGWARS_API_KEY
# → [wigle-to-wdgwars] key OK — user=…  wifi=… ble=… aircraft=…

.venv/bin/python wigle_to_wdgwars.py my-wardrive.wiglecsv.gz \
    --key YOUR_WDGWARS_API_KEY --chunk-size 10000
```

`--chunk-size 10000` is the safe default for anything over ~5 000 rows. See
the [Cloudflare 524 footgun](#the-cloudflare-524-footgun) for why.

On Windows: `.venv\Scripts\python wigle_to_wdgwars.py ...`. Or just use
`run.bat` from the [guided setup](#easiest-install--guided-setup) above.

### No file at all — pull straight from WiGLE

If you wardrive with the WiGLE app, your runs already get uploaded to WiGLE.
With `--from-wigle` the tool grabs your latest upload from WiGLE directly and
pushes it to WDGoWars — you never export, unzip, or move a file.

You need two keys: your **WDGoWars** key (`--key`) and your **WiGLE** token
(`--wigle-key`, the "Encoded for use" string from
[wigle.net/account](https://wigle.net/account)).

```bash
.venv/bin/python wigle_to_wdgwars.py --from-wigle \
    --wigle-key YOUR_WIGLE_ENCODED_TOKEN \
    --key YOUR_WDGWARS_API_KEY \
    --chunk-size 10000
```

By default it pulls your single most-recent upload. Use `--wigle-latest N` to
push the last N uploads instead. This is the mode the auto-installed
[timer](#running-on-a-schedule-timer) uses for a fully hands-off pipeline.

---

## Installing

You need **Python 3.10 or newer** and `pip`. Git is **not** required — pip
fetches gungnir (the one dependency) over plain HTTPS using stdlib `urllib`.

### Option A — ZIP download (no git needed)

1. Grab the ZIP from [the GitHub repo](https://github.com/HiroAlleyCat/wigle-to-wdgwars) (Code → Download ZIP) and unzip it.
2. From inside the unzipped folder:

```bash
python3 -m venv .venv          # required on Bookworm / Homebrew (PEP 668)
.venv/bin/pip install -r requirements.txt
.venv/bin/python wigle_to_wdgwars.py --help
```

### Option B — clone with git

```bash
git clone https://github.com/HiroAlleyCat/wigle-to-wdgwars.git
cd wigle-to-wdgwars
python3 -m venv .venv          # required on Bookworm / Homebrew (PEP 668)
.venv/bin/pip install -r requirements.txt
.venv/bin/python wigle_to_wdgwars.py --help
```

> Raspberry Pi OS Bookworm, Debian 12+, Ubuntu 23.04+, and Homebrew Python
> all enforce PEP 668 and block `pip install` against the system Python.
> The `.venv/` step above is the safe path. If `python3 -m venv` itself
> errors out, install the venv module first:
> `sudo apt install -y python3-venv python3-full`.

### Windows

It runs on Windows exactly the same way — it's plain Python, no Linux-only
bits.

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) and
   **tick "Add python.exe to PATH"** in the installer. (Or grab it from the
   Microsoft Store.)
2. Download and unzip the repo into a folder, e.g. `C:\Tools\wigle-to-wdgwars\`
   (or `git clone` it if you have git).
3. Open PowerShell or Command Prompt in that folder and use `python` (not
   `python3`):

```powershell
python -m pip install -r requirements.txt
python wigle_to_wdgwars.py --whoami --key YOUR_API_KEY_HERE
python wigle_to_wdgwars.py my-wardrive.wiglecsv.gz --key YOUR_API_KEY_HERE --chunk-size 10000
```

For a hands-off scheduled push on Windows, see
[Running on a schedule → Windows](#windows--task-scheduler).

### Updating

The easiest path is `--update`, which does both steps for you:

```bash
./run.sh --update
```

That runs `git pull --ff-only` when this is a git checkout, otherwise
fetches `wigle_to_wdgwars.py` and `requirements.txt` from raw GitHub
atomically. Either way it then refreshes the venv deps, so a release
that bumps the gungnir pin self-heals without you having to remember
the second step.

If you'd rather do it by hand (which is what older releases told you
to do):

```bash
git pull             # or: re-download the ZIP and overwrite the folder
.venv/bin/pip install --upgrade -r requirements.txt
```

If you skip the second line on a dep-bump release, you'll end up with
new code importing the old gungnir bytes, which is a recipe for subtle
parity bugs.

### Where the API keys are read from (in order)

**WDGoWars** (`--key` / `$WDGWARS_API_KEY` / `wdgwars.key`):

1. `--key YOUR_KEY` on the command line.
2. `$WDGWARS_API_KEY` environment variable.
3. `~/.config/wigle-to-wdgwars/wdgwars.key` (mode 600).

**WiGLE** (`--wigle-key` / `$WIGLE_API_KEY` / `wigle.key`, used by `--from-wigle`):

1. `--wigle-key YOUR_TOKEN` on the command line.
2. `$WIGLE_API_KEY` environment variable.
3. `~/.config/wigle-to-wdgwars/wigle.key` (mode 600).

`--setup` saves both as files for you. To save them non-interactively (for
provisioning from a script):

```bash
.venv/bin/python wigle_to_wdgwars.py --save-key       YOUR_WDGWARS_KEY
.venv/bin/python wigle_to_wdgwars.py --save-wigle-key YOUR_WIGLE_TOKEN
```

> **Note on scheduled runs:** systemd / cron / schtasks run in a stripped-
> down environment that does *not* inherit `$WDGWARS_API_KEY` /
> `$WIGLE_API_KEY` from your shell. The scheduled command reads the key
> *files*. If you only export env vars, the timer will fail at run time.

The script also writes two state files in `~/.config/wigle-to-wdgwars/`:

| File | Purpose |
|---|---|
| `cooldown.json` | Persisted server-cooldown deadline. Set by 429 responses so a scheduled run an hour later still respects it. |
| `hwm.json` | High-water mark — last successful upload timestamp and import counts, for monitoring. Pure read-only output. |

---

## Getting a WiGLE CSV in the first place

If you've already been wardriving with the WiGLE Android app, skip to
[Option A](#option-a--wigle-android-app). Otherwise, here are the most
common paths.

### Option A — WiGLE Android app

The easiest entry point. Install
[WiGLE WiFi Wardriving](https://play.google.com/store/apps/details?id=net.wigle.wigleandroid)
from the Play Store, give it location + Bluetooth permissions, and do a run.
Afterwards you can hand the tool the export either way:

- **Database → Export to CSV** gives you a plain `WigleWifi_yyyyMMddHHmmss.csv`.
- The **share / upload** flow gives you a gzipped `*.wiglecsv.gz` (a single
  compressed file, sometimes with no inner file extension).

You do **not** need to unzip the `.gz` by hand. This tool detects gzip and
decompresses it for you, so just point it at whichever file you have:

```bash
# plain CSV
./run.sh WigleWifi_20260523120000.csv --chunk-size 10000

# the gzipped export works too — no unzipping needed
./run.sh my-run.wiglecsv.gz --chunk-size 10000
```

If you want BLE included, make sure WiGLE's Bluetooth scanning is enabled
in settings before the drive.

### Option B — Kismet + `kismetdb_to_wiglecsv`

If you already capture with [Kismet](https://www.kismetwireless.net/), the
official conversion tool ships with it:

```bash
kismetdb_to_wiglecsv \
    --in /var/log/kismet/Kismet-20260523.kismet \
    --out wardrive.csv
./run.sh wardrive.csv --chunk-size 10000
```

### Option C — hcxdumptool + `hcxpcapngtool`

If you run [hcxdumptool](https://github.com/ZerBea/hcxdumptool), pipe the
pcapng through `hcxpcapngtool --csv=...`:

```bash
hcxpcapngtool --csv=wardrive.csv capture.pcapng
./run.sh wardrive.csv --chunk-size 10000
```

### Option D — Roll your own

The WiGLE-1.6 CSV format is two header lines followed by data rows. The
columns are:

```
MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type
```

`Type` is `WIFI`, `BLE`, or `GSM` (only WIFI/BLE are honored by WDGoWars).
The first header line is a meta comment that WiGLE writes; the tool
preserves both header lines when chunking.

Minimal example:

```csv
WigleWifi-1.6,appRelease=v0.0.0
MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type
aa:bb:cc:dd:ee:ff,ExampleSSID,[WPA2-PSK-CCMP][ESS],2026-05-23 12:00:00,6,-55,41.0,-81.0,200,10,WIFI
```

---

## Running on a schedule (timer)

The point of a leaderboard is showing up consistently. Instead of pushing by
hand every time, set a timer and forget it.

**Fastest path — let the tool install the timer for you.** `--schedule` writes
the right artifact for your OS (systemd user unit on Linux-with-systemd, cron
entry on Mac / Linux-without-systemd, scheduled task on Windows). Defaults to
`--from-wigle` daily at 03:00 with `--chunk-size 10000`, in dry-run mode the
first time so the first tick decodes and logs but never POSTs.

```bash
.venv/bin/python wigle_to_wdgwars.py --schedule          # interactive
.venv/bin/python wigle_to_wdgwars.py --schedule \
    --schedule-time 03:00 --schedule-chunk-size 10000 \
    --schedule-dry-run                                   # headless

.venv/bin/python wigle_to_wdgwars.py --unschedule        # remove later
```

The interactive mode previews the exact unit/cron-line/schtasks command before
installing, and asks one last "install now?" confirmation. Re-run `--schedule`
and answer "no" to the dry-run prompt to flip from dry-run to live uploads.

If you'd rather write the unit / cron entry / scheduled task yourself, the
hand-written recipes below still work and they all stay supported. They give
you finer control (file-watch mode, custom intervals, multiple drives) than
the `--schedule` auto-installer.

**The truly hands-off version:** use `--from-wigle` (see
[No file at all](#no-file-at-all--pull-straight-from-wigle)). The timer pulls
your latest WiGLE upload and pushes it to WDGoWars with no file involved at
all. Swap the command in any recipe below for:

```
./run.sh --from-wigle --wigle-key WIGLE_TOKEN --key WDGWARS_KEY --chunk-size 10000
```

**The file-based version:** always export (or save) your WiGLE file to the
*same path* — e.g. `wardrive.wiglecsv.gz` — and point a timer at that path.
Each run re-pushes the file; WDGoWars dedupes server-side, so re-sending the
same data is harmless and still picks up any new rows or merged location
samples. Pick the recipe for your OS below.

### Windows — Task Scheduler

Easiest if you wardrive with your phone and copy the export to your PC. Save a
tiny batch file, then point Task Scheduler at it.

`push-wardrive.bat` (edit the paths and paste your key after `--key`):

```bat
@echo off
python "C:\Tools\wigle-to-wdgwars\wigle_to_wdgwars.py" "C:\Wardrives\wardrive.wiglecsv.gz" --key YOUR_API_KEY_HERE --chunk-size 10000 >> "C:\Wardrives\push.log" 2>&1
```

Create the timer (run once in an **admin** PowerShell or Command Prompt — this
fires it daily at 3am):

```powershell
schtasks /Create /F /TN "WDGoWars Push" /TR "C:\Wardrives\push-wardrive.bat" /SC DAILY /ST 03:00
```

(`/F` lets you re-run the same line later to change the time without an
overwrite prompt.)

To change the time, run the same `schtasks /Create` again with a new `/ST`, or
edit it in the Task Scheduler GUI (search "Task Scheduler" in the Start menu →
find "WDGoWars Push").

### cron (Linux / Mac) — push every 6 hours

```cron
# m h dom mon dow command
0 */6 * * * /usr/bin/env python3 /home/me/bin/wigle_to_wdgwars.py /home/me/wardrives/latest.csv --chunk-size 10000 >> /home/me/wardrives/push.log 2>&1
```

Point it at whatever file you keep fresh (a `.csv` or `.gz` both work). The
tool persists cooldown state to `~/.config/wigle-to-wdgwars/cooldown.json`, so
back-to-back jobs that catch a 429 won't hammer the server.

### systemd timer — daily at 03:00

`~/.config/systemd/user/wdgwars-push.service`:

```ini
[Unit]
Description=Push wardrive CSV to WDGoWars

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 %h/bin/wigle_to_wdgwars.py %h/wardrives/latest.csv --chunk-size 10000
```

`~/.config/systemd/user/wdgwars-push.timer`:

```ini
[Unit]
Description=Daily WDGoWars push

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wdgwars-push.timer
```

### Pre-flight check

Wrap the push in a `--whoami` check so a bad/expired key fails loudly
before you try a long upload:

```bash
#!/bin/sh
set -e
./run.sh --whoami > /dev/null
exec ./run.sh /home/me/wardrives/latest.csv --chunk-size 10000
```

### Parser preview

Before you wire a CSV path into a schedule (or push something big you
just got out of Kismet / hcxdumptool), it's worth confirming the parser
sees what you expect. `--preview` does that without any network calls:

```bash
./run.sh --preview /path/to/your.wiglecsv
```

Prints the first 6 data rows as JSON to stdout, no upload, no key
needed. Same shape as Heimdall's and Muninn's `--preview` so the
mental model carries between feeders.

### Pointing at a staging host

`--api-url` overrides the CSV upload endpoint. Useful when you're
testing against a local mock or staging server without flipping
`/etc/hosts`:

```bash
./run.sh --api-url http://localhost:9999/api/upload-csv \
         --dry-run /path/to/your.wiglecsv
```

Aircraft JSON uploads still use the signed `/endpoint/upload/` endpoint
unchanged — if you need to redirect those, use Muninn's `--api-url`.

---

## WDGoWars API reference

> **There are no official public docs for the WDGoWars API.** The table
> below was reverse-engineered from network captures and from the
> open-source uploaders that already work against the portal (see
> [Related tools](#related-tools)). It is accurate as of late May 2026;
> if it drifts, open an issue.

### Endpoints

| Method | Path | Purpose | Auth | Body |
|---|---|---|---|---|
| `GET` | `/api/me` | Validate key, read stats/badges/gang | `X-API-Key: <key>` | — |
| `POST` | `/api/upload-csv` | Bulk Wi-Fi/BLE ingest | `X-API-Key: <key>` | `multipart/form-data`, field `file=` (WiGLE-1.6 CSV) |
| `POST` | `/api/upload/` | Signed JSON ingest (aircraft, mesh, …) | `X-API-Key: <key>` | `application/json` envelope, see below |

**Auth header is `X-API-Key`.** `Authorization: Bearer …` is rejected.

### `GET /api/me` response

```json
{
  "ok": true,
  "username": "your_handle",
  "gang": "Your Gang",
  "gang_id": 1,
  "country": "US",
  "joined": "2026-01-01",
  "wifi": 1234,
  "ble": 5678,
  "aircraft": 0,
  "mesh": 0,
  "cracked": 0,
  "total": 6912,
  "recent_today": 100,
  "recent_7d": 900,
  "badges": ["first_blood", "gang_member", "wifi_100", "wifi_1k", "ble_100", "ble_1k"],
  "credits": {"balance": 0, "lifetime_earned": 0}
}
```

### `POST /api/upload-csv` response

```json
{
  "ok": true,
  "imported": 701,
  "captured": 1,
  "updated": 0,
  "duplicates": 56673,
  "no_gps": 0,
  "bad_rows": 3,
  "cooldown": 0,
  "merged_samples": 156,
  "total": 48421278
}
```

- `imported` — new fingerprints accepted into the user's account.
- `captured` — newly-flagged "first to capture" wins (rare).
- `duplicates` — rows the server has already seen from this user.
- `no_gps` — rows skipped for missing lat/lon.
- `bad_rows` — malformed rows the parser rejected.
- `merged_samples` — observations folded into an existing fingerprint as
  additional signal samples.
- `total` — **server-wide** row count across all users (not the caller's).
- `cooldown` — when nonzero, seconds the server is asking the client to
  wait before the next upload.

### Rate limiting

The server enforces a **per-account upload queue**. While one upload is
still being processed, a second request returns HTTP 429:

```json
{"error":"Another upload is already being processed for this account. Please wait for it to finish before starting a new one.","retry_after":20}
```

This tool persists `retry_after` to `~/.config/wigle-to-wdgwars/cooldown.json`
and sleeps until the deadline on the next run (capped at 15 min to avoid
deadlocks if a stale deadline sticks).

### The Cloudflare 524 footgun

The origin behind the portal processes each CSV **synchronously in one
request**. Cloudflare in front has a **120-second response timeout**.
Anything taking longer returns:

```
HTTP 524 — origin_response_timeout
```

to your client, but **the origin keeps ingesting** — you'll see the rows
land in your `/api/me` count even though your client errored.

**Mitigation:** chunk the CSV into ≤10 000-row chunks. Each chunk lands in
15–35 s comfortably under the cap. This tool does it automatically with
`--chunk-size 10000`. Each chunk re-sends the WiGLE 2-line header so the
server treats it as a valid file.

### Common error responses

| HTTP | Body | Meaning |
|---|---|---|
| 400 | `{"error":"Invalid data format"}` | Most likely you POSTed a CSV to `/api/upload` (no `-csv` suffix). Wrong endpoint, not a malformed file. |
| 401 | `{"error":"Invalid API key"}` | Bad/expired key, or you used `Authorization: Bearer …` instead of `X-API-Key:`. |
| 429 | `{"error":"Another upload is already being processed …","retry_after":N}` | Per-account queue. Wait `retry_after` seconds. |
| 524 | (HTML from Cloudflare) | Origin timed out. Chunk smaller. Rows are still ingesting on the origin. |

### WiGLE API (the `--from-wigle` pull side)

`--from-wigle` reads your own uploads back out of WiGLE, then feeds them into
the WDGoWars push above. The WiGLE side uses HTTP Basic auth with the
**pre-encoded token** from [wigle.net/account](https://wigle.net/account) (the
"Encoded for use" string), sent as `Authorization: Basic <token>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/file/transactions?pagestart=N&pageend=M` | List your uploads, newest first, paged 100 at a time. Each result has a `transid`. |
| `GET` | `/api/v2/file/csv/{transid}` | Download that upload as a WiGLE CSV. |

The tool lists the newest `--wigle-latest N` transactions and downloads each as
CSV. This mirrors the contract used by the community tool
[joelkoen/wigledl](https://github.com/joelkoen/wigledl). WiGLE enforces its own
per-account query limits, so pulling your whole history in one run can hit a
rate cap — pulling the latest upload (the default) stays well under it.

---

## Aircraft JSON format (signed endpoint)

The signed `/api/upload/` endpoint accepts a different payload shape for
aircraft, mesh, and (likely future) other observation types. Use
`--aircraft-json FILE` when you have ADS-B data to push.

### Envelope

The wire format wraps a payload in an HMAC-SHA256 envelope:

```json
{
  "data": "<base64(json(payload))>",
  "nonce": "<random hex>",
  "sig":  "<hex hmac_sha256(api_key, nonce + data)>"
}
```

Sent as `Content-Type: application/json`, with the same `X-API-Key` header
as the CSV path.

### Payload

The inner payload (pre-base64) is:

```json
{
  "networks": [],
  "aircraft": [ {<record>}, {<record>}, ... ],
  "meshcore_nodes": []
}
```

`networks` and `meshcore_nodes` are currently passed empty by this tool —
Wi-Fi/BLE goes through the CSV path because of better dedup and merging
behavior server-side.

### Aircraft record schema

```json
{
  "icao": "A12345",
  "callsign": "UAL123",
  "lat": 41.4712,
  "lon": -81.7887,
  "alt_ft": 35000,
  "speed_kt": 450,
  "heading": 270,
  "first_seen": "2026-05-23 12:00:00",
  "type": "ADSB"
}
```

`icao` and at least one of (`lat`, `lon`) are required. `first_seen` should
be `YYYY-MM-DD HH:MM:SS` in UTC. Missing fields are tolerated; bad fields
silently get zeroed.

### Input file

Pass a JSON file containing a top-level **list** of these record dicts:

```bash
./run.sh --aircraft-json aircraft.json
```

If you want a full-featured ADS-B uploader that auto-detects 12 capture
formats (dump1090 JSON, SBS-1, Mode-S Beast, GDL-90, etc.) and produces
this JSON for you, use **[Muninn (adsb-to-wdgwars)](https://github.com/HiroAlleyCat/adsb-to-wdgwars)**
instead. This tool's aircraft mode is intended for cases where you already
have records in this shape (e.g. exported from your own pipeline).

### Response

```json
{
  "ok": true,
  "aircraft_imported": 47,
  "aircraft_already_seen": 1203,
  "new_badges": ["plane_hunter"]
}
```

---

## Troubleshooting

**`{"error":"Invalid data format"}`** — You hit `/api/upload` (signed) with
a CSV. The CSV endpoint is `/api/upload-csv`. This tool uses the right
endpoint by default; only hits when something rewrites the URL.

**`HTTP 401`** — Bad key, or you set `Authorization: Bearer …` somewhere.
Run `--whoami` to confirm. Make sure your key is the full string from the
WDGoWars account page, no extra whitespace.

**`HTTP 429` repeating forever** — Your previous upload is still queued
server-side. Wait the `retry_after` seconds (the tool does this for you on
the next run). If a stale `cooldown.json` is causing >15 min sleeps, delete
it: `rm ~/.config/wigle-to-wdgwars/cooldown.json`.

**`HTTP 524`** — Cloudflare gave up waiting on the origin. Add or lower
`--chunk-size` (try 5000 if 10000 still trips it on a slow link). Your
data is probably ingesting anyway — check `--whoami` counts after.

**`imported: 0, duplicates: <huge>`** — Expected on the second push of the
same CSV. WDGoWars dedupes per-fingerprint. Only new BSSIDs/SSIDs (or new
locations for existing ones) count.

**`bad_rows: <nonzero>`** — Some rows didn't parse. Most often missing or
malformed `FirstSeen`, or a non-numeric `Lat`/`Lon`. Validate with:

```bash
awk -F, 'NR>2 && (length($1)!=17 || $7+0==0) {print NR": "$0}' wardrive.csv
```

**Script hangs on a chunk for minutes** — The origin is grinding through a
large chunk. urlopen timeout is 600 s in this tool. If you want to bail
out and let the origin finish in the background, Ctrl-C and check
`--whoami` 30–60 s later.

---

## Related tools

The wardriving + WDGoWars ecosystem of uploaders:

| Tool | Platform | Path | Repo |
|---|---|---|---|
| **wigle-to-wdgwars** (this) | Linux/Mac/Win (Python) | Wi-Fi + BLE CSV, aircraft JSON | (this repo) |
| **Muninn (adsb-to-wdgwars)** | Linux/Mac/Win (Python) + browser | ADS-B aircraft, 12 capture formats | https://github.com/HiroAlleyCat/adsb-to-wdgwars |
| **Piglet** | Arduino / RP2040 | Wi-Fi from on-device captures | https://github.com/Hamspiced/piglet |
| **Raspyjack `wdgwars_upload`** | Bash Bunny / Pi payload | CSV from Raspyjack payloads | https://github.com/7h30th3r0n3/Raspyjack |
| **pineapple_pager_wdgwars** | Wi-Fi Pineapple | Pineapple captures | https://github.com/LOCOSP/pineapple_pager_wdgwars |
| **M5MonsterC5 / CardputerADV** | M5Stack ESP32 | On-device captures | https://github.com/C5Lab/M5MonsterC5-CardputerADV |

Cross-cutting links:

- [WiGLE](https://wigle.net/) — the original wardriving network.
- [WiGLE WiFi Wardriving (Android)](https://play.google.com/store/apps/details?id=net.wigle.wigleandroid) — easiest capture stack.
- [Kismet](https://www.kismetwireless.net/) — the open-source wireless detector / sniffer / IDS.
- [hcxdumptool](https://github.com/ZerBea/hcxdumptool) — fast 802.11 capture for handshake hunting; pairs with `hcxpcapngtool --csv`.

---

## License

MIT. Use it, fork it, send a PR.

---

## Acknowledgments

The reverse-engineered API documentation here was cross-checked against
the open-source uploaders in the [Related tools](#related-tools) table —
in particular `Hamspiced/piglet` and `7h30th3r0n3/Raspyjack`. The
chunking-around-Cloudflare-524 workaround is well-documented across the
community; this tool just bakes it in by default.

WDGoWars is run by its community. If you upload a lot, consider joining a
gang and helping the leaderboard stay weird.
