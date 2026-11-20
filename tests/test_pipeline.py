"""端到端流程测试：分批 -> 确认 -> 迟到修订 -> 发布 -> 钻取，含幂等性。"""
import json
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import base_rows, write_batch
from warehouse import associate, publish


def _split_late(rows: dict) -> tuple[dict, dict]:
    early = {k: v for k, v in rows.items() if k != "boarding.json"}
    late_credentials = {"C9", "C10"}
    b01 = dict(early)
    b01["boarding.json"] = [c for c in rows["boarding.json"]
                            if c["credential_id"] not in late_credentials]
    b02 = {"boarding.json": [c for c in rows["boarding.json"]
                             if c["credential_id"] in late_credentials]}
    return b01, b02


def _handoff_rows() -> dict:
    """合成一份带替班交接与迟到换乘凭证的资料（与真实 data 同构）。"""
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
        {"event_id": f"G{i}", "dispatch_id": d, "vehicle": "CAR",
         "stop_id": stop, "kind": kind,
         "at": at}
        for i, (d, stop, kind, at) in enumerate([
            ("D1", "S1", "boarding_open", "2026-11-04T09:05:00+08:00"),
            ("D1", "S2N", "boarding_open", "2026-11-04T09:15:00+08:00"),
            ("D2", "S2N", "boarding_open", "2026-11-04T09:17:00+08:00"),
            ("D2", "S3", "boarding_open", "2026-11-04T09:30:00+08:00"),
        ])
    ]
    rows["boarding.json"] = [
        {"credential_id": "C1", "person_id": "P1", "reservation_id": "R1",
         "dispatch_id": "D1", "stop_id": "S1", "action": "board",
         "at": "2026-11-04T09:05:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
        {"credential_id": "C2", "person_id": "P1", "reservation_id": "R1",
         "dispatch_id": "D2", "stop_id": "S3", "action": "alight",
         "at": "2026-11-04T09:30:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
        # 迟到的换乘交接凭证
        {"credential_id": "C9", "person_id": "P1", "reservation_id": "R1",
         "dispatch_id": "D1", "stop_id": "S2N", "action": "alight",
         "at": "2026-11-04T09:15:00+08:00", "received_at": "2026-11-05T10:00:00+08:00"},
        {"credential_id": "C10", "person_id": "P1", "reservation_id": "R1",
         "dispatch_id": "D2", "stop_id": "S2N", "action": "board",
         "at": "2026-11-04T09:17:00+08:00", "received_at": "2026-11-05T10:00:00+08:00"},
    ]
    return rows


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        rows = _handoff_rows()
        b01, b02 = _split_late(rows)
        self.b01 = write_batch(self.tmp / "b01", "B01", b01)
        self.b02 = write_batch(self.tmp / "b02", "B02", b02)
        self.state = self.tmp / "state"
        self.output = self.tmp / "outputs"

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_flow_confirmation_then_late_revision(self):
        # 第一批：缺交接凭证，P1 跨调度半段待核对
        associate.run_ingest(self.state, [self.b01])
        model_b01 = associate.load_canonical(self.state)
        self.assertTrue(all(l["split_leg"] for l in model_b01.ride_leg))

        cf = associate.confirm_scope(
            self.state, {"service_date": "2026-11-04"},
            "2026-11-05T09:30:00+08:00", "联合工作组")
        self.assertEqual(cf["visible_batches"], ["B01"])
        frozen_legs = set(cf["frozen"]["ride_leg"])
        self.assertTrue(frozen_legs)  # 半段也被冻结

        # 第二批迟到凭证到达后重算
        associate.run_ingest(self.state, [self.b01, self.b02])
        model = associate.load_canonical(self.state)
        self.assertEqual(len(model.ride_leg), 2)
        self.assertTrue(all(not l["split_leg"] for l in model.ride_leg))
        self.assertTrue(all(l["reservation_id"] == "R1" for l in model.ride_leg))
        transfer_legs = [l for l in model.ride_leg if l["transfer_confirmed"]]
        self.assertEqual(len(transfer_legs), 1)
        self.assertEqual(transfer_legs[0]["board_stop_id"], "S2N")

        # 修订只解释、不覆盖确认
        result = associate.apply_revision(self.state, "2026-11-05T12:00:00+08:00")
        self.assertEqual(len(result["revisions"]), 1)
        rv = result["revisions"][0]
        self.assertEqual(set(rv["late_credential_ids"]), {"C9", "C10"})
        cf_after = json.loads(
            (self.state / "confirmations" / f"{cf['confirmation_id']}.json").read_text())
        self.assertEqual(cf_after, cf)  # 快照字节级不变

    def test_publish_village_day_dedup_and_drill(self):
        associate.run_ingest(self.state, [self.b01, self.b02])
        summary = publish.publish(self.state, self.output)
        self.assertGreater(summary["village_day_rows"], 0)
        village_day = json.loads((self.output / "village_day.json").read_text())
        v2 = next(r for r in village_day
                  if r["village"] == "V2" and r["service_date"] == "2026-11-04")
        # 一个履约键只计一次可达，D1/D2 替班不倍增
        self.assertEqual(v2["reachable_opportunity_count"], 1)
        self.assertEqual(v2["planned_opportunity_count"], 1)

        model = associate.load_canonical(self.state)
        drill = publish.drill_village_day(model, "V2", "2026-11-04")
        self.assertEqual(len(drill["opportunities"]), 1)
        self.assertEqual(drill["opportunities"][0]["run_status"], "fulfilled")
        self.assertEqual(drill["opportunities"][0]["stop_resolution"]["planned_stop_id"], "S2")

        trace = publish.trace_person(model, "P1", self.state)
        reasons = {r["leg_id"]: r["reason"] for r in trace["reasons"]}
        self.assertTrue(any("换乘" in r for r in reasons.values()))

    def test_ingest_is_idempotent(self):
        associate.run_ingest(self.state, [self.b01, self.b02])
        first = {p.name: p.read_bytes() for p in (self.state / "canonical").iterdir()}
        associate.run_ingest(self.state, [self.b01, self.b02])
        second = {p.name: p.read_bytes() for p in (self.state / "canonical").iterdir()}
        # manifest 时间戳除外，事实表字节完全一致
        first.pop("_manifest.json"); second.pop("_manifest.json")
        self.assertEqual(first, second)

    def test_route_service_day_separates_plan_and_run(self):
        associate.run_ingest(self.state, [self.b01, self.b02])
        publish.publish(self.state, self.output)
        route_day = json.loads((self.output / "route_service_day.json").read_text())
        row = next(r for r in route_day if r["service_date"] == "2026-11-04")
        self.assertEqual(row["planned_opportunity_count"], 1)
        self.assertEqual(row["fulfilled_count"], 1)
        self.assertEqual(row["not_fulfilled_count"], 0)

    def test_added_opportunity_kept_distinct_in_village_day(self):
        # 增开键 EXTRA1 必须独立进入村日，不与同 trip 的模板行合并
        rows = _handoff_rows()
        rows["exceptions.json"] = [
            {"exception_id": "E2", "trip_id": "T1", "service_date": "2026-11-04",
             "action": "add", "new_opportunity": "EXTRA1"},
        ]
        b01, b02 = _split_late(rows)
        d1 = write_batch(self.tmp / "x1", "X1", b01)
        d2 = write_batch(self.tmp / "x2", "X2", b02)
        associate.run_ingest(self.state, [d1, d2])
        model = associate.load_canonical(self.state)
        ts = next(t for t in model.plan_trip_stop if t["opportunity_id"] == "EXTRA1")
        self.assertEqual(ts["trip_id"], "T1")
        village_day = publish.build_village_day(model)
        v1 = next(r for r in village_day
                  if r["village"] == "V1" and r["service_date"] == "2026-11-04")
        self.assertIn("EXTRA1", v1["opportunity_ids"])
        drill = publish.drill_village_day(model, "V1", "2026-11-04")
        self.assertIn("EXTRA1", [o["opportunity_id"] for o in drill["opportunities"]])


if __name__ == "__main__":
    unittest.main()
