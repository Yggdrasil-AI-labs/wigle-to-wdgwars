"""Tests for the --since time-window gate introduced in v1.5.0.

The gate drops WiGLE rows whose FirstSeen falls outside the trailing
window before chunking or uploading. Default is 7 days so cron jobs
stop re-pushing years of WiGLE history every tick. These tests cover
the parser, the row filter, and the upload-path skip when the window
is empty.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

import wigle_to_wdgwars as w2w


HEADER = (
    "WigleWifi-1.6,appRelease=2.74,model=Pixel\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
    "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
)


def _row(when: str, mac_suffix: int = 1) -> str:
    return (
        f"aa:bb:cc:00:00:{mac_suffix:02x},Net{mac_suffix},[WPA2],"
        f"{when},6,-65,41.46,-82.18,200,5,WIFI\n"
    )


class ParseDurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(w2w.parse_duration("7d"), 7 * 86400)
        self.assertEqual(w2w.parse_duration("24h"), 24 * 3600)
        self.assertEqual(w2w.parse_duration("30m"), 30 * 60)
        self.assertEqual(w2w.parse_duration("1w"), 7 * 86400)
        self.assertEqual(w2w.parse_duration("3600s"), 3600)

    def test_bare_int_is_days(self):
        self.assertEqual(w2w.parse_duration("14"), 14 * 86400)

    def test_zero_disables(self):
        self.assertEqual(w2w.parse_duration("0"), 0)

    def test_case_insensitive(self):
        self.assertEqual(w2w.parse_duration("7D"), 7 * 86400)

    def test_bad_input_raises(self):
        with self.assertRaises(ValueError):
            w2w.parse_duration("ten days")
        with self.assertRaises(ValueError):
            w2w.parse_duration("")


class FilterCsvSinceTests(unittest.TestCase):
    def _csv(self, *firstseens: str) -> bytes:
        return (HEADER + "".join(_row(ts, i)
                                 for i, ts in enumerate(firstseens))).encode()

    def test_keeps_recent_drops_old(self):
        cutoff = datetime(2026, 6, 2, 0, 0, 0)
        csv_bytes = self._csv(
            "2026-06-08 10:00:00",  # kept (after cutoff)
            "2026-06-03 10:00:00",  # kept
            "2026-05-20 10:00:00",  # dropped
            "2024-01-01 10:00:00",  # dropped
        )
        out, stats = w2w.filter_csv_since(csv_bytes, cutoff)
        self.assertEqual(stats["kept"], 2)
        self.assertEqual(stats["dropped_old"], 2)
        self.assertEqual(stats["dropped_unparseable"], 0)
        self.assertIn(b"2026-06-08", out)
        self.assertIn(b"2026-06-03", out)
        self.assertNotIn(b"2026-05-20", out)
        self.assertNotIn(b"2024-01-01", out)

    def test_preserves_header_lines(self):
        cutoff = datetime(2026, 6, 1, 0, 0, 0)
        csv_bytes = self._csv("2026-06-05 10:00:00")
        out, _ = w2w.filter_csv_since(csv_bytes, cutoff)
        lines = out.decode().splitlines()
        self.assertTrue(lines[0].startswith("WigleWifi-1.6"))
        self.assertTrue(lines[1].startswith("MAC,SSID,"))

    def test_all_dropped_yields_header_only(self):
        cutoff = datetime(2026, 6, 1, 0, 0, 0)
        csv_bytes = self._csv("2024-01-01 10:00:00")
        out, stats = w2w.filter_csv_since(csv_bytes, cutoff)
        self.assertEqual(stats["kept"], 0)
        self.assertEqual(stats["dropped_old"], 1)
        lines = out.decode().splitlines()
        self.assertEqual(len(lines), 2)  # banner + header, no data

    def test_unparseable_firstseen_counted(self):
        cutoff = datetime(2026, 6, 1, 0, 0, 0)
        csv_bytes = self._csv("not-a-date", "2026-06-05 10:00:00")
        _, stats = w2w.filter_csv_since(csv_bytes, cutoff)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["dropped_unparseable"], 1)

    def test_missing_firstseen_column_passes_through(self):
        # WiGLE export with no FirstSeen column shouldn't break the tool.
        no_fs = (
            "WigleWifi-1.6,appRelease=2.74,model=Pixel\n"
            "MAC,SSID,AuthMode,Type\n"
            "aa:bb:cc:00:00:01,Foo,[WPA2],WIFI\n"
        ).encode()
        cutoff = datetime(2026, 6, 1)
        out, stats = w2w.filter_csv_since(no_fs, cutoff)
        self.assertTrue(stats.get("no_firstseen"))
        self.assertEqual(out, no_fs)


class ApplySinceTests(unittest.TestCase):
    def test_zero_seconds_is_passthrough(self):
        csv_bytes = (HEADER + _row("2020-01-01 00:00:00")).encode()
        self.assertEqual(w2w._apply_since(csv_bytes, 0, "x.csv"), csv_bytes)

    def test_all_filtered_returns_none(self):
        csv_bytes = (HEADER + _row("2020-01-01 00:00:00")).encode()
        # 7-day window vs a 2020 row → 0 kept → None.
        self.assertIsNone(w2w._apply_since(csv_bytes, 7 * 86400, "x.csv"))


class UploadSkipsEmptyWindowTests(unittest.TestCase):
    """When --since filters every row out, upload paths must return 0
    without calling _upload_chunks (no spurious empty POST to LOCOSP)."""

    def test_upload_csv_bytes_skips_when_window_empty(self):
        csv_bytes = (HEADER + _row("2020-01-01 00:00:00")).encode()
        with mock.patch.object(w2w, "_upload_chunks") as mock_up, \
             mock.patch.object(w2w, "_cooldown_check_and_sleep"):
            rc = w2w.upload_csv_bytes(csv_bytes, "x.csv", "K", "file",
                                      dry_run=False, since_seconds=7 * 86400)
        self.assertEqual(rc, 0)
        mock_up.assert_not_called()

    def test_upload_csv_bytes_uploads_when_in_window(self):
        recent = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        csv_bytes = (HEADER + _row(recent)).encode()
        with mock.patch.object(w2w, "_upload_chunks", return_value=0) as mock_up, \
             mock.patch.object(w2w, "_cooldown_check_and_sleep"):
            rc = w2w.upload_csv_bytes(csv_bytes, "x.csv", "K", "file",
                                      dry_run=False, since_seconds=7 * 86400)
        self.assertEqual(rc, 0)
        mock_up.assert_called_once()


if __name__ == "__main__":
    unittest.main()
