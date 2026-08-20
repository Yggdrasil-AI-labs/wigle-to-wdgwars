"""Transient-failure handling on the nightly WiGLE pull path.

A scheduled run that cannot reach WiGLE has nothing to fix and nothing to
report. Before this, a network blip raised an uncaught URLError (a Python
traceback in the journal) and a WiGLE 429 or 503 exited 1, so a systemd
timer reported FAILURE for an upstream condition nobody could act on.

Now those are classified as WigleUnavailable and the run stays quiet, but
not silently forever: the consecutive-blocked-run count is persisted next
to the processed transids and escalates to a real failure at
TRANSIENT_RUN_LIMIT, so a dead network or a dead token cannot hide behind
a green unit. A queued CSV (HTTP 204) is NOT counted, because WiGLE is
healthy in that case and simply still building.

These drive main() with only urlopen mocked, so they cover the real
argparse -> pull-loop -> upload wiring rather than a stubbed download.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import wigle_to_wdgwars as w2w

from tests._helpers import HEADER, csv_with_rows

TIDS = ["T-A", "T-B", "T-C"]


class _Resp(io.BytesIO):
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status

    def info(self):
        return self.headers


def _http_error(url: str, code: int, body: bytes = b"err"):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


def _ready(tid, url):
    return _Resp(csv_with_rows(1))


def _queued(tid, url):
    return _Resp(b"", 204)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "processed-transids.json"
        self.posted: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _opener(self, csv_answer=_ready, list_answer=None):
        def fake(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "transactions" in url:
                if list_answer is not None:
                    return list_answer(url)
                return _Resp(json.dumps(
                    {"success": True,
                     "results": [{"transid": t} for t in TIDS]}).encode())
            if "upload" in url:
                self.posted.append(url)
                return _Resp(json.dumps(
                    {"ok": True, "imported": 1, "captured": 1, "updated": 0,
                     "duplicates": 0, "no_gps": 0, "bad_rows": 0,
                     "total": 1}).encode())
            tid = next((t for t in TIDS if t in url), "?")
            return csv_answer(tid, url)
        return fake

    def _run_once(self, fake_urlopen, extra_argv=()):
        """One main() invocation against this test's persistent state file."""
        argv = ["w.py", "--from-wigle", "--wigle-latest", str(len(TIDS)),
                "--key", "K", "--wigle-key", "T", "--chunk-cooldown", "0",
                "--all-time", *extra_argv]
        buf = io.StringIO()
        with mock.patch.object(w2w, "PROCESSED_FILE", self.state), \
             mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(w2w.sys, "argv", argv), \
             mock.patch.object(w2w.sys, "stderr", buf), \
             mock.patch.object(w2w.sys, "stdout", buf):
            try:
                rc = w2w.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        return rc, buf.getvalue()

    def _state_now(self) -> dict:
        return json.loads(self.state.read_text()) if self.state.exists() else {}


class NetworkErrorTests(_Base):
    """A URLError used to escape as a traceback. It must not."""

    def test_network_down_at_transactions_list_stays_quiet(self):
        def boom(url):
            raise urllib.error.URLError("[Errno 111] Connection refused")

        rc, out = self._run_once(self._opener(list_answer=boom))
        self.assertEqual(rc, 0)
        self.assertNotIn("Traceback", out)
        self.assertIn("network error listing WiGLE uploads", out)
        self.assertEqual(self._state_now().get("transient_runs"), 1)

    def test_network_down_at_csv_download_stays_quiet(self):
        def drop(tid, url):
            raise urllib.error.URLError("[Errno -3] name resolution failure")

        rc, out = self._run_once(self._opener(csv_answer=drop))
        self.assertEqual(rc, 0)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.posted, [])
        self.assertEqual(self._state_now().get("transient_runs"), 1)

    def test_read_timeout_wrapped_in_urlerror_still_retries(self):
        """The 600s/900s retry must survive the new URLError branch.

        A socket timeout surfaces either bare or wrapped in URLError
        depending on where it fires; the wrapped form must not be
        misread as an unreachable network.
        """
        calls = []

        def slow(tid, url):
            calls.append(url)
            raise urllib.error.URLError(TimeoutError("timed out"))

        rc, out = self._run_once(self._opener(csv_answer=slow))
        self.assertGreaterEqual(len(calls), 2, "expected the longer retry")
        self.assertIn("timed out after", out)
        self.assertEqual(rc, 1)


class TransientStatusTests(_Base):
    def test_rate_limit_and_gateway_statuses_stay_quiet(self):
        for code in w2w.WIGLE_TRANSIENT_HTTP:
            with self.subTest(code=code):
                self.setUp()

                def unavailable(url, code=code):
                    raise _http_error(url, code)

                rc, _ = self._run_once(self._opener(list_answer=unavailable))
                self.assertEqual(rc, 0, f"HTTP {code} should not fail the run")
                self.assertEqual(self._state_now().get("transient_runs"), 1)

    def test_unexpected_500_is_still_fatal(self):
        def five_hundred(url):
            raise _http_error(url, 500, b"boom")

        rc, _ = self._run_once(self._opener(list_answer=five_hundred))
        self.assertEqual(rc, 1)

    def test_bad_token_is_still_fatal_and_not_counted_transient(self):
        def unauthorized(url):
            raise _http_error(url, 401, b"nope")

        rc, _ = self._run_once(self._opener(list_answer=unauthorized))
        self.assertEqual(rc, 1)
        self.assertNotIn("transient_runs", self._state_now())

    def test_csv_404_is_still_fatal(self):
        def gone(tid, url):
            raise _http_error(url, 404, b"gone")

        rc, _ = self._run_once(self._opener(csv_answer=gone))
        self.assertEqual(rc, 1)


class BlockedRunStreakTests(_Base):
    """Quiet for a blip, loud for a wall."""

    @staticmethod
    def _boom(url):
        raise urllib.error.URLError("network unreachable")

    def test_streak_escalates_at_the_limit(self):
        codes = [self._run_once(self._opener(list_answer=self._boom))[0]
                 for _ in range(w2w.TRANSIENT_RUN_LIMIT + 1)]
        expected = ([0] * (w2w.TRANSIENT_RUN_LIMIT - 1)
                    + [1, 1])
        self.assertEqual(codes, expected)
        self.assertEqual(self._state_now().get("transient_runs"),
                         w2w.TRANSIENT_RUN_LIMIT + 1)

    def test_escalation_message_names_the_streak(self):
        for _ in range(w2w.TRANSIENT_RUN_LIMIT - 1):
            self._run_once(self._opener(list_answer=self._boom))
        _, out = self._run_once(self._opener(list_answer=self._boom))
        self.assertIn("consecutive runs have now been blocked", out)

    def test_a_run_that_pushes_clears_the_streak(self):
        self._run_once(self._opener(list_answer=self._boom))
        self.assertEqual(self._state_now().get("transient_runs"), 1)
        rc, _ = self._run_once(self._opener())
        self.assertEqual(rc, 0)
        self.assertNotIn("transient_runs", self._state_now())
        self.assertEqual(len(self.posted), len(TIDS))

    def test_streak_restarts_from_one_after_a_good_run(self):
        self._run_once(self._opener(list_answer=self._boom))
        self._run_once(self._opener())
        rc, _ = self._run_once(self._opener(list_answer=self._boom))
        self.assertEqual(rc, 0)
        self.assertEqual(self._state_now().get("transient_runs"), 1)

    def test_a_queued_csv_is_not_a_blocked_run(self):
        """WiGLE is healthy, just slow. A week of 204s must not alarm."""
        for _ in range(w2w.TRANSIENT_RUN_LIMIT + 2):
            rc, _ = self._run_once(self._opener(csv_answer=_queued))
            self.assertEqual(rc, 0)
        self.assertNotIn("transient_runs", self._state_now())
        self.assertEqual(self.posted, [])

    def test_drop_after_partial_progress_stays_quiet_and_keeps_the_pushes(self):
        def half(tid, url):
            if tid == TIDS[2]:
                raise urllib.error.URLError("connection reset")
            return _ready(tid, url)

        rc, out = self._run_once(self._opener(csv_answer=half))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posted), 2)
        recorded = self._state_now().get("processed", [])
        self.assertIn(TIDS[0], recorded)
        self.assertIn(TIDS[1], recorded)
        self.assertNotIn(TIDS[2], recorded)
        self.assertNotIn("transient_runs", self._state_now())
        self.assertIn("left untried", out)

    def test_streak_state_does_not_disturb_the_processed_list(self):
        w2w_state = {"processed": ["OLD-1", "OLD-2"]}
        self.state.write_text(json.dumps(w2w_state))
        with mock.patch.object(w2w, "PROCESSED_FILE", self.state):
            w2w._write_transient_streak(2)
            self.assertEqual(w2w._load_processed_transids(),
                             {"OLD-1", "OLD-2"})
            self.assertEqual(w2w._load_transient_streak(), 2)
            w2w._write_transient_streak(0)
            self.assertEqual(w2w._load_processed_transids(),
                             {"OLD-1", "OLD-2"})
            self.assertEqual(w2w._load_transient_streak(), 0)

    def test_streak_survives_a_corrupt_state_file(self):
        self.state.write_text("{not json")
        with mock.patch.object(w2w, "PROCESSED_FILE", self.state):
            self.assertEqual(w2w._load_transient_streak(), 0)


class HealthyRunTests(_Base):
    def test_control_all_three_uploads_pushed_and_recorded(self):
        rc, _ = self._run_once(self._opener())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posted), len(TIDS))
        self.assertEqual(sorted(self._state_now()["processed"]), sorted(TIDS))

    def test_header_only_upload_is_recorded_but_never_posted(self):
        def one_empty(tid, url):
            return _Resp(HEADER) if tid == TIDS[0] else _ready(tid, url)

        rc, _ = self._run_once(self._opener(csv_answer=one_empty))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posted), 2)
        self.assertIn(TIDS[0], self._state_now()["processed"])


if __name__ == "__main__":
    unittest.main()
