"""分批来源装载与引用校验。

批次目录是独立目录（不可信数据不与脚本混放），每个批次带 manifest.json：
    {"batch_id": "B01", "files": ["calendars.json", ...],
     "source_hashes": {"boarding.json": "..."}}
- 同一来源实体在后续批次再次出现即按主键覆盖（最新批次优先），天然幂等；
- boarding 以 credential_id 去重，凭证号重复投递不新增长度或人数；
- 迟到与否由 received_at 相对确认时点决定，这里只保留原始 received_at。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .clock import infer_local_tz, parse_dt

SOURCE_FILES = (
    "calendars.json",
    "trips.json",
    "exceptions.json",
    "dispatches.json",
    "stops.json",
    "vehicle_events.json",
    "reservations.json",
    "boarding.json",
)
KEY_FIELD = {
    "calendars.json": "calendar_id",
    "trips.json": "trip_id",
    "exceptions.json": "exception_id",
    "dispatches.json": "dispatch_id",
    "stops.json": "stop_id",
    "vehicle_events.json": "event_id",
    "reservations.json": "reservation_id",
    "boarding.json": "credential_id",
}


class SourceError(ValueError):
    """来源资料违反契约或批次完整性时抛出。"""


@dataclass
class SourceBundle:
    rows: dict[str, dict[str, dict]]                      # 文件 -> 主键 -> 行
    duplicate_credentials: list[dict] = field(default_factory=list)  # 凭证号重复投递
    batches: list[str] = field(default_factory=list)     # 已装载批次，按顺序
    credential_batches: dict[str, str] = field(default_factory=dict)
    local_tz: object = None

    def table(self, name: str) -> list[dict]:
        return list(self.rows.get(name, {}).values())

    def row(self, name: str, key: str) -> dict | None:
        return self.rows.get(name, {}).get(key)


def _load_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise SourceError(f"{path.name} 顶层必须是对象数组")
    return data


def load_batches(batch_dirs: list[str | Path]) -> SourceBundle:
    rows: dict[str, dict[str, dict]] = {name: {} for name in SOURCE_FILES}
    bundle = SourceBundle(rows=rows)
    seen_batch_ids: set[str] = set()

    for batch_dir in [Path(d) for d in batch_dirs]:
        manifest_path = batch_dir / "manifest.json"
        if not manifest_path.is_file():
            raise SourceError(f"批次目录缺少 manifest.json: {batch_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        batch_id = manifest.get("batch_id")
        if not batch_id or batch_id in seen_batch_ids:
            raise SourceError(f"批次编号缺失或重复: {batch_dir}")
        seen_batch_ids.add(batch_id)
        bundle.batches.append(batch_id)

        for name in manifest["files"]:
            if name not in KEY_FIELD:
                raise SourceError(f"批次 {batch_id} 含未登记来源文件: {name}")
            path = (batch_dir / name).resolve()
            if not path.is_relative_to(batch_dir.resolve()):
                raise SourceError(f"来源路径越界: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = (manifest.get("source_hashes") or {}).get(name)
            if expected is not None and digest != expected:
                raise SourceError(f"批次 {batch_id} 的 {name} 与登记指纹不符")
            key_field = KEY_FIELD[name]
            for record in _load_json(path):
                key = record.get(key_field)
                if key is None:
                    raise SourceError(f"{name} 行缺少主键 {key_field}")
                key = str(key)
                if name == "boarding.json" and key in rows[name]:
                    # 凭证号重复投递：登记但不新增事实，长度与人数不增加
                    bundle.duplicate_credentials.append(
                        {"credential_id": key, "batch_id": batch_id})
                    continue
                rows[name][key] = record  # 后批覆盖同键旧值
                if name == "boarding.json":
                    bundle.credential_batches[key] = batch_id

    _validate_references(bundle)
    _validate_enums(bundle)
    bundle.local_tz = infer_local_tz(
        [parse_dt(r["at"]) for r in bundle.table("vehicle_events.json")]
        + [parse_dt(r["at"]) for r in bundle.table("boarding.json")]
    )
    return bundle


def _validate_references(bundle: SourceBundle) -> None:
    """来源契约中的引用必须存在（空值表示无引用，允许）。"""
    checks = [
        ("trips.json", "calendar_id", "calendars.json", "calendar_id", False),
        ("exceptions.json", "trip_id", "trips.json", "trip_id", False),
        ("dispatches.json", "replaces", "dispatches.json", "dispatch_id", True),
        ("stops.json", "replaces_stop", "stops.json", "stop_id", True),
        ("vehicle_events.json", "dispatch_id", "dispatches.json", "dispatch_id", False),
        ("vehicle_events.json", "stop_id", "stops.json", "stop_id", False),
        ("reservations.json", "from_stop", "stops.json", "stop_id", False),
        ("reservations.json", "to_stop", "stops.json", "stop_id", False),
        ("boarding.json", "reservation_id", "reservations.json", "reservation_id", False),
        ("boarding.json", "dispatch_id", "dispatches.json", "dispatch_id", False),
        ("boarding.json", "stop_id", "stops.json", "stop_id", False),
    ]
    for src, field_name, tgt, tgt_field, nullable in checks:
        target_keys = set(bundle.rows[tgt])
        for record in bundle.table(src):
            value = record.get(field_name)
            if nullable and (value is None or value == ""):
                continue
            if str(value) not in target_keys:
                raise SourceError(
                    f"{src} 的 {field_name}={value!r} 在 {tgt} 中不存在"
                )
    # trips.stops 是数组引用
    stop_keys = set(bundle.rows["stops.json"])
    for trip in bundle.table("trips.json"):
        for stop in trip["stops"]:
            if str(stop) not in stop_keys:
                raise SourceError(f"trips {trip['trip_id']} 引用未知站点 {stop}")


def _validate_enums(bundle: SourceBundle) -> None:
    for exc in bundle.table("exceptions.json"):
        if exc["action"] not in ("add", "cancel"):
            raise SourceError(f"例外 {exc['exception_id']} 动作非法: {exc['action']}")
        if exc["action"] == "add" and not exc.get("new_opportunity"):
            raise SourceError(f"增开例外 {exc['exception_id']} 缺少 new_opportunity")
        if exc["action"] == "cancel" and exc.get("new_opportunity"):
            raise SourceError(f"取消例外 {exc['exception_id']} 不应携带 new_opportunity")
    for event in bundle.table("vehicle_events.json"):
        if event["kind"] not in ("gps", "boarding_open"):
            raise SourceError(f"车辆事件 {event['event_id']} 类型非法")
    for credential in bundle.table("boarding.json"):
        if credential["action"] not in ("board", "alight"):
            raise SourceError(f"凭证 {credential['credential_id']} 动作非法")
