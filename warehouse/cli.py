"""命令行入口：ingest -> confirm -> revise -> publish -> query 的可重复流程。

用法（始终以 -I 隔离模式运行，来源目录作为参数传入）：
  python -I -m warehouse.cli ingest --state state --batches batches/b01 batches/b02
  python -I -m warehouse.cli confirm --state state --date 2026-11-04 \
      --at 2026-11-06T09:30:00+08:00 --authority 交通养老联合工作组
  python -I -m warehouse.cli revise --state state --at 2026-11-06T12:00:00+08:00
  python -I -m warehouse.cli publish --state state --output outputs
  python -I -m warehouse.cli village-day --state state --village V2 --date 2026-11-04
  python -I -m warehouse.cli person --state state --person P1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许 `python -I /path/to/warehouse/cli.py` 直接运行（-I 不读 PYTHONPATH）：
# 把仓库根加入 sys.path 后再用绝对包名导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse import associate, publish


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warehouse", description="助老接驳事实仓")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="分批装载并规范化")
    p_ingest.add_argument("--state", required=True)
    p_ingest.add_argument("--batches", nargs="+", required=True)

    p_confirm = sub.add_parser("confirm", help="主管部门确认一期履约资料（冻结快照）")
    p_confirm.add_argument("--state", required=True)
    p_confirm.add_argument("--date", required=True)
    p_confirm.add_argument("--route", default=None)
    p_confirm.add_argument("--at", required=True)
    p_confirm.add_argument("--authority", required=True)
    p_confirm.add_argument("--note", default="")

    p_revise = sub.add_parser("revise", help="迟到资料形成修订解释（不覆盖确认）")
    p_revise.add_argument("--state", required=True)
    p_revise.add_argument("--at", required=True)

    p_publish = sub.add_parser("publish", help="发布村日与线路服务日数据集")
    p_publish.add_argument("--state", required=True)
    p_publish.add_argument("--output", required=True)

    p_vd = sub.add_parser("village-day", help="村日钻取：计划/兑现/资料不足机会")
    p_vd.add_argument("--state", required=True)
    p_vd.add_argument("--village", required=True)
    p_vd.add_argument("--date", required=True)

    p_person = sub.add_parser("person", help="人员反查：换乘/待核对原因")
    p_person.add_argument("--state", required=True)
    p_person.add_argument("--person", required=True)

    p_tables = sub.add_parser("tables", help="打印规范化表清单与条数")
    p_tables.add_argument("--state", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest":
        _print(associate.run_ingest(args.state, args.batches))
    elif args.command == "confirm":
        scope = {"service_date": args.date}
        if args.route:
            scope["route_id"] = args.route
        _print(associate.confirm_scope(args.state, scope, args.at,
                                       args.authority, args.note))
    elif args.command == "revise":
        _print(associate.apply_revision(args.state, args.at))
    elif args.command == "publish":
        _print(publish.publish(args.state, args.output))
    elif args.command == "village-day":
        model = associate.load_canonical(args.state)
        _print(publish.drill_village_day(model, args.village, args.date))
    elif args.command == "person":
        model = associate.load_canonical(args.state)
        _print(publish.trace_person(model, args.person, args.state))
    elif args.command == "tables":
        model = associate.load_canonical(args.state)
        _print({name: len(getattr(model, name))
                for name in associate.CANONICAL_TABLES})
    return 0


if __name__ == "__main__":
    sys.exit(main())
