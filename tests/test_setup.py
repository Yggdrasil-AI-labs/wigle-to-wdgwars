"""Setup-wizard tests.

Exercises the validators and the file-write helpers WITHOUT hitting
live wdgwars.pl or api.wigle.net. Every test that would otherwise
make a network call monkeypatches gungnir's whoami path and the
WiGLE _wigle_get helper.

The interactive_setup() walk-through itself isn't unit-tested here,
it's driven by stdin prompts that are awkward to mock cleanly and is
better covered by manual release verification. The pieces it composes
(validators + key savers) are tested instead.

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
