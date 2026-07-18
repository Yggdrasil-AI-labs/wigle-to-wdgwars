"""Tests for the HTTP 409 duplicate_upload path introduced in v1.6.1.

Background: an idempotent re-send of the same --since window (e.g. an hourly
cron pushing a byte-identical CSV) gets HTTP 409 `{error: duplicate_upload}`
from the server. The rows are already on the server, so the client treats
that specific envelope as a benign success instead of surfacing a non-zero
exit that trips downstream alerting. This module exercises that behavior
end-to-end without touching the network.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import wigle_to_wdgwars as w2w


HEADER = (
    b"WigleWifi-1.6,appRelease=2.74,model=Pixel\n"
    b"MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
    b"CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
)


def _row(mac_suffix: int) -> bytes:
    return (
        f"aa:bb:cc:00:00:{mac_suffix:02x},Net{mac_suffix},[WPA2],"
        f"2026-06-05 10:00:00,6,-65,41.46,-82.18,200,5,WIFI\n"
    ).encode()


def _csv_with_rows(n: int) -> bytes:
    return HEADER + b"".join(_row(i) for i in range(n))


def _409_envelope(duplicate_at: str = "2026-07-14T13:00:00Z") -> str:
    return json.dumps(
        {
            "ok": False,
            "error": "duplicate_upload",
            "http_status": 409,
            "duplicate_at": duplicate_at,
            "message": "This exact file was already uploaded.",
        }
    )


def _ok_envelope(imported: int = 1, total: int = 1) -> str:
    return json.dumps(
        {
            "ok": True,
            "imported": imported,
            "captured": imported,
            "updated": 0,
            "duplicates": 0,
            "no_gps": 0,
            "bad_rows": 0,
            "total": total,
        }
    )


class UploadChunks409Tests(unittest.TestCase):
    """End-to-end: _upload_chunks should treat 409 duplicate_upload as ok."""

    def _run_with_responses(self, chunks: list[bytes], responses: list[tuple]) -> tuple[int, list[tuple], str, str, mock.Mock, mock.Mock]:
        """Drive _upload_chunks with a queued list of (status, body) tuples.
        Returns (rc, post_call_summaries, stdout, stderr, hwm_mock, sleep_mock).
        """
        post_calls: list[tuple] = []
        response_iter = iter(responses)

        def fake_post(body, name, key, field):
            status, raw = next(response_iter)
            post_calls.append((status, len(body)))
            return status, raw, 0.01

        fake_out = io.StringIO()
        fake_err = io.StringIO()
        with mock.patch.object(w2w, "_post_one", side_effect=fake_post), \
             mock.patch.object(w2w, "_hwm_record") as hwm, \
             mock.patch("time.sleep") as slept, \
             mock.patch("sys.stderr", new=fake_err), \
             mock.patch("sys.stdout", new=fake_out):
            rc = w2w._upload_chunks(
                chunks, name="t.csv", key="k", field="file",
                dry_run=False, cooldown_sec=5.0,
            )
        return rc, post_calls, fake_out.getvalue(), fake_err.getvalue(), hwm, slept

    def test_409_duplicate_single_chunk_exits_zero(self) -> None:
        csv = _csv_with_rows(4)
        responses = [(409, _409_envelope())]
        rc, calls, out, err, hwm, _ = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["duplicate_upload"])
        # The duplicate must not advance the upload high-water mark: the
        # rows already counted when the original upload succeeded.
        hwm.assert_not_called()
        self.assertIn("duplicate_upload", err)
        self.assertIn("2026-07-14T13:00:00Z", err)

    def test_409_duplicate_then_ok_chunk_aggregates_ok(self) -> None:
        chunks = [_csv_with_rows(2), _csv_with_rows(3)]
        responses = [
            (409, _409_envelope()),
            (200, _ok_envelope(imported=3)),
        ]
        rc, calls, out, _, hwm, slept = self._run_with_responses(chunks, responses)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        agg = json.loads(out)
        self.assertTrue(agg["ok"])
        self.assertEqual(agg["chunks"], 2)
        # Exactly one sleep: the 409-branch cooldown before the next queued
        # chunk. The trailing 200 empties the queue, so the generic
        # inter-chunk sleep must not fire — call_count pins the sleep to the
        # duplicate branch rather than the fallthrough path.
        slept.assert_called_once_with(5.0)
        # The real upload advances the high-water mark; the duplicate didn't.
        hwm.assert_called_once()

    def test_409_duplicate_on_last_chunk_skips_cooldown(self) -> None:
        csv = _csv_with_rows(2)
        responses = [(409, _409_envelope())]
        rc, _, _, _, _, slept = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 0)
        # Empty queue after the duplicate: no pointless trailing sleep.
        slept.assert_not_called()

    def test_409_with_other_error_still_fails(self) -> None:
        """Only the duplicate_upload envelope is benign; any other 409 body
        must keep surfacing a non-zero exit."""
        csv = _csv_with_rows(2)
        other = json.dumps({"ok": False, "error": "conflict", "http_status": 409})
        responses = [(409, other)]
        rc, calls, out, _, _, _ = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_non_409_status_with_duplicate_body_still_fails(self) -> None:
        """The benign path requires status 409 AND the duplicate_upload
        envelope. A 500 whose body happens to echo duplicate_upload must not
        be masked as success."""
        csv = _csv_with_rows(2)
        responses = [(500, _409_envelope())]
        rc, calls, out, _, hwm, _ = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(json.loads(out)["ok"])
        hwm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
