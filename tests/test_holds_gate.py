"""The already-sent gate: don't re-send rows the server has confirmed.

A cron pushing the same export every few minutes re-sends the same rows
every time. The server counts those as syncs that carried nothing new and
says so on the Uplink page. The mechanism is gungnir.holds, shared with
Muninn and heimdall; what is ours is the key (a network is its MAC and
SSID together) and the two CSV filters around it.

Muninn shipped this first and got it wrong three times, so the rules those
mistakes produced are pinned here too:

- A response that imported nothing means the server already held every row
  in the payload, which earns the day-long hold. Anything imported keeps
  the short one, because the response does not say WHICH rows were new.
- Unknown is not zero. A multi-chunk upload cannot be verified against its
  own last-chunk watermark and must not earn the long hold.
- Every unreadable thing errs toward uploading.

Run: WIGLE_TEST_ALLOW_LIVE_KEY=1 python -m unittest tests.test_holds_gate
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import gungnir

import wigle_to_wdgwars as w2w
from tests._helpers import HEADER, csv_with_rows


class RowKeyTests(unittest.TestCase):
    def test_key_is_mac_and_ssid_together(self):
        self.assertEqual(w2w._row_key(["aa:bb:cc", "CoffeeShop"]),
                         "AA:BB:CC|CoffeeShop")

    def test_mac_case_is_folded_but_ssid_case_is_not(self):
        # A MAC is case-insensitive. Two SSIDs differing only in case are
        # two different networks, and folding them would suppress one.
        self.assertEqual(w2w._row_key(["AA:BB:CC", "x"]),
                         w2w._row_key(["aa:bb:cc", "x"]))
        self.assertNotEqual(w2w._row_key(["aa:bb:cc", "Net"]),
                            w2w._row_key(["aa:bb:cc", "net"]))

    def test_an_unreadable_row_has_no_key(self):
        # None means "always upload it".
        self.assertIsNone(w2w._row_key([]))
        self.assertIsNone(w2w._row_key(["", "ssid"]))
        self.assertIsNone(w2w._row_key(["   ", "ssid"]))

    def test_a_missing_ssid_still_keys_on_the_mac(self):
        self.assertEqual(w2w._row_key(["aa:bb:cc"]), None)
        self.assertEqual(w2w._row_key(["aa:bb:cc", ""]), "AA:BB:CC|")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.csv = csv_with_rows(3)

    def _state_for(self, *suffixes):
        return {f"AA:BB:CC:00:00:{i:02X}|Net{i}": self.now + 999
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
        state = {"AA:BB:CC:00:00:00|Net0": self.now - 1}
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
                         ["AA:BB:CC:00:00:00|Net0",
                          "AA:BB:CC:00:00:01|Net1"])


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
                                  ["AA:BB:CC:00:00:00|Net0"], self.now)
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


class RecordHoldsTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.csv = csv_with_rows(2)

    def _write_hwm(self, imported, ts=None):
        p = gungnir.hwm._path(w2w.HOLDS_TOOL)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'{{"last_upload_ts": {ts or self.now + 5}, '
                     f'"counters": {{"imported": {imported}}}}}')

    def test_nothing_imported_earns_the_long_hold(self):
        self._write_hwm(0)
        w2w._record_holds(self.csv, self.now, True)
        state = gungnir.holds.load(w2w.HOLDS_TOOL)
        self.assertGreater(state["AA:BB:CC:00:00:00|Net0"],
                           self.now + gungnir.holds.SENT_TTL)

    def test_something_imported_keeps_the_short_hold(self):
        self._write_hwm(1)
        w2w._record_holds(self.csv, self.now, True)
        state = gungnir.holds.load(w2w.HOLDS_TOOL)
        self.assertLessEqual(state["AA:BB:CC:00:00:00|Net0"],
                             self.now + gungnir.holds.SENT_TTL)

    def test_a_multi_chunk_upload_cannot_earn_the_long_hold(self):
        # Its watermark describes the last chunk only.
        self._write_hwm(0)
        w2w._record_holds(self.csv, self.now, False)
        state = gungnir.holds.load(w2w.HOLDS_TOOL)
        self.assertLessEqual(state["AA:BB:CC:00:00:00|Net0"],
                             self.now + gungnir.holds.SENT_TTL)

    def test_an_old_gungnir_records_nothing_and_does_not_raise(self):
        with mock.patch.object(w2w, "HOLDS_AVAILABLE", False):
            w2w._record_holds(self.csv, self.now, True)
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


if __name__ == "__main__":
    unittest.main()
