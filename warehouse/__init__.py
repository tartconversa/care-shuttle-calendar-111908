"""农村助老接驳事实仓。

三层事实分离：
- plan_*  计划供给（日历展开、例外、临时改站后的计划机会）
- run_*   实际运行（调度替班链、车辆事件、对客开门证明）
- ride_*  人员乘车（去重凭证、乘车段、预约匹配、未解释区间）

发布层 village_day / route_service_day 只做可连接而不倍增的聚合。
"""

__all__ = ["clock", "sources", "ingest", "canonical", "associate", "confirm", "publish", "cli"]
