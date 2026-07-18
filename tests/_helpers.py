"""Shared fixtures for the upload-path test modules.

test_413_autosplit.py and test_409_duplicate.py both drive
_upload_chunks against synthetic WiGLE-1.6 CSVs and canned server
envelopes; the CSV builders and the success envelope live here so the
two modules can't drift apart.
"""
from __future__ import annotations

import json

HEADER = (
    b"WigleWifi-1.6,appRelease=2.74,model=Pixel\n"
    b"MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
    b"CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
)


def row(mac_suffix: int) -> bytes:
    return (
        f"aa:bb:cc:00:00:{mac_suffix:02x},Net{mac_suffix},[WPA2],"
        f"2026-06-05 10:00:00,6,-65,41.46,-82.18,200,5,WIFI\n"
    ).encode()


def csv_with_rows(n: int) -> bytes:
    return HEADER + b"".join(row(i) for i in range(n))


def ok_envelope(imported: int = 1, total: int = 1) -> str:
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
