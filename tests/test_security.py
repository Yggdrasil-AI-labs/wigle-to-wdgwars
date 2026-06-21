"""Security-posture regression tests.

Unlike the sibling adsb-to-wdgwars (Muninn), wigle-to-wdgwars had no
SonarCloud SAST findings to remediate when its CI quality gate was added —
its scheduler renderers already shell-quote every argument, the schedule argv
deliberately keeps secrets off the command line, and the secret-file writer
refuses to follow a symlink and creates with mode 600. See SECURITY-FINDINGS.md
for the review write-up.

These tests LOCK IN that existing posture so a future refactor can't quietly
regress it. All tests are pure / filesystem-local: nothing uploads, installs a
real scheduler entry, or touches the network.

Run: WIGLE_TEST_ALLOW_LIVE_KEY=1 python -m unittest tests.test_security
"""
from __future__ import annotations
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wigle_to_wdgwars as w  # noqa: E402


PY = "/usr/bin/python3"
SCRIPT = Path("/opt/wigle-to-wdgwars/wigle_to_wdgwars.py")


class ShellQuotingTests(unittest.TestCase):
    """_shell_quote is what stands between an awkward path and a broken (or
    injectable) systemd/cron command line."""

    def test_plain_token_unquoted(self):
        self.assertEqual(w._shell_quote("aircraft.json"), "aircraft.json")
        self.assertEqual(w._shell_quote("/usr/bin/python3"), "/usr/bin/python3")

    def test_empty_becomes_quoted_empty(self):
        self.assertEqual(w._shell_quote(""), "''")

    def test_metacharacters_are_quoted(self):
        for bad in ("a;b", "a b", "a$(b)", "a`b`", "a|b", "a&b", "a>b"):
            q = w._shell_quote(bad)
            self.assertNotEqual(q, bad, f"{bad!r} must be quoted")
            self.assertTrue(q.startswith("'"), f"{bad!r} -> {q!r}")

    def test_embedded_single_quote_is_escaped(self):
        # The classic '"'"' break-out-and-back-in dance.
        q = w._shell_quote("a'b")
        self.assertEqual(q, "'a'\"'\"'b'")


class RendererQuotingTests(unittest.TestCase):
    """The renderers run every argv element through _shell_quote, so a path
    carrying a shell metacharacter can never appear as a bare token."""

    def test_cron_quotes_metachar_script_path(self):
        evil = Path("/opt/with;reboot/wigle_to_wdgwars.py")
        line = w.render_cron_line("03:00", False, 1000, PY, evil)
        self.assertIn(w._shell_quote(str(evil)), line)
        self.assertNotIn(f" {evil} ", line)  # never bare

    def test_systemd_quotes_metachar_script_path(self):
        evil = Path("/opt/with spaces/wigle_to_wdgwars.py")
        units = w.render_systemd_units("03:00", False, 1000, PY, evil)
        self.assertIn(w._shell_quote(str(evil)), units["service"])

    def test_cron_time_is_validated(self):
        # An out-of-range / junk time must be rejected before it reaches the
        # rendered line, not silently formatted in.
        for bad in ("24:00", "$(reboot)", "3:0;reboot", "abc"):
            with self.assertRaises(ValueError, msg=bad):
                w.render_cron_line(bad, False, 1000, PY, SCRIPT)


class ScheduleArgvSecretTests(unittest.TestCase):
    """The scheduled command must use the saved key files, never embed a key
    on the command line where it would land in the unit file / crontab /
    schtasks output (all readable by other processes)."""

    def test_argv_never_carries_key_flags(self):
        for use_from_wigle in (False, True):
            argv = w._schedule_argv(use_from_wigle, 5000)
            self.assertNotIn("--key", argv)
            self.assertNotIn("--wigle-key", argv)
            self.assertNotIn("--save-key", argv)
            self.assertNotIn("--save-wigle-key", argv)

    def test_from_wigle_flag_toggles(self):
        self.assertIn("--from-wigle", w._schedule_argv(True, 1))
        self.assertNotIn("--from-wigle", w._schedule_argv(False, 1))

    def test_rendered_units_contain_no_key_flags(self):
        units = w.render_systemd_units("03:00", True, 1000, PY, SCRIPT)
        line = w.render_cron_line("03:00", True, 1000, PY, SCRIPT)
        for blob in (units["service"], units["timer"], line):
            self.assertNotIn("--key", blob)
            self.assertNotIn("--wigle-key", blob)


class SecretFileTests(unittest.TestCase):
    """_write_secret_file is the only writer of the API key / WiGLE token."""

    def test_refuses_to_write_through_symlink(self):
        with tempfile.TemporaryDirectory() as d, \
                tempfile.TemporaryDirectory() as other:
            target = Path(other).resolve() / "real.key"
            link = Path(d).resolve() / "wdgwars.key"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError, AttributeError):
                self.skipTest("symlink creation not permitted on this host")
            with self.assertRaises(SystemExit):
                w._write_secret_file(link, "s3cret")
            # The symlink target must NOT have been written through.
            self.assertFalse(target.exists())

    def test_writes_content_stripped_with_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wdgwars.key"
            w._write_secret_file(p, "  s3cret  ")
            self.assertEqual(p.read_text(), "s3cret\n")

    @unittest.skipIf(os.name == "nt", "POSIX file mode not enforced on Windows")
    def test_mode_is_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wdgwars.key"
            w._write_secret_file(p, "s3cret")
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode & 0o077, 0,
                             f"secret file is group/other-accessible: {oct(mode)}")


if __name__ == "__main__":
    unittest.main()
