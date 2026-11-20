"""运行关联与状态管理。

状态目录（默认 state/）保存：
  state/ingest/          每批原始行指纹与装载日志（幂等重跑）
  state/canonical/*.json 规范化后的三层事实（每次装载整体重算）
  state/confirmations/   主管部门确认快照（不可变，只追加）
  state/revisions/       迟到凭证等对未确认范围的修订解释

关键纪律：确认快照记录“当时用哪些资料确认了什么”，迟到凭证只能形成
修订解释，绝不回写覆盖已确认快照；未确认范围随有效新资料重算。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import canonical as canonical_mod
from .sources import load_batches

CANONICAL_TABLES = (
    "dim_stop",
    "dim_reservation",
    "dim_route_service_day",
    "plan_opportunity",
    "plan_trip_stop",
    "run_dispatch",
    "run_stop_service",
    "run_opportunity",
    "ride_event",
    "ride_leg",
    "ride_resolution",
)


class StateError(ValueError):
    pass


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
                    encoding="utf-8")


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def run_ingest(state_dir: str | Path, batch_dirs: list[str | Path]) -> dict:
    """分批装载 -> 规范化 -> 落盘。同一批次集合重跑结果不变。"""
    state = Path(state_dir)
    ingest_log = _read_json(state / "ingest" / "log.json", {"runs": []})
    bundle = load_batches(batch_dirs)
    model = canonical_mod.build(bundle)

    manifest = {
        "generated_at": datetime.now(bundle.local_tz).isoformat(),
        "local_utc_offset": _tz_offset(bundle.local_tz),
        "batches": bundle.batches,
        "counts": {name: len(getattr(model, name)) for name in CANONICAL_TABLES},
    }
    for name in CANONICAL_TABLES:
        _write_json(state / "canonical" / f"{name}.json", getattr(model, name))
    _write_json(state / "canonical" / "_manifest.json", manifest)

    # 批次指纹：重跑同批可核对输入是否漂移
    batch_fingerprints = []
    for batch_dir in [Path(d) for d in batch_dirs]:
        files = {}
        for path in sorted(batch_dir.glob("*.json")):
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        batch_fingerprints.append({"dir": str(batch_dir), "files": files})
    ingest_log["runs"].append(
        {"batches": bundle.batches, "fingerprints": batch_fingerprints,
         "counts": manifest["counts"]}
    )
    _write_json(state / "ingest" / "log.json", ingest_log)
    return manifest


def _tz_offset(tz) -> str:
    """固定偏移时区 -> '+HH:MM' 字符串，便于 manifest 持久化。"""
    total = int(tz.utcoffset(None).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _tz_from_offset(offset: str):
    from datetime import timedelta, timezone
    sign = 1 if offset.startswith("+") else -1
    hours, minutes = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def load_canonical(state_dir: str | Path) -> canonical_mod.Canonical:
    state = Path(state_dir) / "canonical"
    if not (state / "_manifest.json").is_file():
        raise StateError("尚未装载任何批次，请先 ingest")
    payload = {name: _read_json(state / f"{name}.json", []) for name in CANONICAL_TABLES}
    manifest = _read_json(state / "_manifest.json", {})
    payload["local_tz"] = _tz_from_offset(manifest.get("local_utc_offset", "+00:00"))
    return canonical_mod.Canonical(**payload)


# ---------------------------------------------------------------- 确认与修订

def confirm_scope(state_dir: str | Path, scope: dict, confirmed_at: str,
                  confirming_authority: str, note: str = "") -> dict:
    """对当前可见事实做确认快照。

    scope 形如 {"service_date": "2026-11-04"} 或
    {"service_date": "2026-11-04", "route_id": "R1"}。
    快照记录截止时点、可见批次与被冻结的事实键，后续修订不改动它。
    """
    state = Path(state_dir)
    model = load_canonical(state)
    manifest = _read_json(state / "canonical" / "_manifest.json", {})

    def in_scope(row: dict) -> bool:
        return all(row.get(k) == v for k, v in scope.items())

    scoped_legs = [r for r in model.ride_leg if in_scope(r)]
    frozen = {
        "plan_opportunity": [r["opportunity_id"] for r in model.plan_opportunity if in_scope(r)],
        "run_opportunity": [r["opportunity_id"] for r in model.run_opportunity if in_scope(r)],
        # 冻结范围内全部乘车段及其当时状态（含待核对半段），作为修订差分基准
        "ride_leg": [r["leg_id"] for r in scoped_legs],
        "ride_leg_status": {r["leg_id"]: r["match_status"] for r in scoped_legs},
        "ride_event": [r["credential_id"] for r in model.ride_event if in_scope(r)],
    }
    confirmation = {
        "confirmation_id": f"CF-{len(list((state / 'confirmations').glob('*.json'))) + 1:03d}",
        "confirmed_at": confirmed_at,
        "authority": confirming_authority,
        "scope": scope,
        "visible_batches": list(manifest.get("batches", [])),
        "frozen": frozen,
        "note": note,
    }
    _write_json(state / "confirmations" / f"{confirmation['confirmation_id']}.json", confirmation)
    return confirmation


def apply_revision(state_dir: str | Path, revision_at: str) -> dict:
    """以当前事实对比已确认快照，生成迟到资料修订解释。

    - 已确认范围内：只追加 revision 记录（解释变化与迟到凭证），快照不动；
    - 未确认范围：当前 canonical 即最新结论，直接随有效资料更新。
    """
    state = Path(state_dir)
    model = load_canonical(state)
    confirmations = [
        _read_json(p, {}) for p in sorted((state / "confirmations").glob("*.json"))
    ]
    revisions = []
    for cf in confirmations:
        frozen_events = set(cf["frozen"]["ride_event"])
        frozen_legs = set(cf["frozen"]["ride_leg"])
        frozen_leg_status = cf["frozen"].get("ride_leg_status", {})
        scope = cf["scope"]
        late_events = [
            e for e in model.ride_event
            if e["credential_id"] not in frozen_events
            and all(e.get(k) == v for k, v in scope.items())
        ]
        current_legs = [
            l for l in model.ride_leg
            if all(l.get(k) == v for k, v in scope.items())
        ]
        new_legs = [l for l in current_legs if l["leg_id"] not in frozen_legs]
        changed_legs = [
            {"leg_id": l["leg_id"],
             "confirmed_status": frozen_leg_status.get(l["leg_id"]),
             "current_status": l["match_status"]}
            for l in current_legs
            if l["leg_id"] in frozen_leg_status
            and frozen_leg_status[l["leg_id"]] != l["match_status"]
        ]
        # 计划/运行层在确认范围内若因新资料改变，同样只记录、不回写快照
        frozen_run = set(cf["frozen"]["run_opportunity"])
        run_changes = [
            {"opportunity_id": r["opportunity_id"], "run_status": r["run_status"]}
            for r in model.run_opportunity
            if r["opportunity_id"] in frozen_run
        ]
        if not late_events and not new_legs and not changed_legs:
            continue
        rev_dir = state / "revisions"
        prefix = f"RV-{cf['confirmation_id']}"
        prior = sorted(rev_dir.glob(f"{prefix}*.json"))
        signature = {
            "late_credential_ids": sorted(e["credential_id"] for e in late_events),
            "new_leg_ids": sorted(l["leg_id"] for l in new_legs),
            "changed_leg_status": changed_legs,
        }
        if prior:
            last = _read_json(prior[-1], {})
            if all(last.get(k) == v for k, v in signature.items()):
                continue  # 自上次修订以来无实质变化，不重复追加
        seq = len(prior) + 1
        record = {
            "revision_id": f"{prefix}-{seq:03d}",
            "confirmation_id": cf["confirmation_id"],
            "revised_at": revision_at,
            **signature,
            "run_status_at_revision": run_changes,
            "rule": "late_evidence_explains_but_never_overwrites_confirmation",
            "explanation": (
                "确认后到达的凭证仅用于解释已冻结结论的变化，不回写确认快照；"
                "未确认范围以当前规范化事实为准。"
            ),
        }
        _write_json(state / "revisions" / f"{record['revision_id']}.json", record)
        revisions.append(record)
    return {"revisions": revisions}
