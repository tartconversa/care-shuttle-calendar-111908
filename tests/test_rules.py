"""业务规则测试：计划供给、临时改站、替班、证明链与村日去重。"""
import copy
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import build_from, base_rows, write_batch, EMPTY
from warehouse.sources import SourceError, load_batches
from warehouse import canonical, publish


class PlanTests(unittest.TestCase):
    def test_calendar_expansion_and_exception_priority(self):
        rows = base_rows()
        rows["calendars.json"] = [
            {"calendar_id": "W1", "weekdays": [1], "from_date": "2026-11-01",
             "until_date": "2026-11-08"},
        ]
        rows["exceptions.json"] = [
            {"exception_id": "E1", "trip_id": "T1", "service_date": "2026-11-02",
             "action": "cancel", "new_opportunity": None},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        by_day = {o["service_date"]: o for o in model.plan_opportunity}
        self.assertEqual(set(by_day), {"2026-11-02"})  # 11 月只有两个周一
        self.assertEqual(by_day["2026-11-02"]["plan_status"], "cancelled")

    def test_cancel_one_trip_keeps_other_route_same_day(self):
        rows = base_rows()
        rows["trips.json"].append(
            {"trip_id": "T2", "calendar_id": "W1", "route_id": "R2",
             "departure_time": "10:00:00", "stops": ["S3", "S4"], "revision": 1})
        rows["stops.json"].append(
            {"stop_id": "S4", "village": "V4", "from_date": "2026-01-01",
             "until_date": None, "replaces_stop": None})
        rows["exceptions.json"] = [
            {"exception_id": "E1", "trip_id": "T1", "service_date": "2026-11-04",
             "action": "cancel", "new_opportunity": None},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        statuses = {o["opportunity_id"]: o["plan_status"] for o in model.plan_opportunity}
        self.assertEqual(statuses["T1@2026-11-04"], "cancelled")
        self.assertEqual(statuses["T2@2026-11-04"], "scheduled")

    def test_add_exception_is_new_opportunity_key(self):
        rows = base_rows()
        rows["exceptions.json"] = [
            {"exception_id": "E2", "trip_id": "T1", "service_date": "2026-11-05",
             "action": "add", "new_opportunity": "EXTRA9"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        ids = {o["opportunity_id"] for o in model.plan_opportunity}
        self.assertIn("EXTRA9", ids)
        extra = next(o for o in model.plan_opportunity if o["opportunity_id"] == "EXTRA9")
        self.assertEqual(extra["plan_status"], "added")

    def test_stop_migration_only_within_period_and_explicit_link(self):
        rows = base_rows()
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        by_day = {(t["service_date"], t["planned_stop_id"]): t
                  for t in model.plan_trip_stop}
        # 11-04 当天 S2 失效，显式替代 S2N 生效
        cur = by_day[("2026-11-04", "S2")]
        self.assertEqual(cur["effective_stop_id"], "S2N")
        self.assertEqual(cur["stop_status"], "replaced")
        # 11-03 之前 S2 仍是 S2，历史停靠不被搬到新站
        earlier = by_day[("2026-11-03" if ("2026-11-03", "S2") in by_day else "2026-11-04", "S2")]
        # 基线周三日历只覆盖 11-04，另构造一个历史日断言
        rows2 = copy.deepcopy(rows)
        rows2["calendars.json"][0]["weekdays"] = [2, 3]
        with tempfile.TemporaryDirectory() as tmp:
            _, model2 = build_from(rows2, Path(tmp))
        hist = next(t for t in model2.plan_trip_stop
                    if t["service_date"] == "2026-11-03" and t["planned_stop_id"] == "S2")
        self.assertEqual(hist["effective_stop_id"], "S2")
        self.assertEqual(hist["stop_status"], "active")


class RunTests(unittest.TestCase):
    def test_gps_alone_is_not_passenger_service(self):
        rows = base_rows()
        rows["vehicle_events.json"] = [
            {"event_id": "G1", "dispatch_id": "D1", "vehicle": "CAR1",
             "stop_id": "S1", "kind": "gps", "at": "2026-11-04T09:05:00+08:00"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        run = next(r for r in model.run_opportunity if r["opportunity_id"] == "T1@2026-11-04")
        self.assertEqual(run["run_status"], canonical.INSUFFICIENT)
        self.assertEqual(run["gps_only_stops"], ["S1"])
        self.assertEqual(run["proven_service_stops"], [])
        # 村日数据集：GPS 只证明车到过，不计入村级可达，落入资料不足
        village_day = publish.build_village_day(model)
        v1 = next(r for r in village_day
                  if r["village"] == "V1" and r["service_date"] == "2026-11-04")
        self.assertEqual(v1["reachable_opportunity_count"], 0)
        self.assertEqual(v1["insufficient_count"], 1)

    def test_no_dispatch_means_not_fulfilled(self):
        rows = base_rows()
        rows["dispatches.json"] = []
        rows["vehicle_events.json"] = []
        rows["boarding.json"] = []
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        run = next(r for r in model.run_opportunity if r["opportunity_id"] == "T1@2026-11-04")
        self.assertEqual(run["run_status"], canonical.NOT_FULFILLED)

    def test_replacement_chain_keeps_one_opportunity_identity(self):
        rows = base_rows()
        rows["dispatches.json"] = [
            {"dispatch_id": "D1", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR1", "replaces": None,
             "from_stop": "S1", "until_stop": "S2"},
            {"dispatch_id": "D2", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR2", "replaces": "D1",
             "from_stop": "S2", "until_stop": "S3"},
        ]
        rows["vehicle_events.json"] = [
            {"event_id": f"G{i}", "dispatch_id": d, "vehicle": "CARx",
             "stop_id": stop, "kind": "boarding_open",
             "at": f"2026-11-04T09:{10+i:02d}:00+08:00"}
            for i, (d, stop) in enumerate([("D1", "S1"), ("D2", "S2N"), ("D2", "S3")])
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        runs = [r for r in model.run_opportunity if r["opportunity_id"] == "T1@2026-11-04"]
        self.assertEqual(len(runs), 1)  # 替班不倍增为两趟
        self.assertEqual(runs[0]["dispatch_ids"], ["D1", "D2"])
        self.assertEqual(runs[0]["run_status"], canonical.FULFILLED)
        self.assertEqual(runs[0]["proven_service_stops"], ["S1", "S2N", "S3"])

    def test_duplicate_events_at_same_stop_do_not_multiply(self):
        rows = base_rows()
        rows["vehicle_events.json"].append(
            {"event_id": "G2", "dispatch_id": "D1", "vehicle": "CAR1",
             "stop_id": "S1", "kind": "boarding_open", "at": "2026-11-04T09:06:00+08:00"})
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        services = [s for s in model.run_stop_service if s["dispatch_id"] == "D1" and s["stop_id"] == "S1"]
        self.assertEqual(len(services), 1)
        self.assertEqual(len(services[0]["boarding_open_events"]), 2)

    def test_boarding_open_at_invalid_stop_does_not_prove_service(self):
        rows = base_rows()
        # 开门事件挂在 11-04 已失效的 S2 上（而非有效替代 S2N）
        rows["vehicle_events.json"] = [
            {"event_id": "G1", "dispatch_id": "D1", "vehicle": "CAR1",
             "stop_id": "S2", "kind": "boarding_open", "at": "2026-11-04T09:10:00+08:00"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        run = next(r for r in model.run_opportunity if r["opportunity_id"] == "T1@2026-11-04")
        self.assertEqual(run["run_status"], canonical.INSUFFICIENT)
        self.assertEqual(run["proven_service_stops"], [])

    def test_unplanned_dispatch_is_kept_as_extra_run(self):
        rows = base_rows()
        rows["dispatches.json"].append(
            {"dispatch_id": "DX", "opportunity_id": "GHOST@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR9", "replaces": None,
             "from_stop": "S1", "until_stop": "S3"})
        rows["vehicle_events.json"].append(
            {"event_id": "GX", "dispatch_id": "DX", "vehicle": "CAR9",
             "stop_id": "S1", "kind": "boarding_open", "at": "2026-11-04T11:00:00+08:00"})
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        extra = next(r for r in model.run_opportunity if r["opportunity_id"] == "GHOST@2026-11-04")
        self.assertEqual(extra["run_status"], "extra_run")
        self.assertEqual(extra["plan_status"], "unplanned")


class IngestTests(unittest.TestCase):
    def test_bad_reference_rejected(self):
        rows = base_rows()
        rows["boarding.json"][0]["dispatch_id"] = "NOPE"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SourceError):
                build_from(rows, Path(tmp))

    def test_duplicate_batch_id_rejected(self):
        rows = base_rows()
        with tempfile.TemporaryDirectory() as tmp:
            b1 = write_batch(Path(tmp) / "B1", "B1", rows)
            b2 = write_batch(Path(tmp) / "B2", "B1", EMPTY | {"calendars.json": rows["calendars.json"]})
            with self.assertRaises(SourceError):
                load_batches([b1, b2])

    def test_tampered_hash_rejected(self):
        rows = base_rows()
        with tempfile.TemporaryDirectory() as tmp:
            batch = write_batch(Path(tmp) / "B1", "B1", rows)
            (batch / "stops.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(SourceError):
                load_batches([batch])


if __name__ == "__main__":
    unittest.main()
