# Security review — findings & disposition

On **2026-06-21**, as part of bringing the WDGoWars feeder family onto a common
gated CI pipeline (pytest + coverage → SonarCloud → Snyk), `wigle_to_wdgwars.py`
was reviewed for the same classes of issue that SonarCloud's SAST flagged in the
sibling **adsb-to-wdgwars (Muninn)** repo — path traversal, command/argument
injection in scheduler artifacts, insecure temp-directory use, and unsafe
database opens.

**Outcome: no remediation needed.** Unlike Muninn (which carried 21 accepted
findings), wigle-to-wdgwars was already defended against every category. This
document records that review so the disposition is written down, and so that the
posture is now backed by regression tests ([`tests/test_security.py`](tests/test_security.py)).

## Why the Muninn finding classes don't apply here

| Muninn finding class | Status in wigle-to-wdgwars |
|---|---|
| **S2083** — path traversal into a watch state file | **N/A** — wigle has no watch mode and writes no state file. It uploads a CSV/JSON the operator names; it never derives a second path from a watched directory. |
| **S5443** — use of a publicly-writable / `/tmp` directory | **N/A** — no `tempfile`, `gettempdir`, or hardcoded `/tmp` path anywhere. Config lives under `~/.config/wigle-to-wdgwars/`. |
| **S8706** — SQLite connection built from a filename | **N/A** — wigle has no SQLite/`.sqb` support. |
| **S6350 / S8705** — command / OS-command argument from untrusted data | **Already defended** — the scheduler renderers (`render_systemd_units`, `render_cron_line`, `render_schtasks_create`) take only trusted inputs (`sys.executable`, `__file__`, a validated `HH:MM`, an int chunk size, and a bool). No value from `argv` reaches them. Every argv element is still passed through `_shell_quote()` for systemd/cron, and the time is validated by `_validate_hhmm()`. |
| **S8707 / S6549** — path construction from CLI args | **Accept-by-design** — the only CLI path inputs are the positional `csv` and `--aircraft-json FILE`. Both are **read-only** (`read_bytes`/`read_text`), guarded by an `is_file()` check, and chosen by the operator. As documented in `SECURITY.md`, this is a local operator CLI: there is no sandbox root to confine to. |

## Existing defenses this review confirmed (now under test)

- **Secrets never hit the command line.** `_schedule_argv()` deliberately omits
  `--key` / `--wigle-key`, so the API key and WiGLE token never land in a unit
  file, crontab line, or schtasks action (all readable by other local
  processes). Locked by `ScheduleArgvSecretTests`.
- **Secret files are written safely.** `_write_secret_file()` refuses to write
  through a symlink (dotfile-redirect defence), and creates the file with
  `O_CREAT | O_TRUNC` and mode `0o600` from the start (no world-readable race).
  Locked by `SecretFileTests`.
- **Scheduler arguments are shell-quoted.** `_shell_quote()` single-quotes any
  argument containing a shell metacharacter, and the renderers apply it to
  every argv element. Locked by `ShellQuotingTests` / `RendererQuotingTests`.
- **Schedule time is validated.** `_validate_hhmm()` rejects out-of-range and
  non-numeric input before it can reach a rendered command. Locked by
  `RendererQuotingTests.test_cron_time_is_validated`.

## A note for when SonarCloud is enabled

This repo is not yet imported into SonarCloud. Once it is (and the `SONAR_TOKEN`
/ `SNYK_TOKEN` secrets are added — see [CI.md](CI.md)), the scanner may still
raise **security hotspots** (review-required, not vulnerabilities) on the
read-only CLI path inputs and the `subprocess` calls in the installers. The
disposition above is the rationale to mark those *Safe* / *Accepted*: the inputs
are operator-controlled and trusted, no `shell=True` is used, and the scheduler
arguments are quoted.
