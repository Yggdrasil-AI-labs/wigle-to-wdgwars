"""Tests for the HTTP 413 auto-bisect path introduced in v1.4.0.

Background: 2026-06-05 LOCOSP rolled out a temporary 15 MB body cap on every
wdgwars.pl upload endpoint with a structured 413 envelope
(`{error: payload-too-large, max_bytes, received, ...}`). The client now
reacts to that envelope by halving the offending chunk and retrying both
halves; this module exercises that behavior end-to-end without touching the
network.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import wigle_to_wdgwars as w2w
from tests._helpers import HEADER
from tests._helpers import csv_with_rows as _csv_with_rows
from tests._helpers import ok_envelope as _ok_envelope


def _413_envelope(received: int, max_bytes: int = 15_728_640) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": "payload-too-large",
            "http_status": 413,
            "max_bytes": max_bytes,
            "received": received,
            "message": "Your upload is too large for this hosting plan...",
            "retry_after": 0,
        }
    )


class HalveChunkTests(unittest.TestCase):
    def test_halve_preserves_header_on_both_halves(self) -> None:
        csv = _csv_with_rows(8)
        halves = w2w._halve_chunk(csv)
        assert halves is not None
        left, right = halves
        # Both halves carry the 2-line WigleWifi-1.6 + column header.
        self.assertTrue(left.startswith(HEADER))
        self.assertTrue(right.startswith(HEADER))
        # Roughly even split (8 rows → 4+4).
        left_rows = [ln for ln in left.decode().splitlines()[2:] if ln]
        right_rows = [ln for ln in right.decode().splitlines()[2:] if ln]
        self.assertEqual(len(left_rows), 4)
        self.assertEqual(len(right_rows), 4)

    def test_halve_returns_none_on_one_row(self) -> None:
        csv = _csv_with_rows(1)
        self.assertIsNone(w2w._halve_chunk(csv))

    def test_halve_returns_none_on_empty(self) -> None:
        self.assertIsNone(w2w._halve_chunk(HEADER))


class UploadChunks413Tests(unittest.TestCase):
    """End-to-end: _upload_chunks should bisect-and-retry on a 413 envelope."""

    def _run_with_responses(self, chunks: list[bytes], responses: list[tuple]) -> tuple[int, list[tuple]]:
        """Drive _upload_chunks with a queued list of (status, body) tuples.
        Returns (rc, list_of_post_call_arg_summaries).
        """
        post_calls: list[tuple] = []
        response_iter = iter(responses)

        def fake_post(body, name, key, field):
            status, raw = next(response_iter)
            post_calls.append((status, len(body)))
            return status, raw, 0.01

        with mock.patch.object(w2w, "_post_one", side_effect=fake_post), \
             mock.patch.object(w2w, "_hwm_record"), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stderr", new=io.StringIO()), \
             mock.patch("sys.stdout", new=io.StringIO()):
            rc = w2w._upload_chunks(
                chunks, name="t.csv", key="k", field="file",
                dry_run=False, cooldown_sec=0.0,
            )
        return rc, post_calls

    def test_413_bisects_and_both_halves_succeed(self) -> None:
        csv = _csv_with_rows(8)
        responses = [
            (413, _413_envelope(received=20_000_000)),
            (200, _ok_envelope(imported=4)),
            (200, _ok_envelope(imported=4)),
        ]
        rc, calls = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 0)
        # 1 initial POST + 2 halves
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], 413)
        self.assertEqual(calls[1][0], 200)
        self.assertEqual(calls[2][0], 200)
        # Halves are strictly smaller than the original chunk.
        self.assertLess(calls[1][1], calls[0][1])
        self.assertLess(calls[2][1], calls[0][1])

    def test_413_bisects_twice_when_first_halves_still_oversize(self) -> None:
        csv = _csv_with_rows(8)
        responses = [
            (413, _413_envelope(received=40_000_000)),
            (413, _413_envelope(received=20_000_000)),
            (200, _ok_envelope(imported=2)),
            (200, _ok_envelope(imported=2)),
            (200, _ok_envelope(imported=4)),
        ]
        rc, calls = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 0)
        # Initial + left-half-413 + 2 quarter halves + right half = 5 POSTs.
        self.assertEqual(len(calls), 5)
        statuses = [s for s, _ in calls]
        self.assertEqual(statuses, [413, 413, 200, 200, 200])

    def test_413_single_row_chunk_records_failure_and_continues(self) -> None:
        """If a chunk is already one row and still gets 413, record it as a
        failure and move on. Other chunks must still process."""
        one_row = _csv_with_rows(1)
        ok_chunk = _csv_with_rows(2)
        responses = [
            (413, _413_envelope(received=16_000_000)),
            (200, _ok_envelope(imported=2)),
        ]
        rc, calls = self._run_with_responses([one_row, ok_chunk], responses)
        # Aggregate ok=False because one payload failed.
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], 413)
        self.assertEqual(calls[1][0], 200)

    def test_413_max_bytes_not_hard_coded_against_server_value(self) -> None:
        """If LOCOSP later raises the cap to 25 MB, the client must still
        react to the same envelope shape — not a baked-in constant.
        We exercise this by sending a 413 envelope with max_bytes=25 MB and
        confirming the bisect path fires identically."""
        csv = _csv_with_rows(8)
        responses = [
            (413, _413_envelope(received=30_000_000, max_bytes=26_214_400)),
            (200, _ok_envelope(imported=4)),
            (200, _ok_envelope(imported=4)),
        ]
        rc, calls = self._run_with_responses([csv], responses)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
