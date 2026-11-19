# 来源资料约定

助老接驳计划、调度、轨迹和乘车原始资料。

所有人员、机构、地址、业务事件及数值均为合成，不来自真实企业或个人。UTF-8 编码，日期为 ISO 8601，时间附明确时区。有效区间左闭右开，空结束值为尚无结束记录，不据此推断未来。金额使用整数分，非整数数量用十进制字符串；未知不是零。

计划机会键为trip_id@service_date，增开用new_opportunity。运营日模板时刻25:10代表次日01:10。replaces只替代明确站段，原车其他已完成段保留。证明对客机会需有效计划、调度及有效村站boarding_open共同成立；GPS只证明位置。未计划却有服务凭据的运行保留额外实驶。乘车缺少换乘上下车证据时保留未解释区间，不补造动作。独立人数、乘车段数和计划机会不同粒度，村日机会按履约键去重。

## calendars

运营日当地04:00至次日04:00。

- calendar_id：日历
- weekdays：星期1到7
- from_date：开始
- until_date：结束

## trips

计划班次模板。

- trip_id：班次
- calendar_id：日历
- route_id：线路
- departure_time：可超过24小时的运营日时刻
- stops：有序站点
- revision：修订

## exceptions

例外仅作用明确班次和服务日。

- exception_id：例外
- trip_id：班次
- service_date：运营日
- action：add或cancel
- new_opportunity：增开键

## dispatches

替班接续的履约身份。

- dispatch_id：调度
- opportunity_id：履约身份
- service_date：运营日
- vehicle：车辆
- replaces：被替班调度
- from_stop：起站
- until_stop：终站

## stops

临时替代站只在明确期间适用。

- stop_id：站点版本
- village：村
- from_date：开始
- until_date：结束
- replaces_stop：明确原站

## vehicle_events

GPS与对客开门凭据不等同。

- event_id：事件
- dispatch_id：调度
- vehicle：车
- stop_id：站
- kind：gps或boarding_open
- at：标准时刻

## reservations

取消是预约状态，不否定已经乘车。

- reservation_id：预约
- person_id：人员
- from_stop：起站
- to_stop：终站
- service_date：运营日
- cancelled：是否取消

## boarding

凭证号重复投递不新增长度或人数。

- credential_id：凭证
- person_id：人员
- reservation_id：预约
- dispatch_id：调度
- stop_id：站
- action：board或alight
- at：时刻
- received_at：收到
