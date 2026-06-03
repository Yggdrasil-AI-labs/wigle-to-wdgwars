#!/usr/bin/env bash
# Pre-release smoke test for wigle-to-wdgwars. Runs in CI and locally.
#
# Exercises the contained, deterministic parts of the install path:
#   1. README example linter (catches venv-form drift like the Muninn
#      v2.0.8 footgun the Pi24 user hit).
#   2. Throwaway venv + pinned-dep install (matches setup.sh flow).
#   3. AST parse + import sanity.
#   4. Unit tests (offline — every test mocks the network).
#   5. wigle_to_wdgwars.py --version + --help sanity.
#   6. --schedule headless renders a unit file with --dry-run + marker,
#      in an XDG-isolated home. systemctl is allowed to fail (no live
#      user manager in CI) — we only assert on the artifact.
#
# Live `--schedule` install against the real systemd user manager is
# NOT part of this script — that belongs in a pre-release manual
# checklist with a sacrificial key.
#
# Run from the repo root:   bash scripts/smoke.sh
# Exit: 0 all pass, 1 any failure (fail-fast).

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d -t wigle-smoke-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM

say()  { printf "[smoke] %s\n" "$*"; }
fail() { printf "[smoke] FAIL: %s\n" "$*" >&2; exit 1; }
ok()   { printf "[smoke] ok: %s\n" "$*"; }

cd "$REPO_DIR"

# ─── 1. README example linter (stdlib-only, runs without deps) ───
say "linting README examples..."
if python3 scripts/check_readme_examples.py README.md > "$TMP_DIR/lint.log" 2>&1; then
    ok "README clean"
else
    cat "$TMP_DIR/lint.log" >&2
    fail "README linter"
fi

# ─── 2. throwaway venv + pinned deps ───
say "creating throwaway venv at $TMP_DIR/venv..."
if ! python3 -m venv "$TMP_DIR/venv" > "$TMP_DIR/venv.log" 2>&1; then
    cat "$TMP_DIR/venv.log" >&2
    fail "venv create (is python3-venv installed?)"
fi
if [ -x "$TMP_DIR/venv/bin/python" ]; then
    VENV_PY="$TMP_DIR/venv/bin/python"
elif [ -x "$TMP_DIR/venv/Scripts/python.exe" ]; then
    VENV_PY="$TMP_DIR/venv/Scripts/python.exe"
else
    fail "could not find venv python interpreter under $TMP_DIR/venv/"
fi
say "installing pinned deps into venv..."
if ! "$VENV_PY" -m pip install -q -r requirements.txt \
        > "$TMP_DIR/pip.log" 2>&1; then
    tail -20 "$TMP_DIR/pip.log" >&2
    fail "pip install -r requirements.txt"
fi
ok "venv + deps"

# ─── 2. parse + import sanity ───
say "AST parse + module import..."
"$VENV_PY" -c "import ast; ast.parse(open('wigle_to_wdgwars.py').read())" \
    || fail "ast parse"
"$VENV_PY" -c "import wigle_to_wdgwars; print('version', wigle_to_wdgwars.__version__)" \
    || fail "module import"
ok "parse + import"

# ─── 3. unit tests via venv python ───
say "running unit tests..."
if "$VENV_PY" -m unittest discover tests/ > "$TMP_DIR/tests.log" 2>&1; then
    ok "tests passed"
else
    tail -30 "$TMP_DIR/tests.log" >&2
    fail "unit tests"
fi

# ─── 4. CLI sanity through the venv python ───
say "wigle_to_wdgwars.py --version..."
VER=$("$VENV_PY" wigle_to_wdgwars.py --version 2>&1 | head -1) \
    || fail "--version"
say "  $VER"
"$VENV_PY" wigle_to_wdgwars.py --help > /dev/null || fail "--help"
ok "--version + --help"

# ─── 5. --schedule headless: write unit file to a temp XDG and assert ───
# Linux only. macOS gets cron; CI runs Linux so we focus there.
if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1 \
        && [ -d /run/systemd/system ]; then
    say "rendering systemd unit (no install) — XDG-isolated..."
    # Need a saved WiGLE key file present, but it never gets read in
    # --schedule mode (the value goes through the unit's ExecStart at
    # runtime, not at install time). Use a sacrificial placeholder.
    export HOME="$TMP_DIR/home"
    mkdir -p "$HOME/.config/wigle-to-wdgwars"
    echo "PLACEHOLDER" > "$HOME/.config/wigle-to-wdgwars/wigle.key"
    export XDG_CONFIG_HOME="$HOME/.config"
    # Suppress systemctl errors — unit file is written BEFORE the call.
    "$VENV_PY" wigle_to_wdgwars.py --schedule \
        --schedule-time 03:00 \
        --schedule-chunk-size 10000 \
        --schedule-dry-run > "$TMP_DIR/sched.log" 2>&1 || true
    UNIT="$XDG_CONFIG_HOME/systemd/user/wigle-to-wdgwars.service"
    TIMER="$XDG_CONFIG_HOME/systemd/user/wigle-to-wdgwars.timer"
    if [ ! -f "$UNIT" ]; then
        cat "$TMP_DIR/sched.log" >&2
        fail "no service file written to $UNIT"
    fi
    if [ ! -f "$TIMER" ]; then
        cat "$TMP_DIR/sched.log" >&2
        fail "no timer file written to $TIMER"
    fi
    grep -q "Description=wigle-to-wdgwars daily push \[DRY-RUN\]" "$UNIT" \
        || fail "dry-run marker missing from service Description"
    grep -q -- "--from-wigle .* --chunk-size 10000 --dry-run" "$UNIT" \
        || fail "ExecStart missing expected flags"
    grep -q "# managed-by-wigle-to-wdgwars" "$UNIT" \
        || fail "marker comment missing from service"
    grep -q "OnCalendar=\*-\*-\* 03:00:00" "$TIMER" \
        || fail "OnCalendar missing from timer"
    # CRITICAL: ensure no API key ever lands in the unit file
    grep -q -- "--key " "$UNIT" \
        && fail "WDGoWars key leaked into service ExecStart"
    grep -q -- "--wigle-key " "$UNIT" \
        && fail "WiGLE token leaked into service ExecStart"
    ok "unit + timer content correct (dry-run + marker + flags + no key leak)"
else
    say "(skipping systemd unit smoke — not on a systemd Linux host)"
fi

say "all smoke checks passed"
exit 0
