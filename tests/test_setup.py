"""Setup-wizard tests.

Exercises the validators and the file-write helpers WITHOUT hitting
live wdgwars.pl or api.wigle.net. Every test that would otherwise
make a network call monkeypatches gungnir's whoami path and the
WiGLE _wigle_get helper.

The top-level interactive_setup() orchestration itself isn't unit
tested here, it's a thin sequence of calls to the two sub-steps below
plus interactive_schedule_setup, and is better covered by manual
release verification. The sub-steps it composes (_setup_wdgwars_key,
_setup_wigle_token, the validators, and the key savers) ARE tested,
by mocking the four seams each sub-step calls through
(_prompt_yes_no, _prompt_secret, check_whoami/check_wigle_token,
save_key/save_wigle_token) instead of driving real stdin.

Hard guard: if a test ever accidentally reaches the real network we
want it to fail loud, not silently. The setUp clobbers urllib's
urlopen so any un-mocked call raises immediately.

Run: python -m unittest tests.test_setup
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wigle_to_wdgwars as w  # noqa: E402


def _network_blocker(*_a, **_kw):  # pragma: no cover, guard
    raise AssertionError(
        "test made an un-mocked network call. Mock urlopen / _wigle_get / "
        "_client.whoami before exercising code that would hit wdgwars.pl "
        "or api.wigle.net."
    )


class _NetworkBlockedCase(unittest.TestCase):
    """Base: blocks live HTTP for the test's lifetime."""

    def setUp(self):
        patcher = mock.patch.object(urllib.request, "urlopen",
                                    side_effect=_network_blocker)
        patcher.start()
        self.addCleanup(patcher.stop)


class WriteSecretFileTests(_NetworkBlockedCase):

    def test_writes_value_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wdgwars.key"
            w._write_secret_file(p, "abc123")
            self.assertEqual(p.read_text(), "abc123\n")

    def test_strips_whitespace(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wdgwars.key"
            w._write_secret_file(p, "  abc123\n  ")
            self.assertEqual(p.read_text(), "abc123\n")

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nested" / "dir" / "wdgwars.key"
            w._write_secret_file(p, "abc")
            self.assertTrue(p.exists())
            self.assertEqual(p.read_text(), "abc\n")

    @unittest.skipIf(sys.platform == "win32",
                     "Windows ignores chmod 600; the user-profile ACL "
                     "is what restricts access there.")
    def test_mode_600_on_posix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wdgwars.key"
            w._write_secret_file(p, "abc")
            mode = p.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600,
                             f"expected mode 600, got {oct(mode)}")

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wdgwars.key"
            p.write_text("OLD\n")
            w._write_secret_file(p, "NEW")
            self.assertEqual(p.read_text(), "NEW\n")


class SaveKeyTests(_NetworkBlockedCase):
    """save_key + save_wigle_token route through _write_secret_file but
    additionally print a confirmation. The print is intentional UX,
    just confirm the file lands where load_*() will look."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.fake_config = Path(self.tmpdir) / ".config" / "wigle-to-wdgwars"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil as sh
        sh.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_key_writes_to_wdgwars_key(self):
        with mock.patch.object(w, "CONFIG_DIR", self.fake_config), \
             mock.patch.object(w, "DEFAULT_KEY_FILE",
                               self.fake_config / "wdgwars.key"):
            w.save_key("abc123")
            self.assertEqual(
                (self.fake_config / "wdgwars.key").read_text(),
                "abc123\n",
            )

    def test_save_wigle_token_writes_to_wigle_key(self):
        with mock.patch.object(w, "CONFIG_DIR", self.fake_config), \
             mock.patch.object(w, "WIGLE_KEY_FILE",
                               self.fake_config / "wigle.key"):
            w.save_wigle_token("xyz789")
            self.assertEqual(
                (self.fake_config / "wigle.key").read_text(),
                "xyz789\n",
            )


class CheckWigleTokenTests(_NetworkBlockedCase):

    def _patch_wigle_get(self, status, body):
        return mock.patch.object(w, "_wigle_get",
                                 return_value=(status, body))

    def test_200_with_success_true(self):
        with self._patch_wigle_get(
            200, b'{"success": true, "results": []}'
        ):
            self.assertEqual(w.check_wigle_token("token"), 0)

    def test_200_with_success_false_rejected(self):
        with self._patch_wigle_get(
            200, b'{"success": false, "message": "too many calls"}'
        ):
            self.assertEqual(w.check_wigle_token("token"), 1)

    def test_401_rejected(self):
        with self._patch_wigle_get(401, b'{"error": "bad token"}'):
            self.assertEqual(w.check_wigle_token("token"), 1)

    def test_500_rejected(self):
        with self._patch_wigle_get(500, b"server error"):
            self.assertEqual(w.check_wigle_token("token"), 1)

    def test_network_error_rejected(self):
        with mock.patch.object(w, "_wigle_get",
                               side_effect=urllib.error.URLError("boom")):
            self.assertEqual(w.check_wigle_token("token"), 1)

    def test_non_json_200_rejected(self):
        with self._patch_wigle_get(200, b"<html>error</html>"):
            self.assertEqual(w.check_wigle_token("token"), 1)


class SetupWdgwarsKeyTests(_NetworkBlockedCase):
    """_setup_wdgwars_key() drives the WDGWars half of the wizard. It
    only ever calls out to _prompt_yes_no / _prompt_secret / check_whoami
    / save_key, so each branch is exercised here by mocking those four
    seams instead of real stdin, no terminal interaction required."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.key_file = Path(self.tmpdir) / "wdgwars.key"
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmpdir, ignore_errors=True))
        self._patch_path = mock.patch.object(w, "DEFAULT_KEY_FILE",
                                             self.key_file)
        self._patch_path.start()
        self.addCleanup(self._patch_path.stop)

    def test_existing_key_declined_replace_counts_as_saved(self):
        self.key_file.write_text("old-key\n")
        with mock.patch.object(w, "_prompt_yes_no", return_value=False):
            self.assertEqual(w._setup_wdgwars_key(), 1)

    def test_declining_to_save_returns_zero(self):
        with mock.patch.object(w, "_prompt_yes_no", return_value=False):
            self.assertEqual(w._setup_wdgwars_key(), 0)

    def test_empty_input_then_cancel_returns_minus_one(self):
        # save? yes -> keep trying after empty input? yes -> then no.
        prompts = iter([True, True, False])
        with mock.patch.object(w, "_prompt_yes_no",
                               side_effect=lambda *a, **k: next(prompts)), \
             mock.patch.object(w, "_prompt_secret", return_value=""):
            self.assertEqual(w._setup_wdgwars_key(), -1)

    def test_rejected_key_then_cancel_returns_minus_one(self):
        # save? yes -> keep trying after reject? no.
        prompts = iter([True, False])
        with mock.patch.object(w, "_prompt_yes_no",
                               side_effect=lambda *a, **k: next(prompts)), \
             mock.patch.object(w, "_prompt_secret", return_value="bad-key"), \
             mock.patch.object(w, "check_whoami", return_value=1):
            self.assertEqual(w._setup_wdgwars_key(), -1)

    def test_valid_key_gets_saved(self):
        with mock.patch.object(w, "_prompt_yes_no", return_value=True), \
             mock.patch.object(w, "_prompt_secret", return_value="good-key"), \
             mock.patch.object(w, "check_whoami", return_value=0), \
             mock.patch.object(w, "save_key") as sk:
            self.assertEqual(w._setup_wdgwars_key(), 1)
            sk.assert_called_once_with("good-key")


class SetupWigleTokenTests(_NetworkBlockedCase):
    """Mirror of SetupWdgwarsKeyTests for the optional WiGLE half."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.key_file = Path(self.tmpdir) / "wigle.key"
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmpdir, ignore_errors=True))
        self._patch_path = mock.patch.object(w, "WIGLE_KEY_FILE",
                                             self.key_file)
        self._patch_path.start()
        self.addCleanup(self._patch_path.stop)

    def test_existing_token_declined_replace_counts_as_saved(self):
        self.key_file.write_text("old-token\n")
        with mock.patch.object(w, "_prompt_yes_no", return_value=False):
            self.assertEqual(w._setup_wigle_token(), 1)

    def test_declining_to_save_returns_zero(self):
        with mock.patch.object(w, "_prompt_yes_no", return_value=False):
            self.assertEqual(w._setup_wigle_token(), 0)

    def test_empty_input_then_cancel_returns_minus_one(self):
        prompts = iter([True, True, False])
        with mock.patch.object(w, "_prompt_yes_no",
                               side_effect=lambda *a, **k: next(prompts)), \
             mock.patch.object(w, "_prompt_secret", return_value=""):
            self.assertEqual(w._setup_wigle_token(), -1)

    def test_valid_token_gets_saved(self):
        with mock.patch.object(w, "_prompt_yes_no", return_value=True), \
             mock.patch.object(w, "_prompt_secret", return_value="good-token"), \
             mock.patch.object(w, "check_wigle_token", return_value=0), \
             mock.patch.object(w, "save_wigle_token") as swt:
            self.assertEqual(w._setup_wigle_token(), 1)
            swt.assert_called_once_with("good-token")


class ScheduleSetupNoWigleTests(_NetworkBlockedCase):
    """interactive_schedule_setup(have_wigle=False) returns early with a
    pointer to file-based scheduling, no prompts involved, so it needs
    no mocking beyond the network guard already in setUp."""

    def test_returns_zero_without_wigle_token(self):
        self.assertEqual(w.interactive_schedule_setup(have_wigle=False), 0)


class InteractiveSetupDispatchTests(_NetworkBlockedCase):
    """interactive_setup() orchestrates the two sub-steps above plus
    interactive_schedule_setup. Mock all three so this only checks the
    dispatch logic (which step runs, in what order, on what result),
    not the sub-steps' own behavior (covered by the classes above)."""

    def test_cancelled_wdgwars_key_returns_one_without_wigle_step(self):
        with mock.patch.object(w, "_setup_wdgwars_key",
                               return_value=-1) as wdg, \
             mock.patch.object(w, "_setup_wigle_token") as wigle:
            self.assertEqual(w.interactive_setup(), 1)
        wdg.assert_called_once()
        wigle.assert_not_called()

    def test_declined_wdgwars_key_skips_schedule_setup(self):
        with mock.patch.object(w, "_setup_wdgwars_key", return_value=0), \
             mock.patch.object(w, "_setup_wigle_token", return_value=0), \
             mock.patch.object(w, "interactive_schedule_setup") as sched:
            self.assertEqual(w.interactive_setup(), 0)
        sched.assert_not_called()

    def test_saved_key_runs_schedule_setup_with_wigle_flag(self):
        with mock.patch.object(w, "_setup_wdgwars_key", return_value=1), \
             mock.patch.object(w, "_setup_wigle_token", return_value=1), \
             mock.patch.object(w, "interactive_schedule_setup",
                               return_value=0) as sched:
            self.assertEqual(w.interactive_setup(), 0)
        sched.assert_called_once_with(have_wigle=True)

    def test_schedule_setup_keyboard_interrupt_is_swallowed(self):
        # A Ctrl+C in the nested wizard must not blow up interactive_setup;
        # the keys already saved before that point must stay saved.
        with mock.patch.object(w, "_setup_wdgwars_key", return_value=1), \
             mock.patch.object(w, "_setup_wigle_token", return_value=0), \
             mock.patch.object(w, "interactive_schedule_setup",
                               side_effect=KeyboardInterrupt):
            self.assertEqual(w.interactive_setup(), 0)


class CheckWhoamiDelegates(_NetworkBlockedCase):
    """check_whoami is a thin shim over gungnir's whoami. Make sure the
    delegation works and returns its rc."""

    def test_returns_zero_on_success(self):
        with mock.patch.object(w._client, "whoami", return_value=0):
            self.assertEqual(w.check_whoami("good-key"), 0)

    def test_returns_one_on_failure(self):
        with mock.patch.object(w._client, "whoami", return_value=1):
            self.assertEqual(w.check_whoami("bad-key"), 1)


if __name__ == "__main__":
    unittest.main()
