"""Scheduler renderer tests.

Pure-function tests for the systemd / cron / schtasks renderers added
in v1.2.0. No side effects: never writes a real unit, never touches
crontab, never invokes schtasks. The installer functions (which DO
touch the system) are exercised manually during release verification.

Run: python -m unittest tests.test_scheduler
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wigle_to_wdgwars as w  # noqa: E402


PY = "/usr/bin/python3"
SCRIPT = Path("/opt/wigle-to-wdgwars/wigle_to_wdgwars.py")


class TimeValidation(unittest.TestCase):

    def test_accepts_valid_times(self):
        self.assertEqual(w._validate_hhmm("03:00"), "03:00")
        self.assertEqual(w._validate_hhmm("00:00"), "00:00")
        self.assertEqual(w._validate_hhmm("23:59"), "23:59")

    def test_zero_pads(self):
        self.assertEqual(w._validate_hhmm("3:5"), "03:05")
        self.assertEqual(w._validate_hhmm("9:00"), "09:00")

    def test_rejects_bad(self):
        for bad in ("24:00", "12:60", "abc", "3", "12:00:00", ""):
            with self.assertRaises(ValueError, msg=bad):
                w._validate_hhmm(bad)


class SystemdRendererTests(unittest.TestCase):

    def test_writes_both_service_and_timer(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT)
        self.assertIn("service", units)
        self.assertIn("timer", units)
        self.assertTrue(units["service"])
        self.assertTrue(units["timer"])

    def test_service_is_oneshot(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT)
        self.assertIn("Type=oneshot", units["service"])

    def test_service_includes_from_wigle_flags(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT)
        self.assertIn("--from-wigle", units["service"])
        self.assertIn("--wigle-latest 1", units["service"])
        self.assertIn("--chunk-size 10000", units["service"])

    def test_service_omits_from_wigle_when_disabled(self):
        units = w.render_systemd_units("03:00", False, 10000, PY, SCRIPT)
        self.assertNotIn("--from-wigle", units["service"])
        self.assertIn("--chunk-size 10000", units["service"])

    def test_timer_has_oncalendar_with_seconds(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT)
        # systemd OnCalendar wants HH:MM:SS; we always emit :00 seconds.
        self.assertIn("OnCalendar=*-*-* 03:00:00", units["timer"])
        self.assertIn("Persistent=true", units["timer"])

    def test_timer_respects_custom_time(self):
        units = w.render_systemd_units("14:30", True, 10000, PY, SCRIPT)
        self.assertIn("OnCalendar=*-*-* 14:30:00", units["timer"])

    def test_marker_present_for_uninstall(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT)
        self.assertIn(w.SCHEDULE_MARKER, units["service"])
        self.assertIn(w.SCHEDULE_MARKER, units["timer"])

    def test_dry_run_baked_into_exec_start(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT,
                                       dry_run=True)
        self.assertIn("--dry-run", units["service"])
        self.assertIn("[DRY-RUN]", units["service"])

    def test_live_omits_dry_run(self):
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT,
                                       dry_run=False)
        self.assertNotIn("--dry-run", units["service"])
        self.assertNotIn("[DRY-RUN]", units["service"])

    def test_no_api_keys_in_unit(self):
        """Critical: API keys never get baked into the unit file. They live
        in ~/.config/wigle-to-wdgwars/*.key and are read at run-time."""
        units = w.render_systemd_units("03:00", True, 10000, PY, SCRIPT)
        for flag in ("--key", "--wigle-key", "X-API-Key"):
            self.assertNotIn(flag, units["service"],
                             f"{flag} must NOT appear in unit file")

    def test_bad_time_raises(self):
        with self.assertRaises(ValueError):
            w.render_systemd_units("25:99", True, 10000, PY, SCRIPT)


class CronRendererTests(unittest.TestCase):

    def test_default_3am(self):
        line = w.render_cron_line("03:00", True, 10000, PY, SCRIPT)
        self.assertTrue(line.startswith("0 3 * * *"),
                        f"unexpected cron start: {line!r}")

    def test_custom_time(self):
        line = w.render_cron_line("14:30", True, 10000, PY, SCRIPT)
        self.assertTrue(line.startswith("30 14 * * *"),
                        f"unexpected cron start: {line!r}")

    def test_includes_python_and_script(self):
        line = w.render_cron_line("03:00", True, 10000, PY, SCRIPT)
        self.assertIn(PY, line)
        self.assertIn(str(SCRIPT), line)

    def test_includes_marker_for_uninstall(self):
        line = w.render_cron_line("03:00", True, 10000, PY, SCRIPT)
        self.assertIn(w.SCHEDULE_MARKER, line)

    def test_logs_to_homedir(self):
        line = w.render_cron_line("03:00", True, 10000, PY, SCRIPT)
        self.assertIn("$HOME/.wigle-to-wdgwars-cron.log", line)

    def test_dry_run_baked_in(self):
        line = w.render_cron_line("03:00", True, 10000, PY, SCRIPT,
                                  dry_run=True)
        self.assertIn("--dry-run", line)

    def test_no_api_keys_in_cron_line(self):
        line = w.render_cron_line("03:00", True, 10000, PY, SCRIPT)
        for flag in ("--key", "--wigle-key"):
            self.assertNotIn(flag, line,
                             f"{flag} must NOT appear in cron line")


class SchtasksRendererTests(unittest.TestCase):

    def test_create_argv_shape(self):
        cmd = w.render_schtasks_create("03:00", True, 10000, PY, SCRIPT)
        self.assertEqual(cmd[0], "schtasks")
        self.assertIn("/Create", cmd)
        self.assertIn("/TN", cmd)
        self.assertIn(w.WINDOWS_TASK_NAME, cmd)
        self.assertIn("/SC", cmd)
        self.assertIn("DAILY", cmd)
        self.assertIn("/ST", cmd)
        self.assertIn("03:00", cmd)
        self.assertIn("/F", cmd)  # force overwrite of same-named task

    def test_action_includes_flags(self):
        cmd = w.render_schtasks_create("03:00", True, 10000, PY, SCRIPT)
        tr_idx = cmd.index("/TR")
        action = cmd[tr_idx + 1]
        self.assertIn("--from-wigle", action)
        self.assertIn("--chunk-size 10000", action)

    def test_action_within_261_char_schtasks_limit(self):
        """schtasks /TR hard-caps at 261 chars. Use a realistic long
        venv-python path to make sure we stay under it. (Earlier attempt
        to wrap the action in `cmd /c "... >> log 2>&1"` for stdout
        capture pushed past this limit on standard install paths and was
        rolled back.)"""
        realistic_py = Path(r"C:\Users\someuser\AppData\Local\Temp\wigle-to-wdgwars\.venv\Scripts\python.exe")
        realistic_script = Path(r"C:\Users\someuser\AppData\Local\Temp\wigle-to-wdgwars\wigle_to_wdgwars.py")
        cmd = w.render_schtasks_create("03:00", True, 10000,
                                       str(realistic_py), realistic_script,
                                       dry_run=True)
        action = cmd[cmd.index("/TR") + 1]
        self.assertLessEqual(len(action), 261,
                             f"action length {len(action)} exceeds schtasks "
                             f"/TR limit. action={action!r}")

    def test_action_quotes_paths_with_spaces(self):
        space_script = Path(r"C:\Program Files\wigle-to-wdgwars\wigle_to_wdgwars.py")
        cmd = w.render_schtasks_create("03:00", True, 10000,
                                       r"C:\Python313\python.exe",
                                       space_script)
        action = cmd[cmd.index("/TR") + 1]
        self.assertIn('"C:\\Program Files\\wigle-to-wdgwars\\wigle_to_wdgwars.py"',
                      action)

    def test_dry_run_baked_in(self):
        cmd = w.render_schtasks_create("03:00", True, 10000, PY, SCRIPT,
                                       dry_run=True)
        action = cmd[cmd.index("/TR") + 1]
        self.assertIn("--dry-run", action)

    def test_no_api_keys_in_schtasks_action(self):
        cmd = w.render_schtasks_create("03:00", True, 10000, PY, SCRIPT)
        action = cmd[cmd.index("/TR") + 1]
        for flag in ("--key", "--wigle-key"):
            self.assertNotIn(flag, action,
                             f"{flag} must NOT appear in schtasks action")


class ShellQuoteTests(unittest.TestCase):

    def test_passes_simple(self):
        self.assertEqual(w._shell_quote("foo"), "foo")
        self.assertEqual(w._shell_quote("foo-bar.baz"), "foo-bar.baz")
        self.assertEqual(w._shell_quote("/usr/bin/python3"), "/usr/bin/python3")

    def test_quotes_spaces(self):
        self.assertEqual(w._shell_quote("foo bar"), "'foo bar'")

    def test_quotes_empty(self):
        self.assertEqual(w._shell_quote(""), "''")

    def test_escapes_single_quote(self):
        # POSIX trick: end-quote, escape ', start-quote
        self.assertEqual(w._shell_quote("it's"), "'it'\"'\"'s'")


if __name__ == "__main__":
    unittest.main()
