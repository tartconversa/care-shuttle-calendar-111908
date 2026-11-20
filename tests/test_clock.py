"""时钟与服务日规则测试。"""
import unittest
from datetime import date, timezone, timedelta

from warehouse.clock import (
    parse_clock, parse_dt, service_date_of, service_window,
    departure_dt, effective_on,
)

TZ = timezone(timedelta(hours=8))


class ClockTests(unittest.TestCase):
    def test_parse_clock_over_24h(self):
        self.assertEqual(parse_clock("25:10:00"), timedelta(hours=25, minutes=10))

    def test_departure_crosses_midnight(self):
        dt = departure_dt(date(2026, 11, 4), "25:10:00", TZ)
        self.assertEqual(dt.isoformat(), "2026-11-05T01:10:00+08:00")

    def test_service_window_is_4am_to_4am(self):
        start, end = service_window(date(2026, 11, 4), TZ)
        self.assertEqual(start.isoformat(), "2026-11-04T04:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-11-05T04:00:00+08:00")

    def test_after_midnight_trip_belongs_to_dispatch_service_day(self):
        # 01:10 的跨日往返归入发车运营日 11-04，而不是 11-05
        self.assertEqual(
            service_date_of(parse_dt("2026-11-05T01:10:00+08:00"), TZ),
            date(2026, 11, 4),
        )
        # 04:00 整属于新服务日（左闭右开）
        self.assertEqual(
            service_date_of(parse_dt("2026-11-05T04:00:00+08:00"), TZ),
            date(2026, 11, 5),
        )

    def test_effective_interval_half_open(self):
        # S2 until_date=2026-11-04：11-03 有效，11-04 失效
        self.assertTrue(effective_on(date(2026, 11, 3), "2026-01-01", "2026-11-04"))
        self.assertFalse(effective_on(date(2026, 11, 4), "2026-01-01", "2026-11-04"))
        # 空结束不推断未来终点
        self.assertTrue(effective_on(date(2030, 1, 1), "2026-01-01", None))
        self.assertFalse(effective_on(date(2025, 12, 31), "2026-01-01", None))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            parse_dt("2026-11-05T01:10:00")


if __name__ == "__main__":
    unittest.main()
