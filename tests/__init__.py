"""wigle-to-wdgwars test suite.

Safety net: refuse to start the test process if a live WDGoWars API
key (or WiGLE token) is configured at the canonical wigle-to-wdgwars
paths. Tests that exercise upload paths read these the same way
production runs do — a stray test invocation can post synthetic data
to LOCOSP's production endpoint. Same shape as Muninn's guard, added
after the 2026-06-01 phantom-aircraft incident there.

To run tests with a real key present (sacrificial account on purpose):

    WIGLE_TEST_ALLOW_LIVE_KEY=1 python -m unittest discover tests/

The guard runs once at import time and only flags the canonical key
paths. Env vars (WDGWARS_API_KEY / WIGLE_API_TOKEN) and CLI flags are
out of scope — those require explicit caller intent.

Note: this is a saved-key guard, not a network blocker. test_setup.py
has its own per-test urlopen patcher that catches accidental network
calls; the two safety nets cover different threat models.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path


def _key_paths() -> list[Path]:
    """Return the canonical wigle-to-wdgwars saved-key paths.

    Mirrors wigle_to_wdgwars.py's CONFIG_DIR WITHOUT importing the
    module (which depends on gungnir, may not be present in a minimal
    CI environment). The production paths are POSIX-shaped on every OS
    — even on Windows the tool reads from ~/.config/wigle-to-wdgwars/
    rather than APPDATA — so this guard matches that exactly. If
    production ever moves to XDG_CONFIG_HOME or APPDATA, update both
    here and in CONFIG_DIR together.
    """
    cfg_dir = Path.home() / ".config" / "wigle-to-wdgwars"
    return [
        cfg_dir / "wdgwars.key",
        cfg_dir / "wigle.key",
    ]


def _check_live_key_guard() -> None:
    if os.environ.get("WIGLE_TEST_ALLOW_LIVE_KEY") == "1":
        return
    found = [p for p in _key_paths() if p.exists()]
    if not found:
        return
    paths_block = "\n".join(f"   {p}" for p in found)
    aside_block = "\n".join(
        f"     mv {p} {p}.bak" for p in found
    )
    sys.stderr.write(
        "\n"
        "================================================================\n"
        " wigle-to-wdgwars test suite: live key(s) detected, refusing\n"
        " to run.\n"
        "================================================================\n"
        f" Found:\n{paths_block}\n"
        "\n"
        " Tests that exercise upload paths will read these and post\n"
        " synthetic data to LOCOSP's production endpoint. This guard\n"
        " exists to keep test runs from polluting your real account.\n"
        "\n"
        " To run tests anyway (sacrificial account on purpose):\n"
        "\n"
        "     WIGLE_TEST_ALLOW_LIVE_KEY=1 python -m unittest discover tests/\n"
        "\n"
        " To run tests with no key risk, move the key(s) aside first:\n"
        "\n"
        f"{aside_block}\n"
        "================================================================\n"
        "\n"
    )
    sys.exit(2)


_check_live_key_guard()
