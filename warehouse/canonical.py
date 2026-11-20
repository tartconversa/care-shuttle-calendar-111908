"""规范化层：把来源行变成可连接、不倍增的维度与事实。

事实分三层，互不混写：
  plan_opportunity / plan_trip_stop   计划供给
  run_dispatch / run_stop_service     实际运行（GPS 不等于对客服务）
  ride_leg / ride_resolution          人员乘车（凭证去重，未解释不补造）

所有事实以稳定自然键为主键，重跑可幂等覆盖；跨层只通过
opportunity_id、dispatch_id、service_date、stop_id、person_id 连接。
"""
from __future__ import annotations

from dataclasses import dataclass

from .clock import (
    departure_dt,
    effective_on,
    parse_date,
    parse_dt,
    service_date_of,
)
from .sources import SourceBundle

# 村日机会的三种履约结论
FULFILLED = "fulfilled"          # 已兑现：有效计划 + 调度 + 对客开门共同成立
NOT_FULFILLED = "not_fulfilled"  # 计划未兑现：无调度或无对客开门证明
INSUFFICIENT = "insufficient"    # 资料不足：有运行凭据但证明链不完整


@dataclass
class Canonical:
    local_tz: object
    dim_stop: list[dict]
    dim_reservation: list[dict]
    dim_route_service_day: list[dict]
    plan_opportunity: list[dict]
    plan_trip_stop: list[dict]
    run_dispatch: list[dict]
    run_stop_service: list[dict]
    run_opportunity: list[dict]
    ride_event: list[dict]
    ride_leg: list[dict]
    ride_resolution: list[dict]

    def index(self, table: str, key: str) -> dict:
        return {row[key]: row for row in getattr(self, table)}


def build(bundle: SourceBundle) -> Canonical:
    tz = bundle.local_tz
    dim_stop = _dim_stops(bundle)
    dim_reservation = _dim_reservations(bundle)
    opportunities, trip_stops, route_days = _plan_facts(bundle, tz)
    dispatches, stop_services, run_opps = _run_facts(bundle, tz, opportunities)
    events, legs, resolutions = _ride_facts(bundle, tz, dispatches)
    return Canonical(
        local_tz=tz,
        dim_stop=dim_stop,
        dim_reservation=dim_reservation,
        dim_route_service_day=route_days,
        plan_opportunity=opportunities,
        plan_trip_stop=trip_stops,
        run_dispatch=dispatches,
        run_stop_service=stop_services,
        run_opportunity=run_opps,
        ride_event=events,
        ride_leg=legs,
        ride_resolution=resolutions,
    )


def _dim_reservations(bundle: SourceBundle) -> list[dict]:
    """预约维度：取消是预约状态，不否定已经发生的乘车事实。"""
    return [
        {
            "reservation_id": r["reservation_id"],
            "person_id": r["person_id"],
            "from_stop": r["from_stop"],
            "to_stop": r["to_stop"],
            "service_date": r["service_date"],
            "cancelled": r["cancelled"],
        }
        for r in sorted(bundle.table("reservations.json"),
                        key=lambda r: r["reservation_id"])
    ]


# ---------------------------------------------------------------- 维度

def _dim_stops(bundle: SourceBundle) -> list[dict]:
    """站点版本维度：只承认 stops 内显式 replaces_stop 的替代关系。

    绝不按最近坐标搬迁历史停靠——来源里没有坐标，替代仅在生效期内成立。
    """
    out = []
    for stop in sorted(bundle.table("stops.json"), key=lambda r: r["stop_id"]):
        out.append(
            {
                "stop_id": stop["stop_id"],
                "village": stop["village"],
                "from_date": stop["from_date"],
                "until_date": stop["until_date"],
                "replaces_stop": stop["replaces_stop"],
            }
        )
    return out


def _resolve_stop(bundle: SourceBundle, stop_id: str, day) -> dict:
    """返回某服务日该计划站的有效版本与状态。"""
    stop = bundle.row("stops.json", stop_id)
    if stop is not None and effective_on(day, stop["from_date"], stop["until_date"]):
        return {"stop_id": stop_id, "effective_stop_id": stop_id, "stop_status": "active"}
    # 原站失效：只沿显式替代链寻找当日生效版本，不做空间就近推断
    current, visited = stop_id, {stop_id}
    while True:
        candidates = [
            s for s in bundle.table("stops.json")
            if s["replaces_stop"] == current
        ]
        if not candidates:
            return {"stop_id": stop_id, "effective_stop_id": None, "stop_status": "no_effective_stop"}
        if len(candidates) > 1:
            return {"stop_id": stop_id, "effective_stop_id": None, "stop_status": "ambiguous_replacement"}
        nxt = candidates[0]
        if nxt["stop_id"] in visited:
            return {"stop_id": stop_id, "effective_stop_id": None, "stop_status": "replacement_cycle"}
        visited.add(nxt["stop_id"])
        if effective_on(day, nxt["from_date"], nxt["until_date"]):
            return {"stop_id": stop_id, "effective_stop_id": nxt["stop_id"],
                    "stop_status": "replaced"}
        current = nxt["stop_id"]


# ---------------------------------------------------------------- 计划

def _calendar_dates(bundle: SourceBundle) -> dict[str, list]:
    result = {}
    for cal in bundle.table("calendars.json"):
        start = parse_date(cal["from_date"])
        until = parse_date(cal["until_date"])
        days, cursor = [], start
        while cursor < until:  # 左闭右开
            if cursor.isoweekday() in cal["weekdays"]:
                days.append(cursor)
            cursor = cursor.fromordinal(cursor.toordinal() + 1)
        result[cal["calendar_id"]] = days
    return result


def _plan_facts(bundle: SourceBundle, tz):
    cal_dates = _calendar_dates(bundle)
    # 例外索引：(trip_id, service_date) -> exception
    exc_index = {
        (e["trip_id"], e["service_date"]): e
        for e in bundle.table("exceptions.json")
    }

    opportunities, trip_stops, route_days = [], [], []
    route_day_seen: set[tuple] = set()

    for trip in sorted(bundle.table("trips.json"), key=lambda r: r["trip_id"]):
        for day in cal_dates.get(trip["calendar_id"], []):
            day_iso = day.isoformat()
            exc = exc_index.get((trip["trip_id"], day_iso))
            if exc is not None and exc["action"] == "cancel":
                # 取消只取消这一趟，不取消同日其他线路
                opportunities.append(_opportunity_row(trip, day_iso, tz, "cancelled", None,
                                                      exc["exception_id"]))
            else:
                opportunities.append(_opportunity_row(trip, day_iso, tz, "scheduled", None, None))
            _emit_trip_stops(bundle, trip, day, day_iso, trip_stops,
                             opportunity_id=f"{trip['trip_id']}@{day_iso}")
            rd = (trip["route_id"], day_iso)
            if rd not in route_day_seen:
                route_day_seen.add(rd)
                route_days.append({"route_id": trip["route_id"], "service_date": day_iso})

    # 增开是新的履约机会，键来自 new_opportunity，不与模板班次共用
    for exc in sorted(bundle.table("exceptions.json"), key=lambda r: r["exception_id"]):
        if exc["action"] != "add":
            continue
        trip = bundle.row("trips.json", exc["trip_id"])
        day_iso = exc["service_date"]
        opportunities.append(
            _opportunity_row(trip, day_iso, tz, "added", exc["new_opportunity"], exc["exception_id"])
        )
        _emit_trip_stops(bundle, trip, parse_date(day_iso), day_iso, trip_stops,
                         opportunity_id=exc["new_opportunity"])
        rd = (trip["route_id"], day_iso)
        if rd not in route_day_seen:
            route_day_seen.add(rd)
            route_days.append({"route_id": trip["route_id"], "service_date": day_iso})

    route_days.sort(key=lambda r: (r["service_date"], r["route_id"]))
    return opportunities, trip_stops, route_days


def _opportunity_row(trip: dict, day_iso: str, tz, plan_status: str,
                     override_key: str | None, exception_id: str | None) -> dict:
    opportunity_id = override_key or f"{trip['trip_id']}@{day_iso}"
    return {
        "opportunity_id": opportunity_id,
        "trip_id": trip["trip_id"],
        "route_id": trip["route_id"],
        "service_date": day_iso,
        "revision": trip["revision"],
        "plan_status": plan_status,  # scheduled / added / cancelled
        "exception_id": exception_id,
        "planned_departure_at": departure_dt(parse_date(day_iso),
                                             trip["departure_time"], tz).isoformat(),
    }


def _emit_trip_stops(bundle: SourceBundle, trip: dict, day, day_iso: str,
                     out: list[dict], opportunity_id: str | None = None) -> None:
    for seq, stop_id in enumerate(trip["stops"]):
        resolution = _resolve_stop(bundle, stop_id, day)
        out.append(
            {
                "opportunity_id": opportunity_id or f"{trip['trip_id']}@{day_iso}",
                "trip_id": trip["trip_id"],
                "service_date": day_iso,
                "stop_seq": seq,
                "planned_stop_id": stop_id,
                "effective_stop_id": resolution["effective_stop_id"],
                "stop_status": resolution["stop_status"],
            }
        )


# ---------------------------------------------------------------- 运行

def _run_facts(bundle: SourceBundle, tz, opportunities: list[dict]):
    opp_ids = {o["opportunity_id"] for o in opportunities}
    dispatches = []
    for d in sorted(bundle.table("dispatches.json"), key=lambda r: r["dispatch_id"]):
        if d["opportunity_id"] not in opp_ids:
            # 未计划却有调度凭据：保留为额外实驶，不塞进计划机会
            linkage = "extra_unplanned"
        else:
            linkage = "planned"
        dispatches.append(
            {
                "dispatch_id": d["dispatch_id"],
                "opportunity_id": d["opportunity_id"],
                "service_date": d["service_date"],
                "vehicle": d["vehicle"],
                "replaces": d["replaces"],
                "from_stop": d["from_stop"],
                "until_stop": d["until_stop"],
                "linkage": linkage,
            }
        )

    # 每个 (调度, 站) 只出一条服务事实；同一时刻多条事件按 kind 归并不倍增
    service_map: dict[tuple, dict] = {}
    for ev in sorted(bundle.table("vehicle_events.json"), key=lambda r: r["event_id"]):
        key = (ev["dispatch_id"], ev["stop_id"])
        slot = service_map.setdefault(
            key,
            {
                "dispatch_id": ev["dispatch_id"],
                "stop_id": ev["stop_id"],
                "gps_events": [],
                "boarding_open_events": [],
                "has_gps": False,
                "has_boarding_open": False,
            },
        )
        slot["gps_events"].append(ev["event_id"]) if ev["kind"] == "gps" else None
        slot["boarding_open_events"].append(ev["event_id"]) if ev["kind"] == "boarding_open" else None
        slot["has_gps"] = slot["has_gps"] or ev["kind"] == "gps"
        slot["has_boarding_open"] = slot["has_boarding_open"] or ev["kind"] == "boarding_open"
    stop_services = sorted(service_map.values(), key=lambda r: (r["dispatch_id"], r["stop_id"]))

    # 机会级履约汇总：替班延续同一履约身份，链上调度不各算一趟
    run_opps = _rollup_opportunities(opportunities, dispatches, stop_services, bundle)
    return dispatches, stop_services, run_opps


def _rollup_opportunities(opportunities, dispatches, stop_services, bundle: SourceBundle) -> list[dict]:
    dispatch_by_opp: dict[str, list[dict]] = {}
    for d in dispatches:
        dispatch_by_opp.setdefault(d["opportunity_id"], []).append(d)

    out = []
    for opp in opportunities:
        chain = dispatch_by_opp.get(opp["opportunity_id"], [])
        proven_stops, gps_only_stops = set(), set()
        if opp["plan_status"] == "cancelled":
            status = "cancelled"
        elif not chain:
            status = NOT_FULFILLED
        else:
            # 对客机会需有效计划、调度及有效村站 boarding_open 共同成立
            for d in chain:
                for s in stop_services:
                    if s["dispatch_id"] != d["dispatch_id"]:
                        continue
                    stop = bundle.row("stops.json", s["stop_id"])
                    stop_valid = stop is not None and effective_on(
                        parse_date(opp["service_date"]), stop["from_date"], stop["until_date"]
                    )
                    if s["has_boarding_open"] and stop_valid:
                        proven_stops.add(s["stop_id"])
                    elif s["has_gps"]:
                        gps_only_stops.add(s["stop_id"])
            if proven_stops:
                status = FULFILLED
            else:
                # 有调度在跑（甚至有 GPS），但缺有效村站对客开门 -> 资料不足
                status = INSUFFICIENT
        out.append(
            {
                "opportunity_id": opp["opportunity_id"],
                "route_id": opp["route_id"],
                "service_date": opp["service_date"],
                "plan_status": opp["plan_status"],
                "dispatch_ids": sorted(d["dispatch_id"] for d in chain),
                "proven_service_stops": sorted(proven_stops) if chain else [],
                "gps_only_stops": sorted(gps_only_stops) if chain else [],
                "run_status": status,
            }
        )

    # 未计划却有服务凭据的额外实驶
    planned_opp_ids = {o["opportunity_id"] for o in opportunities}
    for opp_id in sorted({d["opportunity_id"] for d in dispatches} - planned_opp_ids):
        chain = dispatch_by_opp[opp_id]
        proven = sorted(
            s["stop_id"] for s in stop_services
            if s["dispatch_id"] in {d["dispatch_id"] for d in chain} and s["has_boarding_open"]
        )
        out.append(
            {
                "opportunity_id": opp_id,
                "route_id": None,
                "service_date": chain[0]["service_date"],
                "plan_status": "unplanned",
                "dispatch_ids": sorted(d["dispatch_id"] for d in chain),
                "proven_service_stops": proven,
                "gps_only_stops": [],
                "run_status": "extra_run",
            }
        )
    return out


# ---------------------------------------------------------------- 乘车

def _ride_facts(bundle: SourceBundle, tz, dispatches: list[dict]):
    dispatch_index = {d["dispatch_id"]: d for d in dispatches}
    # 去重分两层：
    # 1) 凭证号重复投递（同 credential_id）：装载层已丢弃，不产生事件；
    # 2) 同车重复刷卡（不同 credential_id，同人同动作同时刻同调度同站）：
    #    保留事件并标记 duplicate_swipe，配对时跳过，不新增段数或人数。
    events, seen, delivery_seen = [], set(), set()
    for c in sorted(bundle.table("boarding.json"),
                    key=lambda r: (r["at"], r["credential_id"])):
        at_dt = parse_dt(c["at"])
        svc_day = service_date_of(at_dt, tz).isoformat()
        dedup_key = (c["person_id"], c["action"], c["at"], c["dispatch_id"], c["stop_id"])
        repeated_swipe = dedup_key in seen  # 同车重复刷卡（不同凭证号）
        seen.add(dedup_key)
        repeated_delivery = c["credential_id"] in delivery_seen  # 凭证号重复投递
        delivery_seen.add(c["credential_id"])
        d = dispatch_index.get(c["dispatch_id"])
        events.append(
            {
                "credential_id": c["credential_id"],
                "person_id": c["person_id"],
                "reservation_id": c["reservation_id"],
                "dispatch_id": c["dispatch_id"],
                "stop_id": c["stop_id"],
                "action": c["action"],
                "at": c["at"],
                "received_at": c["received_at"],
                "service_date": svc_day,
                "dispatch_service_date": d["service_date"] if d else None,
                "day_consistent": bool(d and d["service_date"] == svc_day),
                "duplicate_swipe": repeated_swipe,
                "duplicate_delivery": repeated_delivery,
                "batch_id": bundle.credential_batches.get(c["credential_id"]),
            }
        )

    legs, resolutions = _assemble_legs(events, bundle)
    return events, legs, resolutions


def _assemble_legs(events: list[dict], bundle: SourceBundle):
    """按人把凭证配对成乘车段；配不上的保留为未解释，不补造动作。"""
    legs, resolutions = [], []
    by_person: dict[str, list[dict]] = {}
    for ev in events:
        # 重复刷卡事件保留并标记，配对时跳过，不新增长度或人数
        if ev["duplicate_swipe"]:
            continue
        by_person.setdefault(ev["person_id"], []).append(ev)

    for person_id in sorted(by_person):
        chain = sorted(by_person[person_id], key=lambda e: e["at"])
        open_board: dict | None = None
        person_legs: list[dict] = []
        for ev in chain:
            if ev["action"] == "board":
                if open_board is not None:
                    # 连续两次上车、缺下车证据：前段无法闭合，保留未解释
                    resolutions.append(_unexplained(open_board, "missing_alight"))
                open_board = ev
            else:  # alight
                if open_board is None:
                    resolutions.append(_unexplained(ev, "missing_board"))
                    continue
                person_legs.extend(_close_leg(open_board, ev, legs, resolutions))
                open_board = None
        if open_board is not None:
            resolutions.append(_unexplained(open_board, "missing_alight"))
        _detect_transfers(person_legs, legs)
        _link_reservations(person_id, person_legs, bundle, legs, resolutions)

    legs.sort(key=lambda r: r["leg_id"])
    resolutions.sort(key=lambda r: (r["person_id"], r["at"], r["kind"]))
    return legs, resolutions


def _close_leg(board: dict, alight: dict, legs: list[dict],
               resolutions: list[dict]) -> list[dict]:
    """一张预约可由两段实际乘车完成；跨调度且无交接证据时拆开并标记待核对。"""
    created = []
    if board["dispatch_id"] == alight["dispatch_id"]:
        created.append(_leg_row(board, alight, split=False,
                                handoff_status="same_dispatch"))
    else:
        # 跨调度：缺交接资料，不能把原车和替班各算一趟完整履约，
        # 拆成两个半段，中间区间作为未解释保留
        first = _leg_row(board, None, split=True,
                         handoff_status="handoff_unverified")
        second = _leg_row(None, alight, split=True,
                          handoff_status="handoff_unverified")
        created.extend([first, second])
        resolutions.append(
            {
                "kind": "unexplained_interval",
                "person_id": base_person_id(board, alight),
                "service_date": (board or alight)["service_date"],
                "at": board["at"],
                "between_dispatch_ids": [board["dispatch_id"], alight["dispatch_id"]],
                "from_credential_id": board["credential_id"],
                "to_credential_id": alight["credential_id"],
                "reason": "cross_dispatch_without_handoff_evidence",
                "detail": (
                    f"原车 {board['dispatch_id']} 与替班 {alight['dispatch_id']} 之间"
                    "缺少交接资料，无法确认乘客是否同车延续，半段保留待核对"
                ),
            }
        )
    legs.extend(created)
    return created


def base_person_id(board: dict | None, alight: dict | None) -> str:
    return (board or alight)["person_id"]


def _leg_row(board: dict | None, alight: dict | None, split: bool,
             handoff_status: str) -> dict:
    base = board or alight
    board_key = board["credential_id"] if board else f"open@{base['dispatch_id']}:{base['stop_id']}"
    alight_key = alight["credential_id"] if alight else f"open@{base['dispatch_id']}:{base['stop_id']}"
    return {
        "leg_id": f"L|{base['person_id']}|{board_key}|{alight_key}",
        "person_id": base["person_id"],
        "service_date": base["service_date"],
        "board_credential_id": board["credential_id"] if board else None,
        "alight_credential_id": alight["credential_id"] if alight else None,
        "board_dispatch_id": board["dispatch_id"] if board else None,
        "alight_dispatch_id": alight["dispatch_id"] if alight else None,
        "board_stop_id": board["stop_id"] if board else None,
        "alight_stop_id": alight["stop_id"] if alight else None,
        "board_at": board["at"] if board else None,
        "alight_at": alight["at"] if alight else None,
        "split_leg": split,
        "handoff_status": handoff_status,
        "transfer_confirmed": False,
        "reservation_id": None,
        "match_status": "unmatched",
        "candidate_reservation_ids": [],
    }


def _detect_transfers(person_legs: list[dict], all_legs: list[dict]) -> None:
    """换乘判定：相邻两段在同一站有明确 alight 后再 board，才算换乘。"""
    complete = sorted(
        (l for l in person_legs if not l["split_leg"]),
        key=lambda l: (l["board_at"] or "", l["alight_at"] or ""),
    )
    for prev, nxt in zip(complete, complete[1:]):
        if (prev["alight_stop_id"] == nxt["board_stop_id"]
                and prev["alight_at"] is not None
                and nxt["board_at"] is not None
                and prev["alight_at"] <= nxt["board_at"]):
            nxt["transfer_confirmed"] = True
            nxt["transfer_from_leg_id"] = prev["leg_id"]


def _link_reservations(person_id: str, person_legs: list[dict],
                       bundle: SourceBundle, all_legs: list[dict],
                       resolutions: list[dict]) -> None:
    reservations = [
        r for r in bundle.table("reservations.json") if r["person_id"] == person_id
    ]
    # 调度 -> 履约机会 -> 线路，用于把乘车段限定在同线路站序内比较
    opp_route = {}
    for d in bundle.table("dispatches.json"):
        opp = d["opportunity_id"]
        trip_id = opp.split("@", 1)[0] if "@" in opp else None
        trip = bundle.row("trips.json", trip_id) if trip_id else None
        opp_route[d["dispatch_id"]] = trip["route_id"] if trip else None

    # 预约起终点在当日的有效站点版本（显式替代、生效期内）
    reservation_windows = []
    for r in reservations:
        day = r["service_date"]
        from_eff = _resolve_stop(bundle, r["from_stop"], parse_date(day))["effective_stop_id"]
        to_eff = _resolve_stop(bundle, r["to_stop"], parse_date(day))["effective_stop_id"]
        reservation_windows.append((r, from_eff, to_eff))

    for leg in person_legs:
        if leg["split_leg"]:
            leg["match_status"] = "pending_handoff"
            continue
        day = leg["service_date"]
        route_id = opp_route.get(leg["board_dispatch_id"]) or opp_route.get(
            leg["alight_dispatch_id"])
        candidates = []
        for r, from_eff, to_eff in reservation_windows:
            if r["service_date"] != day:
                continue
            # 一张预约可由两段实际乘车完成：每段起终点落在预约有效站序区间内即可
            if _segment_within_window(bundle, leg, from_eff, to_eff, day, route_id):
                candidates.append(r)
        leg["candidate_reservation_ids"] = sorted(r["reservation_id"] for r in candidates)
        if len(candidates) == 1:
            leg["reservation_id"] = candidates[0]["reservation_id"]
            leg["match_status"] = "matched"
        elif len(candidates) > 1:
            # 无法唯一连接：保留候选，不强行配对凑预约人数
            leg["match_status"] = "ambiguous"
        else:
            leg["match_status"] = "unmatched"
            resolutions.append(
                {
                    "kind": "unmatched_leg",
                    "person_id": person_id,
                    "service_date": leg["service_date"],
                    "at": leg["board_at"],
                    "leg_id": leg["leg_id"],
                    "reason": "no_reservation_covers_segment",
                    "credential_ids": [x for x in (leg["board_credential_id"],
                                                   leg["alight_credential_id"]) if x],
                }
            )


def _segment_within_window(bundle: SourceBundle, leg: dict,
                           window_from: str | None, window_to: str | None,
                           day_iso: str, route_id: str | None) -> bool:
    """乘车段是否落在预约有效站序窗口 [起, 终] 内（允许等于端点，允许中途换乘）。"""
    if window_from is None or window_to is None:
        return False
    board, alight = leg["board_stop_id"], leg["alight_stop_id"]
    if board is None or alight is None:
        return False
    ordered = _ordered_effective_stops(bundle, day_iso, route_id)
    try:
        i_from, i_board = ordered.index(window_from), ordered.index(board)
        i_alight, i_to = ordered.index(alight), ordered.index(window_to)
    except ValueError:
        return False
    return i_from <= i_board <= i_alight <= i_to


def _ordered_effective_stops(bundle: SourceBundle, day_iso: str,
                             route_id: str | None = None) -> list[str]:
    """当日（可限定线路）全部计划站的有效版本，按模板站序合并去重。"""
    day = parse_date(day_iso)
    ordered: list[str] = []
    trips = bundle.table("trips.json")
    if route_id is not None:
        trips = [t for t in trips if t["route_id"] == route_id]
    for trip in trips:
        for stop_id in trip["stops"]:
            eff = _resolve_stop(bundle, stop_id, day)["effective_stop_id"]
            if eff is not None and eff not in ordered:
                ordered.append(eff)
    # 兜底纳入未出现在模板中的当日有效站，保证 index() 不抛错
    for stop in bundle.table("stops.json"):
        if effective_on(day, stop["from_date"], stop["until_date"]) and stop["stop_id"] not in ordered:
            ordered.append(stop["stop_id"])
    return ordered


def _unexplained(ev: dict, reason: str) -> dict:
    return {
        "kind": "unexplained_event",
        "person_id": ev["person_id"],
        "service_date": ev["service_date"],
        "at": ev["at"],
        "credential_id": ev["credential_id"],
        "action": ev["action"],
        "dispatch_id": ev["dispatch_id"],
        "stop_id": ev["stop_id"],
        "reason": reason,
    }
