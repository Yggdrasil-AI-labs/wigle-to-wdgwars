"""Tests for v1.3.0 family-parity flags: --preview, --update wiring,
--api-url override, --quiet/--no-version-check gating.

Network calls are blocked at module load by tests/__init__.py; this
module also mocks urllib explicitly where it would be invoked. No
real GitHub / wdgwars.pl contact happens during this run.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wigle_to_wdgwars as w2w


WIGLE_CSV = b"""WigleWifi-1.6,appRelease=2.74,model=Pixel
MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type
aa:bb:cc:11:22:33,One,[WPA2],2026-06-03 10:00:00,6,-65,41.46,-82.18,200,5,WIFI
aa:bb:cc:11:22:34,Two,[WPA2],2026-06-03 10:00:05,11,-72,41.47,-82.19,201,5,WIFI
aa:bb:cc:11:22:35,Three,[WPA2],2026-06-03 10:00:10,1,-80,41.48,-82.20,202,5,WIFI
"""


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmpdir.name) / "sample.csv"
        self.csv.write_bytes(WIGLE_CSV)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_preview_prints_first_n_rows_as_json(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = w2w.preview_csv(self.csv, n=2)
        self.assertEqual(rc, 0)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["MAC"], "aa:bb:cc:11:22:33")
        self.assertEqual(first["SSID"], "One")

    def test_preview_handles_gzipped_csv(self) -> None:
        import gzip
        gz = self.csv.with_suffix(".csv.gz")
        gz.write_bytes(gzip.compress(WIGLE_CSV))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = w2w.preview_csv(gz, n=3)
        self.assertEqual(rc, 0)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)

    def test_preview_short_file_returns_1(self) -> None:
        short = Path(self.tmpdir.name) / "short.csv"
        short.write_bytes(b"only one line\n")
        rc = w2w.preview_csv(short)
        self.assertEqual(rc, 1)

    def test_preview_empty_data_section_returns_1(self) -> None:
        empty = Path(self.tmpdir.name) / "headers-only.csv"
        empty.write_bytes(b"WigleWifi-1.6\nMAC,SSID\n")
        rc = w2w.preview_csv(empty)
        self.assertEqual(rc, 1)


class VersionCheckCacheTests(unittest.TestCase):
    """_check_for_update is called from main() under the
    --quiet / --no-version-check gate. Verify the cache logic works
    without hitting the network."""

    def test_returns_none_when_cache_says_same_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            cache = cache_dir / "version-check.json"
            cache.write_text(json.dumps({
                "checked_at": 9999999999.0,
                "latest": w2w.__version__,
            }))
            with mock.patch.object(w2w, "CONFIG_DIR", cache_dir):
                self.assertIsNone(w2w._check_for_update())

    def test_returns_newer_when_cache_says_newer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            cache = cache_dir / "version-check.json"
            cache.write_text(json.dumps({
                "checked_at": 9999999999.0,
                "latest": "99.99.99",
            }))
            with mock.patch.object(w2w, "CONFIG_DIR", cache_dir):
                self.assertEqual(w2w._check_for_update(), "99.99.99")

    def test_network_error_returns_none(self) -> None:
        """If the cache is stale or missing, _check_for_update tries
        GitHub. A failed urlopen must NOT raise to the caller — the
        main() gate just skips the nudge in that case."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(w2w, "CONFIG_DIR", Path(td)), \
                 mock.patch.object(w2w.urllib.request, "urlopen",
                                   side_effect=OSError("boom")):
                self.assertIsNone(w2w._check_for_update())


class ApiUrlOverrideTests(unittest.TestCase):
    """--api-url should swap the CSV upload endpoint at runtime.
    Verify the module-level ENDPOINT actually changes."""

    def test_endpoint_overridden_via_main(self) -> None:
        original = w2w.ENDPOINT
        try:
            with tempfile.TemporaryDirectory() as td:
                csv = Path(td) / "x.csv"
                csv.write_bytes(WIGLE_CSV)
                # Force WDGWARS_API_KEY so load_key doesn't fail.
                argv = [
                    "wigle_to_wdgwars.py",
                    "--api-url", "http://localhost:9999/override",
                    "--dry-run", "--no-version-check", "--quiet",
                    "--key", "FAKE_KEY_FOR_DRY_RUN",
                    str(csv),
                ]
                with mock.patch.object(sys, "argv", argv):
                    rc = w2w.main()
            self.assertEqual(rc, 0)
            self.assertEqual(w2w.ENDPOINT, "http://localhost:9999/override")
        finally:
            w2w.ENDPOINT = original


class UpdateDispatchTests(unittest.TestCase):
    """--update should call _run_update before any key validation. We
    don't want to run the real updater in tests, just confirm dispatch."""

    def test_update_flag_calls_run_update(self) -> None:
        argv = ["wigle_to_wdgwars.py", "--update"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(w2w, "_run_update", return_value=0) as ru:
            rc = w2w.main()
        self.assertEqual(rc, 0)
        ru.assert_called_once_with()


class PreviewDispatchTests(unittest.TestCase):
    """--preview should call preview_csv and skip key validation."""

    def test_preview_flag_dispatches_to_preview_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "x.csv"
            csv.write_bytes(WIGLE_CSV)
            argv = ["wigle_to_wdgwars.py", "--preview", "--quiet",
                    "--no-version-check", str(csv)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(w2w, "preview_csv",
                                   return_value=0) as pc:
                rc = w2w.main()
        self.assertEqual(rc, 0)
        pc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
