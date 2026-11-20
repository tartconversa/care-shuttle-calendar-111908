"""时间与服务日工具。

服务日规则（来源契约）：运营日为当地 04:00 至次日 04:00，跨日往返仍归入
发车的运营日；模板时刻可超过 24 小时（25:10 即次日 01:10）。
所有来源时刻均带时区，本模块只做带时区运算，不做朴素时间比较。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

SERVICE_BREAK_HOUR = 4  # 运营日在当地凌晨 04:00 切断


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_dt(value: str) -> datetime:
    """解析带明确时区的 ISO 8601 时刻。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时刻缺少时区: {value}")
    return dt


def parse_clock(value: str) -> timedelta:
    """解析可超过 24 小时的运营日时刻，如 25:10:00 -> 25h10m。"""
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"时刻格式无效: {value}")
    hours, minutes, seconds = (int(p) for p in parts)
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def infer_local_tz(timestamps: list[datetime]) -> timezone:
    """从来源时刻推断当地固定时区（资料中全部为同一偏移，如 +08:00）。"""
    offsets = {dt.utcoffset() for dt in timestamps}
    if not offsets:
        return timezone.utc
    if len(offsets) > 1:
        # 合成资料约定当地统一时区；出现混合偏移时显式报错，不静默挑选。
        raise ValueError(f"来源时刻存在多个 UTC 偏移，无法确定当地时区: {sorted(map(str, offsets))}")
    return timezone(offsets.pop())


def service_window(service_date: date, local_tz: timezone) -> tuple[datetime, datetime]:
    """返回服务日的 [起, 止) 时刻，左闭右开。"""
    start = datetime.combine(service_date, time(SERVICE_BREAK_HOUR, 0), local_tz)
    return start, start + timedelta(days=1)


def service_date_of(dt: datetime, local_tz: timezone) -> date:
    """把任意带时区时刻归入运营日：当地 04:00 前属于前一自然日。"""
    local = dt.astimezone(local_tz)
    if local.time() < time(SERVICE_BREAK_HOUR, 0):
        return local.date() - timedelta(days=1)
    return local.date()


def departure_dt(service_date: date, clock_value: str, local_tz: timezone) -> datetime:
    """模板时刻加上运营日，得到实际墙钟时刻（允许跨日）。"""
    return datetime.combine(service_date, time(0, 0), local_tz) + parse_clock(clock_value)


def effective_on(service_date: date, from_date: str, until_date: str | None) -> bool:
    """左闭右开的生效判断；空结束代表尚无结束记录，不推断未来终点。"""
    start = parse_date(from_date)
    if service_date < start:
        return False
    if until_date is not None and service_date >= parse_date(until_date):
        return False
    return True
