"""发布层：村日与线路服务日数据集，以及从村日/人员反查的查询。

聚合纪律：
- 村级可达班次数按履约键 opportunity_id 去重，替班链不倍增；
- 独立人数按 person_id 去重，乘车段数按 leg_id 统计，三者粒度不同；
- 站点只在显式替代关系与生效期内归村，不做坐标就近；
- 已确认结论与确认后修订同时返回，修订不覆盖确认。
"""
from __future__ import annotations

from pathlib import Path

from .associate import _read_json, _write_json  # 复用状态目录读写
from .canonical import Canonical

REACHABLE_STATES = {"fulfilled"}  # 只有证明链完整的机会才计入村级可达


def _village_of_stop(model: Canonical, stop_id: str | None) -> str | None:
    if stop_id is None:
        return None
    for stop in model.dim_stop:
        if stop["stop_id"] == stop_id:
            return stop["village"]
    return None


def build_village_day(model: Canonical) -> list[dict]:
    # 村日 <- 计划经停（含临时改站后的有效站）
    opp_index = model.index("plan_opportunity", "opportunity_id")
    run_index = model.index("run_opportunity", "opportunity_id")

    # (村, 服务日) -> 每机会一条集合记录，天然按履约键去重
    cells: dict[tuple, dict] = {}
    for ts in model.plan_trip_stop:
        opp = opp_index.get(ts["opportunity_id"])
        if opp is None:
            continue
        village = _village_of_stop(model, ts["effective_stop_id"])
        if village is None:
            continue
        cell = cells.setdefault(
            (village, ts["service_date"]),
            {"village": village, "service_date": ts["service_date"],
             "opportunity_ids": set(), "reachable_ids": set(),
             "not_fulfilled_ids": set(), "insufficient_ids": set(),
             "cancelled_ids": set(), "reached_route_ids": set()},
        )
        cell["opportunity_ids"].add(opp["opportunity_id"])
        run = run_index.get(opp["opportunity_id"])
        status = run["run_status"] if run else "not_fulfilled"
        if status in REACHABLE_STATES:
            cell["reachable_ids"].add(opp["opportunity_id"])
            cell["reached_route_ids"].add(opp["route_id"])
        elif status == "not_fulfilled":
            cell["not_fulfilled_ids"].add(opp["opportunity_id"])
        elif status == "insufficient":
            cell["insufficient_ids"].add(opp["opportunity_id"])
        elif status == "cancelled":
            cell["cancelled_ids"].add(opp["opportunity_id"])

    # 村日 <- 实际乘车（凭证上的有效站归村）
    for leg in model.ride_leg:
        for stop_id in (leg["board_stop_id"], leg["alight_stop_id"]):
            village = _village_of_stop(model, stop_id)
            if village is None:
                continue
            cell = cells.setdefault(
                (village, leg["service_date"]),
                {"village": village, "service_date": leg["service_date"],
                 "opportunity_ids": set(), "reachable_ids": set(),
                 "not_fulfilled_ids": set(), "insufficient_ids": set(),
                 "cancelled_ids": set(), "reached_route_ids": set()},
            )
            cell.setdefault("rider_legs", set()).add(leg["leg_id"])
            cell.setdefault("persons", set()).add(leg["person_id"])

    rows = []
    for cell in cells.values():
        rows.append(
            {
                "village": cell["village"],
                "service_date": cell["service_date"],
                "planned_opportunity_count": len(cell["opportunity_ids"]),
                "reachable_opportunity_count": len(cell["reachable_ids"]),
                "reachable_route_count": len(cell["reached_route_ids"]),
                "not_fulfilled_count": len(cell["not_fulfilled_ids"]),
                "insufficient_count": len(cell["insufficient_ids"]),
                "cancelled_count": len(cell["cancelled_ids"]),
                "distinct_person_count": len(cell.get("persons", set())),
                "ride_leg_count": len(cell.get("rider_legs", set())),
                "opportunity_ids": sorted(cell["opportunity_ids"]),
                "reachable_opportunity_ids": sorted(cell["reachable_ids"]),
                "not_fulfilled_opportunity_ids": sorted(cell["not_fulfilled_ids"]),
                "insufficient_opportunity_ids": sorted(cell["insufficient_ids"]),
                "cancelled_opportunity_ids": sorted(cell["cancelled_ids"]),
            }
        )
    return sorted(rows, key=lambda r: (r["service_date"], r["village"]))


def build_route_service_day(model: Canonical) -> list[dict]:
    rows = []
    for rd in model.dim_route_service_day:
        opps = [o for o in model.plan_opportunity
                if o["route_id"] == rd["route_id"] and o["service_date"] == rd["service_date"]]
        run_index = model.index("run_opportunity", "opportunity_id")
        rows.append(
            {
                "route_id": rd["route_id"],
                "service_date": rd["service_date"],
                "planned_opportunity_count": len(opps),
                "scheduled_count": sum(o["plan_status"] == "scheduled" for o in opps),
                "added_count": sum(o["plan_status"] == "added" for o in opps),
                "cancelled_count": sum(o["plan_status"] == "cancelled" for o in opps),
                "fulfilled_count": sum(
                    (run_index.get(o["opportunity_id"]) or {}).get("run_status") == "fulfilled"
                    for o in opps
                ),
                "not_fulfilled_count": sum(
                    (run_index.get(o["opportunity_id"]) or {}).get("run_status") == "not_fulfilled"
                    for o in opps
                ),
                "insufficient_count": sum(
                    (run_index.get(o["opportunity_id"]) or {}).get("run_status") == "insufficient"
                    for o in opps
                ),
                "opportunity_ids": sorted(o["opportunity_id"] for o in opps),
            }
        )
    return sorted(rows, key=lambda r: (r["service_date"], r["route_id"]))


# ---------------------------------------------------------------- 查询

def drill_village_day(model: Canonical, village: str, service_date: str) -> dict:
    """从一个村日追到每个接驳机会的兑现状态与依据。"""
    stop_ids = {s["stop_id"] for s in model.dim_stop if s["village"] == village}
    trip_stops = [
        ts for ts in model.plan_trip_stop
        if ts["service_date"] == service_date
        and (ts["effective_stop_id"] in stop_ids or ts["planned_stop_id"] in stop_ids)
    ]
    run_index = model.index("run_opportunity", "opportunity_id")
    opp_index = model.index("plan_opportunity", "opportunity_id")
    opportunities = []
    seen = set()
    for ts in trip_stops:  # 增开与模板机会各有独立 opportunity_id，不会被合并
        opp = opp_index.get(ts["opportunity_id"])
        if opp is None or opp["opportunity_id"] in seen:
            continue
        seen.add(opp["opportunity_id"])
        run = run_index.get(opp["opportunity_id"])
        opportunities.append(
            {
                "opportunity_id": opp["opportunity_id"],
                "route_id": opp["route_id"],
                "plan_status": opp["plan_status"],
                "run_status": run["run_status"] if run else "not_fulfilled",
                "dispatch_ids": run["dispatch_ids"] if run else [],
                "proven_service_stops": run["proven_service_stops"] if run else [],
                "gps_only_stops": run["gps_only_stops"] if run else [],
                "stop_resolution": {
                    "planned_stop_id": ts["planned_stop_id"],
                    "effective_stop_id": ts["effective_stop_id"],
                    "stop_status": ts["stop_status"],
                },
            }
        )
    legs = [l for l in model.ride_leg
            if l["service_date"] == service_date
            and (_village_of_stop(model, l["board_stop_id"]) == village
                 or _village_of_stop(model, l["alight_stop_id"]) == village)]
    # 该村当日有乘车事实的人员：未解释区间可能跨村，按人+服务日归属
    village_persons = {
        l["person_id"] for l in model.ride_leg
        if l["service_date"] == service_date
        and (_village_of_stop(model, l["board_stop_id"]) == village
             or _village_of_stop(model, l["alight_stop_id"]) == village)
    }
    unexplained = [
        r for r in model.ride_resolution
        if r.get("service_date") == service_date and r.get("person_id") in village_persons
    ]
    return {
        "village": village,
        "service_date": service_date,
        "opportunities": sorted(opportunities, key=lambda r: r["opportunity_id"]),
        "ride_legs": sorted(legs, key=lambda r: r["leg_id"]),
        "unexplained": unexplained,
    }


def trace_person(model: Canonical, person_id: str,
                 state_dir: str | Path | None = None) -> dict:
    """反查某位合成人员的每段乘车为何算换乘、已匹配或尚待核对。"""
    legs = [l for l in model.ride_leg if l["person_id"] == person_id]
    events = [e for e in model.ride_event if e["person_id"] == person_id]
    reservations = [r for r in model.dim_reservation if r["person_id"] == person_id]
    reservation_ids = {r["reservation_id"] for r in reservations}
    reasons = []
    for leg in sorted(legs, key=lambda l: (l["service_date"], l["board_at"] or "")):
        if leg["split_leg"]:
            reason = "跨调度半段：缺少替班交接证据，区间保留待核对"
        elif leg["transfer_confirmed"]:
            reason = f"同站接续（{leg['board_stop_id']} 先下后上），计为换乘段"
        elif leg["match_status"] == "matched":
            reason = "上下车凭证闭合且唯一连接预约"
        elif leg["match_status"] == "ambiguous":
            reason = f"存在多个候选预约 {leg['candidate_reservation_ids']}，不强行配对"
        else:
            reason = "无法连接到预约，保留未解释"
        reasons.append({"leg_id": leg["leg_id"], "match_status": leg["match_status"],
                        "transfer_confirmed": leg["transfer_confirmed"],
                        "handoff_status": leg["handoff_status"],
                        "reservation_id": leg["reservation_id"],
                        "candidate_reservation_ids": leg["candidate_reservation_ids"],
                        "reason": reason})
    result = {
        "person_id": person_id,
        "reservations": sorted(reservations, key=lambda r: (r["service_date"], r["reservation_id"])),
        "reservation_ids": sorted(reservation_ids),
        "events": sorted(events, key=lambda e: e["at"]),
        "legs": sorted(legs, key=lambda l: (l["service_date"], l["board_at"] or "")),
        "unexplained": [r for r in model.ride_resolution if r["person_id"] == person_id],
        "reasons": reasons,
    }
    if state_dir is not None:
        result["confirmations_and_revisions"] = _person_audit_trail(
            Path(state_dir), person_id, events)
    return result


def _person_audit_trail(state: Path, person_id: str, events: list[dict]) -> list[dict]:
    credential_ids = {e["credential_id"] for e in events}
    trail = []
    for path in sorted((state / "confirmations").glob("*.json")) if (state / "confirmations").exists() else []:
        cf = _read_json(path, {})
        frozen = set(cf.get("frozen", {}).get("ride_event", []))
        if credential_ids & frozen:
            trail.append({"type": "confirmed", "confirmation_id": cf["confirmation_id"],
                          "confirmed_at": cf["confirmed_at"],
                          "credentials": sorted(credential_ids & frozen)})
    rev_dir = state / "revisions"
    if rev_dir.exists():
        for path in sorted(rev_dir.glob("*.json")):
            rv = _read_json(path, {})
            late = set(rv.get("late_credential_ids", [])) & credential_ids
            if late:
                trail.append({"type": "revision", "revision_id": rv["revision_id"],
                              "revised_at": rv["revised_at"],
                              "late_credentials": sorted(late),
                              "explanation": rv["explanation"]})
    return trail


def publish(state_dir: str | Path, output_dir: str | Path) -> dict:
    from .associate import load_canonical
    model = load_canonical(state_dir)
    out = Path(output_dir)
    village_day = build_village_day(model)
    route_day = build_route_service_day(model)
    _write_json(out / "village_day.json", village_day)
    _write_json(out / "route_service_day.json", route_day)
    return {"village_day_rows": len(village_day),
            "route_service_day_rows": len(route_day),
            "output": str(out)}
