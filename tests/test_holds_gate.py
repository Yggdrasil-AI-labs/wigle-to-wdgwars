"""The already-sent gate: don't re-send rows the server has confirmed.

A cron pushing the same export every few minutes re-sends the same rows
every time. The server counts those as syncs that carried nothing new and
says so on the Uplink page. The mechanism is gungnir.holds, shared with
Muninn and heimdall; what is ours is the key and the two CSV filters
around it.

Since v1.8.0 the key is the observation (MAC + SSID + FirstSeen) and every
accepted row is held for ACCEPTED_TTL (30 days). The v1.7.x key was the
network alone, which suppressed re-scans the server scores, refines
position from, and uses to reset decay. What is pinned here:

- The same row pushed again is held; the same network seen at a new
  FirstSeen is not.
- A file with no FirstSeen column holds nothing.
- Every unreadable thing errs toward uploading.

Run: WIGLE_TEST_ALLOW_LIVE_KEY=1 python -m unittest tests.test_holds_gate
"""
from __future__ import annotations

import pathlib
import sys
import time
import unittest
from unittest import mock

import gungnir

import wigle_to_wdgwars as w2w
from tests._helpers import HEADER, csv_with_rows


FS = 3  # FirstSeen column in the fixture header
T = "2026-06-05 10:00:00"


class RowKeyTests(unittest.TestCase):
    def test_key_is_mac_ssid_and_firstseen(self):
        self.assertEqual(w2w._row_key(["aa:bb:cc", "CoffeeShop", "[WPA2]", T], FS),
                         f"AA:BB:CC|CoffeeShop|{T}")

    def test_a_rescan_is_a_different_key(self):
        # The point of v1.8.0: tomorrow's drive past the same network is
        # new data to the server (reinforce, position, decay reset).
        self.assertNotEqual(
            w2w._row_key(["aa:bb:cc", "Net", "", T], FS),
            w2w._row_key(["aa:bb:cc", "Net", "", "2026-06-06 09:00:00"], FS))

    def test_mac_case_is_folded_but_ssid_case_is_not(self):
        self.assertEqual(w2w._row_key(["AA:BB:CC", "x", "", T], FS),
                         w2w._row_key(["aa:bb:cc", "x", "", T], FS))
        self.assertNotEqual(w2w._row_key(["aa:bb:cc", "Net", "", T], FS),
                            w2w._row_key(["aa:bb:cc", "net", "", T], FS))

    def test_an_unreadable_row_has_no_key(self):
        # None means "always upload it".
        self.assertIsNone(w2w._row_key([], FS))
        self.assertIsNone(w2w._row_key(["", "ssid", "", T], FS))
        self.assertIsNone(w2w._row_key(["aa:bb:cc", "ssid", "", ""], FS))
        self.assertIsNone(w2w._row_key(["aa:bb:cc", "ssid"], FS))

    def test_no_firstseen_column_means_no_key(self):
        self.assertIsNone(w2w._row_key(["aa:bb:cc", "ssid", "", T], None))
        self.assertIsNone(w2w._firstseen_index("MAC,SSID,AuthMode,Channel"))
        self.assertEqual(w2w._firstseen_index("MAC,SSID,AuthMode,FirstSeen"), 3)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.csv = csv_with_rows(3)

    def _state_for(self, *suffixes):
        return {f"AA:BB:CC:00:00:{i:02X}|Net{i}|{T}": self.now + 999
                for i in suffixes}

    def test_held_rows_are_dropped_and_the_rest_kept(self):
        out, stats = w2w.filter_csv_held(self.csv, self._state_for(0, 2),
                                         self.now)
        self.assertEqual((stats["kept"], stats["dropped_held"],
                          stats["total"]), (1, 2, 3))
        self.assertIn(b"Net1", out)
        self.assertNotIn(b"Net0", out)

    def test_headers_survive_verbatim(self):
        out, _ = w2w.filter_csv_held(self.csv, self._state_for(0, 1, 2),
                                     self.now)
        self.assertTrue(out.startswith(HEADER))

    def test_an_expired_hold_does_not_drop_the_row(self):
        state = {f"AA:BB:CC:00:00:00|Net0|{T}": self.now - 1}
        _, stats = w2w.filter_csv_held(self.csv, state, self.now)
        self.assertEqual(stats["dropped_held"], 0)

    def test_an_unparseable_row_is_kept(self):
        # The opposite of the --since filter, deliberately. That one asks
        # "is this recent enough to send", where dropping what it cannot
        # read is conservative. This one asks "does the server already have
        # it", where the conservative answer is to send it.
        #
        # A field over the reader's 128k limit is the input that genuinely
        # raises. An unterminated quote does NOT: csv.reader parses it
        # happily, which is how the first version of this test managed to
        # pass without ever reaching the branch it claimed to cover.
        csv = HEADER + b"aa:bb:cc:00:00:99," + b"x" * 200_000 + b"\n"
        _, stats = w2w.filter_csv_held(csv, {}, self.now)
        self.assertEqual(stats["kept"], 1,
                         "a row we cannot parse must be uploaded, never "
                         "silently suppressed")

    def test_row_keys_round_trip(self):
        self.assertEqual(w2w.csv_row_keys(csv_with_rows(2)),
                         [f"AA:BB:CC:00:00:00|Net0|{T}",
                          f"AA:BB:CC:00:00:01|Net1|{T}"])

    def test_a_file_without_firstseen_holds_nothing(self):
        csv = csv_with_rows(2).replace(b"FirstSeen", b"Seen")
        self.assertEqual(w2w.csv_row_keys(csv), [])
        state = {f"AA:BB:CC:00:00:00|Net0|{T}": self.now + 999}
        _, stats = w2w.filter_csv_held(csv, state, self.now)
        self.assertEqual(stats["kept"], 2)


class ApplyHoldsTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def test_no_state_is_a_pass_through(self):
        csv = csv_with_rows(2)
        self.assertIs(w2w._apply_holds(csv, "x.csv", self.now), csv)

    def test_all_rows_held_returns_none_to_skip_the_upload(self):
        gungnir.holds.record_keys(w2w.HOLDS_TOOL,
                                  w2w.csv_row_keys(csv_with_rows(2)),
                                  self.now)
        self.assertIsNone(
            w2w._apply_holds(csv_with_rows(2), "x.csv", self.now))

    def test_a_partially_held_file_uploads_the_remainder(self):
        gungnir.holds.record_keys(w2w.HOLDS_TOOL,
                                  [f"AA:BB:CC:00:00:00|Net0|{T}"], self.now)
        out = w2w._apply_holds(csv_with_rows(2), "x.csv", self.now)
        self.assertIsNotNone(out)
        self.assertNotIn(b"Net0", out)
        self.assertIn(b"Net1", out)

    def test_an_old_gungnir_disables_the_gate_rather_than_failing(self):
        csv = csv_with_rows(2)
        gungnir.holds.record_keys(w2w.HOLDS_TOOL, w2w.csv_row_keys(csv),
                                  self.now)
        with mock.patch.object(w2w, "HOLDS_AVAILABLE", False):
            self.assertIs(w2w._apply_holds(csv, "x.csv", self.now), csv)


class PinAndSummaryHygieneTests(unittest.TestCase):
    """Two things a mutation run caught as untested."""

    def test_required_gungnir_matches_the_pin(self):
        # A guard that drifts from the pin it enforces is worse than none:
        # it would warn about the wrong version, or stay silent on the
        # right one. Muninn has the same assertion.
        req = (pathlib.Path(w2w.__file__).parent
               / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn(f"/v{w2w.REQUIRED_GUNGNIR}.tar.gz", req)



class RecordHoldsTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.csv = csv_with_rows(2)

    def _held_until(self):
        return gungnir.holds.load(w2w.HOLDS_TOOL)[f"AA:BB:CC:00:00:00|Net0|{T}"]

    def test_an_accepted_upload_is_held_for_thirty_days(self):
        w2w._record_holds(self.csv, self.now)
        self.assertEqual(self._held_until(),
                         self.now + gungnir.holds.ACCEPTED_TTL)

    def test_the_hold_expires(self):
        w2w._record_holds(self.csv, self.now)
        later = self.now + gungnir.holds.ACCEPTED_TTL + 1
        self.assertIs(w2w._apply_holds(self.csv, "x.csv", later), self.csv)

    def test_an_old_gungnir_records_nothing_and_does_not_raise(self):
        with mock.patch.object(w2w, "HOLDS_AVAILABLE", False):
            w2w._record_holds(self.csv, self.now)
        self.assertEqual(gungnir.holds.load(w2w.HOLDS_TOOL), {})


class EndToEndTests(unittest.TestCase):
    """Through upload_csv_bytes, which is where the wiring lives."""

    def _upload(self, csv, dry_run=False, rc=0):
        with mock.patch.object(w2w, "_upload_chunks", return_value=rc) as up:
            with mock.patch.object(w2w, "_cooldown_check_and_sleep"):
                out = w2w.upload_csv_bytes(csv, "x.csv", "K", "file",
                                           dry_run=dry_run)
        return out, up

    def test_a_repeat_upload_is_skipped_entirely(self):
        csv = csv_with_rows(2)
        _, first = self._upload(csv)
        self.assertEqual(first.call_count, 1)
        _, second = self._upload(csv)
        self.assertEqual(second.call_count, 0,
                         "the second push of an unchanged export must not "
                         "reach the transport")

    def test_one_new_row_sends_the_remainder(self):
        self._upload(csv_with_rows(2))
        _, up = self._upload(csv_with_rows(3))
        self.assertEqual(up.call_count, 1)
        sent = b"".join(up.call_args.args[0])
        self.assertIn(b"Net2", sent)
        self.assertNotIn(b"Net0", sent)

    def test_a_rescan_of_a_sent_network_still_goes_up(self):
        csv = csv_with_rows(2)
        self._upload(csv)
        rescan = csv.replace(b"2026-06-05 10:00:00", b"2026-06-06 09:00:00")
        _, up = self._upload(rescan)
        self.assertEqual(up.call_count, 1,
                         "a new sighting of a known network is new data")

    def test_a_failed_upload_records_nothing(self):
        csv = csv_with_rows(2)
        self._upload(csv, rc=1)
        self.assertEqual(gungnir.holds.load(w2w.HOLDS_TOOL), {})
        _, retry = self._upload(csv)
        self.assertEqual(retry.call_count, 1, "a failure must be retried")

    def test_dry_run_neither_consults_nor_records(self):
        csv = csv_with_rows(2)
        gungnir.holds.record_keys(w2w.HOLDS_TOOL, w2w.csv_row_keys(csv),
                                  time.time())
        _, up = self._upload(csv, dry_run=True)
        self.assertEqual(up.call_count, 1,
                         "a dry run must not be suppressed by holds")



class PerKeyAndResetTests(unittest.TestCase):
    """v1.9.0: holds are per API key, and --reset-holds clears them all."""

    def _upload(self, csv, key):
        with mock.patch.object(w2w, "_upload_chunks", return_value=0) as up:
            with mock.patch.object(w2w, "_cooldown_check_and_sleep"):
                w2w.upload_csv_bytes(csv, "x.csv", key, "file", dry_run=False)
        return up.call_count

    def test_a_second_key_is_not_held_by_the_first(self):
        csv = csv_with_rows(2, offset=40)
        self.assertEqual(self._upload(csv, "key-a"), 1)
        self.assertEqual(self._upload(csv, "key-a"), 0, "same key: held")
        self.assertEqual(self._upload(csv, "key-b"), 1,
                         "another account has not been sent these rows")

    def test_reset_holds_clears_every_key(self):
        csv = csv_with_rows(2, offset=50)
        self._upload(csv, "key-a")
        with mock.patch.object(sys, "argv",
                               ["wigle_to_wdgwars.py", "--reset-holds"]):
            rc = w2w.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._upload(csv, "key-a"), 1,
                         "after a reset the rows go up again")


if __name__ == "__main__":
    unittest.main()
