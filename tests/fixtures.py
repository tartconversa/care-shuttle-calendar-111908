"""测试夹具：在临时目录构造合成批次，不触碰真实 data/ 目录。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from warehouse.sources import load_batches

FILES = (
    "calendars.json", "trips.json", "exceptions.json", "dispatches.json",
    "stops.json", "vehicle_events.json", "reservations.json", "boarding.json",
)

EMPTY = {name: [] for name in FILES}


def write_batch(batch_dir: str | Path, batch_id: str, rows: dict) -> Path:
    batch_dir = Path(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, payload in rows.items():
        path = batch_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"batch_id": batch_id, "files": sorted(rows), "source_hashes": hashes}
    (batch_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return batch_dir


def build_from(rows: dict, tmp: Path, batch_id: str = "B1"):
    batch_dir = write_batch(tmp / batch_id, batch_id, rows)
    bundle = load_batches([batch_dir])
    from warehouse import canonical
    return bundle, canonical.build(bundle)


# 一组贯穿全部规则的基线资料（可按需覆盖个别文件）
def base_rows() -> dict:
    return {
        "calendars.json": [
            {"calendar_id": "W1", "weekdays": [3], "from_date": "2026-11-01",
             "until_date": "2026-11-30"},
        ],
        "trips.json": [
            {"trip_id": "T1", "calendar_id": "W1", "route_id": "R1",
             "departure_time": "09:00:00", "stops": ["S1", "S2", "S3"], "revision": 1},
        ],
        "exceptions.json": [],
        "stops.json": [
            {"stop_id": "S1", "village": "V1", "from_date": "2026-01-01",
             "until_date": None, "replaces_stop": None},
            {"stop_id": "S2", "village": "V2", "from_date": "2026-01-01",
             "until_date": "2026-11-04", "replaces_stop": None},
            {"stop_id": "S2N", "village": "V2", "from_date": "2026-11-04",
             "until_date": None, "replaces_stop": "S2"},
            {"stop_id": "S3", "village": "V3", "from_date": "2026-01-01",
             "until_date": None, "replaces_stop": None},
        ],
        "dispatches.json": [
            {"dispatch_id": "D1", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR1", "replaces": None,
             "from_stop": "S1", "until_stop": "S3"},
        ],
        "vehicle_events.json": [
            {"event_id": "G1", "dispatch_id": "D1", "vehicle": "CAR1",
             "stop_id": "S1", "kind": "boarding_open", "at": "2026-11-04T09:05:00+08:00"},
        ],
        "reservations.json": [
            {"reservation_id": "R1", "person_id": "P1", "from_stop": "S1",
             "to_stop": "S3", "service_date": "2026-11-04", "cancelled": False},
        ],
        "boarding.json": [
            {"credential_id": "C1", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S1", "action": "board",
             "at": "2026-11-04T09:05:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
            {"credential_id": "C2", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S3", "action": "alight",
             "at": "2026-11-04T09:30:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
        ],
    }
