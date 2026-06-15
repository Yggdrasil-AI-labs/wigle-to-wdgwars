"""Tests for the processed-transid skip gate introduced in v1.6.0.

The daily --from-wigle pull records each successfully pushed WiGLE upload
in a state file and skips re-downloading it on the next run (WiGLE
regenerates each CSV server-side, so re-pulling one already on WDGoWars
wastes minutes). A transid is recorded only on a real, non-dry-run
success; --reprocess ignores the state entirely.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wigle_to_wdgwars as w2w


class ProcessedTransidTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "processed-transids.json"
        self._patch = mock.patch.object(w2w, "PROCESSED_FILE", self.state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_load_empty_when_missing(self):
        self.assertEqual(w2w._load_processed_transids(), set())

    def test_mark_then_load_roundtrip(self):
        w2w._mark_transid_processed("20260612-01443")
        w2w._mark_transid_processed("20260613-00001")
        self.assertEqual(w2w._load_processed_transids(),
                         {"20260612-01443", "20260613-00001"})
        data = json.loads(self.state.read_text())
        self.assertEqual(data["processed"],
                         ["20260612-01443", "20260613-00001"])

    def test_skips_already_processed_without_downloading(self):
        w2w._mark_transid_processed("T1")
        with mock.patch.object(w2w, "wigle_list_transactions", return_value=["T1"]), \
             mock.patch.object(w2w, "wigle_download_csv") as dl, \
             mock.patch.object(w2w, "upload_csv_bytes") as up:
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 0)
        dl.assert_not_called()
        up.assert_not_called()

    def test_marks_on_real_success(self):
        with mock.patch.object(w2w, "wigle_list_transactions", return_value=["T2"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=b"x"), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=0):
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 0)
        self.assertIn("T2", w2w._load_processed_transids())

    def test_dry_run_does_not_mark(self):
        with mock.patch.object(w2w, "wigle_list_transactions", return_value=["T3"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=b"x"), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=0):
            w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=True,
                chunk_rows=10000, cooldown_sec=0)
        self.assertNotIn("T3", w2w._load_processed_transids())

    def test_failed_upload_not_marked(self):
        with mock.patch.object(w2w, "wigle_list_transactions", return_value=["T4"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=b"x"), \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=1):
            rc = w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=False,
                chunk_rows=10000, cooldown_sec=0)
        self.assertEqual(rc, 1)
        self.assertNotIn("T4", w2w._load_processed_transids())

    def test_reprocess_ignores_state(self):
        w2w._mark_transid_processed("T5")
        with mock.patch.object(w2w, "wigle_list_transactions", return_value=["T5"]), \
             mock.patch.object(w2w, "wigle_download_csv", return_value=b"x") as dl, \
             mock.patch.object(w2w, "upload_csv_bytes", return_value=0):
            w2w.pull_from_wigle_push_to_wdgwars(
                "tok", "key", "file", latest=1, dry_run=False,
                chunk_rows=10000, cooldown_sec=0, reprocess=True)
        dl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
