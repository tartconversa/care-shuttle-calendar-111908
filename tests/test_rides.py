"""乘车事实测试：换乘、迟到凭证、重复刷卡、取消预约、未解释不补造。"""
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import build_from, base_rows
from warehouse.sources import load_batches


def leg_index(model):
    return {l["leg_id"]: l for l in model.ride_leg}


class RideTests(unittest.TestCase):
    def test_simple_closed_leg_matches_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(base_rows(), Path(tmp))
        self.assertEqual(len(model.ride_leg), 1)
        leg = model.ride_leg[0]
        self.assertFalse(leg["split_leg"])
        self.assertEqual(leg["match_status"], "matched")
        self.assertEqual(leg["reservation_id"], "R1")
        self.assertEqual(model.ride_resolution, [])

    def test_transfer_two_legs_one_reservation(self):
        rows = base_rows()
        rows["dispatches.json"] = [
            {"dispatch_id": "D1", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR1", "replaces": None,
             "from_stop": "S1", "until_stop": "S3"},
            {"dispatch_id": "D2", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR1", "replaces": None,
             "from_stop": "S2", "until_stop": "S3"},
        ]
        rows["boarding.json"] = [
            {"credential_id": "C1", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S1", "action": "board",
             "at": "2026-11-04T09:05:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
            {"credential_id": "C2", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S2N", "action": "alight",
             "at": "2026-11-04T09:15:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
            {"credential_id": "C3", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D2", "stop_id": "S2N", "action": "board",
             "at": "2026-11-04T09:17:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
            {"credential_id": "C4", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D2", "stop_id": "S3", "action": "alight",
             "at": "2026-11-04T09:30:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        self.assertEqual(len(model.ride_leg), 2)
        self.assertTrue(all(l["reservation_id"] == "R1" for l in model.ride_leg))
        self.assertFalse(model.ride_leg[0]["transfer_confirmed"])
        self.assertTrue(model.ride_leg[1]["transfer_confirmed"])  # S2N 同站先下后上
        self.assertEqual(model.ride_leg[1]["transfer_from_leg_id"], model.ride_leg[0]["leg_id"])

    def test_cross_dispatch_without_handoff_splits_and_keeps_interval(self):
        rows = base_rows()
        rows["dispatches.json"] = [
            {"dispatch_id": "D1", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR1", "replaces": None,
             "from_stop": "S1", "until_stop": "S2"},
            {"dispatch_id": "D2", "opportunity_id": "T1@2026-11-04",
             "service_date": "2026-11-04", "vehicle": "CAR2", "replaces": "D1",
             "from_stop": "S2", "until_stop": "S3"},
        ]
        rows["boarding.json"] = [
            {"credential_id": "C1", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S1", "action": "board",
             "at": "2026-11-04T09:05:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
            {"credential_id": "C2", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D2", "stop_id": "S3", "action": "alight",
             "at": "2026-11-04T09:30:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        self.assertEqual(len(model.ride_leg), 2)
        self.assertTrue(all(l["split_leg"] for l in model.ride_leg))
        self.assertTrue(all(l["match_status"] == "pending_handoff" for l in model.ride_leg))
        intervals = [r for r in model.ride_resolution if r["kind"] == "unexplained_interval"]
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["between_dispatch_ids"], ["D1", "D2"])

    def test_duplicate_swipe_deduped(self):
        rows = base_rows()
        # 同车重复刷卡：不同凭证号，但同人/同动作/同时刻/同调度/同站
        dup = dict(rows["boarding.json"][0])
        dup["credential_id"] = "C1DUP"
        dup["received_at"] = "2026-11-05T09:05:00+08:00"
        rows["boarding.json"].append(dup)
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        self.assertEqual(len(model.ride_event), 3)  # 事件保留
        swipes = [e for e in model.ride_event if e["duplicate_swipe"]]
        self.assertEqual(len(swipes), 1)
        self.assertEqual(swipes[0]["credential_id"], "C1DUP")
        self.assertEqual(len(model.ride_leg), 1)  # 段数不增加

    def test_duplicate_credential_delivery_does_not_grow_facts(self):
        # 凭证号重复投递（同 credential_id 再次送达）：不产生任何新事件
        rows = base_rows()
        with tempfile.TemporaryDirectory() as tmp:
            from tests.fixtures import write_batch
            b1 = write_batch(Path(tmp) / "B1", "B1", rows)
            b2 = write_batch(Path(tmp) / "B2", "B2",
                             {"boarding.json": [rows["boarding.json"][0]]})
            bundle = load_batches([b1, b2])
            from warehouse import canonical
            model = canonical.build(bundle)
        # B1 的两条凭证仍在；B2 重复投递的 C1 不产生新事件
        self.assertEqual(len(model.ride_event), 2)
        self.assertEqual(len(bundle.duplicate_credentials), 1)
        self.assertEqual(bundle.duplicate_credentials[0]["credential_id"], "C1")
        self.assertEqual(len(model.ride_leg), 1)

    def test_cancelled_reservation_keeps_completed_ride(self):
        rows = base_rows()
        rows["reservations.json"][0]["cancelled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        self.assertTrue(model.dim_reservation[0]["cancelled"])
        self.assertEqual(len(model.ride_leg), 1)  # 已发生乘车不消失
        self.assertEqual(model.ride_leg[0]["match_status"], "matched")

    def test_missing_alight_is_unexplained_not_fabricated(self):
        rows = base_rows()
        rows["boarding.json"] = [rows["boarding.json"][0]]  # 只有上车
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        self.assertEqual(model.ride_leg, [])
        self.assertEqual(model.ride_resolution[0]["reason"], "missing_alight")

    def test_ambiguous_reservation_keeps_candidates(self):
        rows = base_rows()
        rows["reservations.json"].append(
            {"reservation_id": "R9", "person_id": "P1", "from_stop": "S1",
             "to_stop": "S3", "service_date": "2026-11-04", "cancelled": False})
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        leg = model.ride_leg[0]
        self.assertEqual(leg["match_status"], "ambiguous")
        self.assertIsNone(leg["reservation_id"])
        self.assertEqual(leg["candidate_reservation_ids"], ["R1", "R9"])

    def test_ride_using_replaced_stop_matches_old_planned_reservation(self):
        # 预约写 S2，当日 S2 已迁至 S2N，凭证在 S2N 上下车仍能匹配
        rows = base_rows()
        rows["reservations.json"][0]["from_stop"] = "S2"
        rows["boarding.json"] = [
            {"credential_id": "C1", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S2N", "action": "board",
             "at": "2026-11-04T09:10:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
            {"credential_id": "C2", "person_id": "P1", "reservation_id": "R1",
             "dispatch_id": "D1", "stop_id": "S3", "action": "alight",
             "at": "2026-11-04T09:30:00+08:00", "received_at": "2026-11-05T09:00:00+08:00"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, model = build_from(rows, Path(tmp))
        self.assertEqual(model.ride_leg[0]["match_status"], "matched")


if __name__ == "__main__":
    unittest.main()
