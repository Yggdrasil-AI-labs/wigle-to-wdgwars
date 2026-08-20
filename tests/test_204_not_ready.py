"""Tests for WiGLE's "CSV not ready yet" answer (issue #8).

While an upload sits in WiGLE's processing queue the CSV endpoint answers
HTTP 204 (or 200 with a zero-byte body). A busy queue can hold an upload
for days, so that is a "come back later", not a failure: the download
returns None, the pull loop skips that transid, and the rest of the batch
still goes to WDGWars. Before this, one queued upload sys.exit(1)'d the
whole scheduled run.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wigle_to_wdgwars as w2w

from ._helpers import csv_with_rows


class DownloadNotReadyTests(unittest.TestCase):
    def test_204_returns_none_instead_of_exiting(self):
        with mock.patch.object(w2w, "_wigle_get", return_value=(204, b"")):
            self.assertIsNone(w2w.wigle_download_csv("tok", "T1"))

    def test_200_with_zero_byte_body_returns_none(self):
        with mock.patch.object(w2w, "_wigle_get", return_value=(200, b"")):
            self.assertIsNone(w2w.wigle_download_csv("tok", "T1"))

    def test_200_with_whitespace_only_body_returns_none(self):
        with mock.patch.object(w2w, "_wigle_get", return_value=(200, b"\r\n")):
            self.assertIsNone(w2w.wigle_download_csv("tok", "T1"))

    def test_not_ready_is_not_retried_within_the_call(self):
        """A queued CSV should cost one query, not two: no timeout retry."""
        get = mock.Mock(return_value=(204, b""))
        with mock.patch.object(w2w, "_wigle_get", get):
            w2w.wigle_download_csv("tok", "T1")
        self.assertEqual(get.call_count, 1)

    def test_real_csv_still_returned(self):
        body = csv_with_rows(3)
        with mock.patch.object(w2w, "_wigle_get", return_value=(200, body)):
            self.assertEqual(w2w.wigle_download_csv("tok", "T1"), body)

    def test_other_error_status_still_fatal(self):
        with mock.patch.object(w2w, "_wigle_get", return_value=(500, b"boom")):
            with self.assertRaises(SystemExit):
                w2w.wigle_download_csv("tok", "T1")


class PullLoopNotReadyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "processed-transids.json"
        self._patch = mock.patch.object(w2w, "PROCESSED_FILE", self.state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_queued_upload_is_skipped_and_batch_still_succeeds(self):
        """Issue #8: 5 pending uploads, the newest still queued at WiGLE."""
        def fake_dl(_tok, tid):
            return None if tid == "T1" else b"x"

        with mock.patch.object(w2w, "wigle_list_transactions",
                               return_value=["T1", "T2", "T3"]), \
             mock.patch.object(w2w, "wigle_download_csv", side_effect=fake_dl), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=0) as up:
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=3, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 0)
        self.assertEqual(up.call_count, 2)

    def test_queued_upload_is_not_marked_processed(self):
        """It must come back on the next run once WiGLE finishes building it."""
        with mock.patch.object(w2w, "wigle_list_transactions",
                               return_value=["T1"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=None), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=0) as up:
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 0)
        up.assert_not_called()
        self.assertEqual(w2w._load_processed_transids(), set())

    def test_all_queued_exits_zero(self):
        """The systemd unit must not report FAILURE when nothing was ready."""
        with mock.patch.object(w2w, "wigle_list_transactions",
                               return_value=["T1", "T2"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=None), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=0):
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=2, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 0)

    def test_a_real_upload_failure_still_fails(self):
        with mock.patch.object(w2w, "wigle_list_transactions",
                               return_value=["T1"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=b"x"), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=1):
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
