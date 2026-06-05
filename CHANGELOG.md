# Changelog

All notable changes to wigle-to-wdgwars are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/) and the
project uses [Semantic Versioning](https://semver.org/).

## [1.4.0] - 2026-06-05 - 15 MB upload cap (HTTP 413 auto-bisect)

LOCOSP rolled out a temporary 15 MB body cap on every wdgwars.pl upload
endpoint on 2026-06-05. The cap is a workaround for the CloudLinux LVE
on the shared host killing the PHP worker mid-buffer on bodies above
roughly 20 MB. Until the planned host migration lands (~2 weeks), the
server returns a structured 413 envelope with `max_bytes` + `received`
instead of a generic 500.

This release reacts to that envelope automatically. The proactive
`--chunk-size` flag is unchanged and remains the recommended path for
scheduled cron uploads, but standalone CLI runs are now resilient too.

### Added

- HTTP 413 auto-bisect. When a chunk comes back with
  `{error: payload-too-large, max_bytes, received}`, the offending
  chunk is split in half (row-count, header preserved on both halves)
  and both halves are pushed back onto the work queue. Recursion
  bottoms out cleanly when a chunk is one row and still 413 (recorded
  as a failure, other chunks continue).
- `_halve_chunk(bytes) -> tuple[bytes, bytes] | None` helper. Public
  enough for sibling tools (Muninn, Heimdall) to vendor if they want
  the same behavior; semantics match `_split_bytes`.
- `tests/test_413_autosplit.py`: 7 tests covering the halver, single-
  bisect success, double-bisect when first halves still oversize,
  one-row-still-413 failure recording, and forward-compat for a
  future `max_bytes` change (no hard-coded 15 MB constant).

### Changed

- `_upload_chunks` reworked from a fixed-list `for` loop to a
  `collections.deque` so the bisect path can push retries back onto
  the front of the queue without breaking the iteration counter.
  Behavior on 200/429/non-JSON paths is byte-for-byte identical to
  v1.3.0.
- `--chunk-size --help` text now describes the auto-bisect behavior
  so the flag's role is clear: proactive splitting to avoid the
  Cloudflare 524 timeout window, with the 413 path as a safety net.

## [1.3.0] - 2026-06-03 - Family-parity catch-up

Closes the flag-surface gaps between wigle-to-wdgwars and its sibling
feeders (Muninn, Heimdall). The five flags ported below were already
shipping in the other two uploaders; wigle-to-wdgwars is now at full
flag parity with the family.

### Added

- `--update`: in-place self-update. Uses `git pull --ff-only` when this
  is a git checkout, otherwise fetches `wigle_to_wdgwars.py` and
  `requirements.txt` from raw GitHub atomically. Refreshes deps via
  `python -m pip install --upgrade -r requirements.txt` after the swap,
  so a future release bumping a pinned dep self-heals. Mirrors the
  Muninn/Heimdall implementation byte-for-byte modulo naming.
- `--preview`: parser dry-run. Prints the first 6 data rows of the
  WiGLE CSV as JSON-lines to stdout and exits. Decompresses gzip
  transparently. No network, no upload. Mirrors Heimdall and Muninn
  `--preview` for cross-tool consistency.
- `-q` / `--quiet`: suppress informational banners. Errors still
  print. Gates the daily version-check nudge alongside
  `--no-version-check`.
- `--no-version-check`: skip the daily GitHub releases probe entirely.
  Useful for offline or privacy-conscious setups, and for CI.
- `--api-url URL`: override the CSV upload endpoint. Useful for
  staging hosts or local mocks. The signed aircraft-JSON endpoint
  (`/endpoint/upload/`) is unchanged — override there belongs in
  Muninn.
- Version-check banner: daily-cached probe of the GitHub releases
  API. Prints a non-blocking nudge if a newer tag is available.
  Cache lives at `~/.config/wigle-to-wdgwars/version-check.json`.
- `SECURITY.md`: ported from Heimdall; documents the network
  footprint, key-handling, and HMAC envelope. Updated to reflect
  the current v1.3.0 surface (the Heimdall original was stale).

### Tests

- `tests/test_family_parity.py`: 10 new unit tests covering
  `preview_csv` (gzip, short files, empty data sections),
  `_check_for_update` cache hits, `--api-url` runtime override of
  the module-level ENDPOINT, and `--update` / `--preview` dispatch
  in `main()`. Network is mocked throughout.

### Notes

- `--watch` is intentionally NOT added. WiGLE CSV uploads are
  bulk one-shot by nature; the polling equivalent for this feeder
  is `--from-wigle --schedule`, which pulls fresh transactions
  from `api.wigle.net` on a daily timer. If you have a continuous
  stream of incoming CSV files, Muninn (ADS-B) and Heimdall
  (meshcore) are the watch-shaped tools.

## [1.2.1] - 2026-06-03 - Family-alignment housekeeping

Patch release surfacing the lessons from the 2026-06-03 feeder-family
alignment audit. No behavior changes for end users; safety nets and a
gungnir-pin refresh that prevents tests from accidentally polluting a
real WDGoWars account.

### Added

- Saved-key test guard (`tests/__init__.py`). Refuses to start the
  test process if a real WDGoWars API key OR WiGLE token is present
  at the canonical paths. Override with
  `WIGLE_TEST_ALLOW_LIVE_KEY=1`. Complements the existing per-test
  `_NetworkBlockedCase` urlopen-blocker — the two cover different
  threat models (saved-key side vs network side).
- `scripts/check_readme_examples.py` — README linter that catches
  `python3 wigle_to_wdgwars.py ...` examples drifting outside the
  venv-teaching blocks. Ported from Muninn after the 2026-06-01 Pi24
  user hit `ModuleNotFoundError: No module named 'gungnir'`
  following the README literally. Auto-detects the entrypoint script.

### Changed

- gungnir pin bumped `v0.1.1 → v0.1.2`. Picks up the Cloudflare-L7
  bypass default URL (`/endpoint/upload/`) that ships in gungnir
  v0.1.2 — every fresh `setup.sh` install now hits the rate-limit-safe
  endpoint by default.
- Six README code-block examples rewritten from `python3
  wigle_to_wdgwars.py ...` to `./run.sh ...` so users following the
  guide hit the venv-aware shim, not the system Python.

### Deferred

- `-q` / `--quiet` and `--no-version-check` parity flags from the
  audit. Both require backing implementation (a print-suppression
  pass and a version-check feature respectively); they're feature
  work, not alignment work, so they're out of scope here.

## [1.2.0] - 2026-06-02 - Guided setup + auto-installed daily timer

Brings wigle-to-wdgwars to parity with Muninn's hand-off-able install flow.
Before this release, getting from a fresh clone to a recurring daily push
meant: read the install section, make a venv, paste two keys somewhere
sensible, copy a systemd unit out of the README, edit the paths, run
`systemctl --user enable --now`. Five steps with five places to get wrong.
Now it's one command.

### Added

- `--setup` interactive wizard. Prompts for the WDGoWars API key,
  validates it against `/api/me`, saves to `~/.config/wigle-to-wdgwars/wdgwars.key`
  with mode 600. Then prompts for the WiGLE "Encoded for use" token,
  validates by listing one transaction, saves to `wigle.key` mode 600.
  Then offers to install a daily timer (next bullet). Each step is
  independently skippable. Re-runnable to rotate keys.
- `--save-key KEY` / `--save-wigle-key TOKEN`. Non-interactive single-key
  saves, for provisioning from a script.
- `--schedule` auto-installer. Writes the right artifact for the host:
  systemd user `.service` + `.timer` on Linux-with-systemd, cron line on
  Mac / Linux-without-systemd, schtasks entry on Windows. Defaults:
  daily at 03:00, `--from-wigle --wigle-latest 1 --chunk-size 10000`,
  dry-run on first install. Interactive when invoked alone; headless
  with `--schedule-time` / `--schedule-chunk-size` / `--schedule-dry-run`.
- `--unschedule` removes every wigle-to-wdgwars-managed schedule entry
  on the host. Marker comments (`# managed-by-wigle-to-wdgwars`) in
  every artifact let the uninstaller find them cleanly without
  clobbering anything else in the user's crontab or systemd unit dir.
- Bootstrap shim scripts: `setup.sh` / `setup.bat` / `run.sh` /
  `run.bat` / `update.sh` / `update.bat`. Mirror Muninn's
  double-clickable pattern. `setup` does venv + deps + `--setup`;
  `run` runs the default daily push (or forwards arbitrary args);
  `update` pulls fresh `requirements.txt` + `wigle_to_wdgwars.py`.
- Test suite. `tests/test_scheduler.py` covers the pure systemd / cron
  / schtasks renderers (no side effects, no live calls).
  `tests/test_setup.py` covers the file-write helpers, key validators,
  and 600-mode enforcement, with `urllib.request.urlopen` blocked at
  the test-class level to catch any accidental live call. 45 tests,
  runs offline.
- `scripts/smoke.sh`. End-to-end pre-release check: throwaway venv +
  pinned-dep install + unit tests + `--help` / `--version` + on Linux,
  a `--schedule --schedule-dry-run` against an XDG-isolated `HOME`
  that asserts the unit artifact contains the marker, the dry-run flag,
  and **does not** contain `--key` or `--wigle-key` (regression net
  against accidentally baking secrets into the unit file).
- `--version` flag. Reports `wigle-to-wdgwars 1.2.0`.

### Security

- API keys never appear in the installed unit file / cron line /
  schtasks action. The scheduled command relies on the saved key files
  at `~/.config/wigle-to-wdgwars/*.key` (mode 600) being readable at
  run time. `--key` / `--wigle-key` are still accepted on the CLI for
  ad-hoc one-off pushes but are not baked into anything persistent.
  Smoke test asserts this. Tests assert this.
- `_write_secret_file` refuses to write through a symlink, opens the
  file with mode 600 from the start (no umask race), and chmods on
  POSIX after the fact for belt-and-suspenders. Windows skips the
  chmod test — the file lives under the user's profile, which is ACL'd
  to the user.

### Notes for existing users

- Existing key files under `~/.config/wigle-to-wdgwars/` keep working
  unchanged. `--setup` notices an existing key and asks before
  replacing it.
- The CLI flags + env vars + on-disk paths from v1.1.x are unchanged.
  Old recipes still run as written.
- Hand-written systemd units / cron lines from the README still work
  and are still documented. The README now mentions `--schedule` as
  the fast path and keeps the hand-written recipes as the
  fine-control fallback.

## [1.1.2] - 2026-06-01 - README install: PEP 668 / Bookworm fix

README install snippets (Option A ZIP, Option B git, Updating section)
now use a project-local `.venv/` instead of `python3 -m pip install`
against the system Python.

On Raspberry Pi OS Bookworm, Debian 12+, Ubuntu 23.04+, and Homebrew
Python, the previous `python3 -m pip install -r requirements.txt` line
errored out with `error: externally-managed-environment` (PEP 668).
Users could work around it with `--break-system-packages` or by
creating a venv themselves, but the README directed them straight
into the wall.

Found by sweeping the feeder family after a Pi24 user reported the
same crash in Muninn's `setup.sh` ([adsb-to-wdgwars#15](https://github.com/HiroAlleyCat/adsb-to-wdgwars/pull/15)).
Wigle has no wrapper scripts to fix — the install instructions live in
the README only.

Windows install block is unchanged. Windows Python has no PEP 668
enforcement, so `python -m pip install` still works there.

### Fixed

- README "Option A — ZIP download" and "Option B — clone with git"
  now show `python3 -m venv .venv` followed by `.venv/bin/pip install`
  and `.venv/bin/python wigle_to_wdgwars.py`.
- "Updating" section uses `.venv/bin/pip install --upgrade` to match.
- Inline note explains which distros enforce PEP 668 and what to do
  if `python3 -m venv` itself errors out (missing `python3-venv` apt
  package).

## [1.1.1] - 2026-05-29 - Fix install path for users without git

v1.1.0 introduced a `requirements.txt` with the gungnir dependency
pinned via `gungnir @ git+https://github.com/...@v0.1.1`. That URL
form forces pip to shell out to `git clone`, which fails on any
machine without git on PATH (a common state for casual ZIP
downloaders, especially on Windows).

The README also still claimed "There's nothing to install. The script
is a single file, depends only on Python 3.10+, and uses `urllib`
from stdlib — no `pip install`." That was true in v1.0 but became
false the moment gungnir was added. New ZIP-download users following
the README hit `ModuleNotFoundError: No module named 'gungnir'`.

### Fixed

- **`requirements.txt` no longer requires `git` on the user's PATH.**
  The gungnir pin switched from
  `gungnir @ git+https://github.com/HiroAlleyCat/gungnir@v0.1.1`
  to
  `gungnir @ https://github.com/HiroAlleyCat/gungnir/archive/refs/tags/v0.1.1.tar.gz`
  — pip fetches the tarball over plain HTTPS with stdlib `urllib`.
  Same v0.1.1 tag, same gungnir bytes, same reproducibility.

### Documentation

- README's tagline and **Installing** section now correctly state that
  this is a one-dependency tool (gungnir), document `pip install -r
  requirements.txt`, and offer a no-git ZIP-download path alongside
  `git clone`.

- New **Updating** subsection explaining that release bumps may move
  the gungnir pin, so `git pull` (or re-extracting the ZIP) must be
  paired with `pip install --upgrade -r requirements.txt`.

## [1.1.0] — extract signed-JSON transport to gungnir

Structural refactor. **Multipart CSV upload (`/api/upload-csv`) and the
WiGLE-side fetch are unchanged** — they stay as local code because
gungnir doesn't handle multipart and doesn't speak the WiGLE API. The
**signed-JSON path (`/api/upload/`)** moves to
[gungnir](https://github.com/HiroAlleyCat/gungnir) 0.1.1, the same
library Muninn v2.0 sits on.

### Changed

- **`upload_aircraft_json()` default `batch` lowered 1000 → 500** per
  locosp's recommendation (100-500 per request). The CLI flag
  `--aircraft-batch` default follows suit.
- **`whoami()` output moved from stdout to structured stderr logs.**
  v1.0 printed raw JSON; v1.1 emits a `key OK — user=X` line plus a
  `wifi=… ble=… aircraft=… total=…` summary line. Cron jobs that
  parsed the raw JSON need to update.
- **`_post_signed()` is now a thin shim over `gungnir.transport
  .send_chunk()`**. Status return is `200` on success or `1` on any
  failure — the exact HTTP code for non-2xx is no longer surfaced
  (gungnir handles 5xx with retry + backoff internally).

### Added

- `__version__ = "1.1.0"` exposed at module top.
- New `requirements.txt` pinning gungnir to v0.1.1.

### Improved (free wins from gungnir 0.1.x)

- **Retry 5xx + network errors** with exponential backoff (3 attempts,
  2s/4s). v1.0 failed the upload on the first transient hiccup.
- **429 stops the batch and persists a cooldown.** v1.0 already had a
  cooldown file (the pattern gungnir borrowed) but kept POSTing after
  a 429 until the chunk loop finished. v1.1 stops immediately.
- **Silent-drop detection.** HTTP 200 ok:true with every counter zero
  now returns rc=1. v1.0 had no detection at all.
- **Inter-chunk cooldown of 1s** (was 2s hard-coded between aircraft
  chunks; the new default is shorter but configurable via the gungnir
  Client).
- **User-Agent now includes the repo URL** per RFC bot-UA convention:
  `wigle-to-wdgwars/1.1.0 (+https://github.com/HiroAlleyCat/wigle-to-wdgwars)`.

### Compatibility

- **API key file unchanged.** Local `load_key()` still reads from
  `~/.config/wigle-to-wdgwars/wdgwars.key`. The decision to keep this
  path *outside* gungnir's per-OS convention is deliberate — Windows
  users of v1.0 stored the key under `~/.config/` (Linux-style) and
  we don't want to break their install.
- **`cooldown.json` and `hwm.json` paths** now follow gungnir's per-OS
  convention. On POSIX this is byte-identical to v1.0
  (`~/.config/wigle-to-wdgwars/cooldown.json`). On Windows the files
  move to `%APPDATA%/wigle-to-wdgwars/`. Both files are ephemeral so
  the migration is harmless.
- **Wire-protocol unchanged.** gungnir's envelope is byte-identical to
  the v1.0 hand-rolled envelope (verified by gungnir's parity test
  against Muninn v1.11.1, which used the same code).

### Migration

```
pip install -r requirements.txt  # pulls gungnir from the pinned tag
```

No config-file changes. Cron stanzas keep working.

## [1.0.0] — initial release

### Added
- CLI tool to upload WiGLE 1.6 CSVs to the WDGoWars wardriving
  leaderboard. Supports both the multipart `/api/upload-csv` (for
  Wi-Fi + BLE observations) and the signed `/api/upload/` (for
  aircraft records).
- `--from-wigle` mode: pull your latest WiGLE upload via the WiGLE
  API and forward it to WDGoWars without staging a local file.
- API-key resolution via `--key`, `$WDGWARS_API_KEY`, or
  `~/.config/wigle-to-wdgwars/wdgwars.key`.
- Cooldown persistence (`cooldown.json`) so 429 deadlines survive
  cron restarts.
- HWM tracking (`hwm.json`) for external monitoring.
- Chunked CSV upload to dodge the Cloudflare 524 timeout on large
  imports.
