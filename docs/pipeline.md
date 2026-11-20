# 助老接驳事实仓：可重复运行流程

本流程回答的是“某村在某个服务日**实际**得到了哪些接驳机会、老人**实际**完成了哪一段
乘车”，而不是把排班表每一行当成已经出车。计划供给、实际运行、人员乘车三层事实分开
存放，通过可连接而不倍增的线路、站点、服务日维度组成村日与线路服务日数据集。

## 0. 纪律与不变量

| 规则 | 落地位置 |
| --- | --- |
| 服务日 04:00 切断，跨日往返归入发车运营日；25:10 = 次日 01:10 | `warehouse/clock.py` |
| 日历例外优先于周模板；取消一趟不取消同日其他线路 | `canonical._plan_facts` |
| 计划机会键 `trip_id@service_date`，增开使用 `new_opportunity` 新键 | `canonical._opportunity_row` |
| 替班延续原履约身份，一条替班链只算一个机会，不各算一趟 | `canonical._rollup_opportunities` |
| GPS 只证明位置；对客机会需有效计划+调度+有效村站 `boarding_open` | `canonical._run_facts` |
| 站点迁移只在生效期与显式 `replaces_stop` 关系内适用，不按坐标搬历史站 | `canonical._resolve_stop` |
| 凭证号重复投递不新增事实；同车重复刷卡保留事件但不新增段数/人数 | `sources.load_batches` / `canonical._ride_facts` |
| 一张预约可由两段乘车完成；缺交接证据的跨调度乘车拆半段并保留未解释区间 | `canonical._close_leg` |
| 取消是预约状态，不否定已发生乘车 | `canonical._dim_reservations` |
| 无法唯一连接的乘车保留候选与未解释，不强行配对凑人数 | `canonical._link_reservations` |
| 迟到凭证形成修订解释，不覆盖确认快照；未确认范围随新资料更新 | `associate.confirm_scope` / `apply_revision` |

## 1. 分批资料进入（ingest）

每个批次是一个**独立目录**（不可信数据不与脚本混放），内含 `manifest.json` 登记
`batch_id`、文件清单与每个文件的 SHA-256 指纹。装载时校验指纹、引用完整性与枚举值；
同一主键在后续批次出现即覆盖（幂等），boarding 以 `credential_id` 去重。

随包提供两个演示批次（由 `data/` 切分得到，指纹已登记）：

- `batches/b01/`：除 C9/C10 外的全部资料（09:00 前收到的一期凭证）；
- `batches/b02/`：仅 C9/C10 两条迟到的换乘交接凭证（10:00 收到）。

```bash
python -I warehouse/cli.py ingest --state state --batches batches/b01
```

`-I` 隔离模式不读环境变量、不把当前目录隐式加入路径；来源目录始终以参数传入。

## 2. 运行关联（canonical）

ingest 内部一次性重算并落盘三层事实（`state/canonical/*.json`）：

- 计划层：`plan_opportunity`（scheduled/added/cancelled）、`plan_trip_stop`（含
  临时改站后的有效站解析）；
- 运行层：`run_dispatch`（替班链）、`run_stop_service`（GPS 与对客开门分开归并）、
  `run_opportunity`（fulfilled / not_fulfilled / insufficient / cancelled / extra_run）；
- 乘车层：`ride_event`（去重标记）、`ride_leg`（完整段/待核对半段/换乘标记/预约匹配
  状态）、`ride_resolution`（未解释事件与跨调度未解释区间）。

维度表 `dim_stop`、`dim_reservation`、`dim_route_service_day` 提供可连接键。

## 3. 主管部门确认与迟到修订（confirm / revise）

```bash
python -I warehouse/cli.py confirm --state state --date 2026-11-04 \
    --at 2026-11-06T09:30:00+08:00 --authority 交通养老联合工作组 --note 一期履约确认
python -I warehouse/cli.py ingest  --state state --batches batches/b01 batches/b02
python -I warehouse/cli.py revise  --state state --at 2026-11-06T12:00:00+08:00
```

确认快照（`state/confirmations/CF-*.json`，只追加）冻结当时可见批次、凭证集合与乘车段
状态。第二批迟到凭证到达后重算 canonical，再执行 `revise`：系统把新凭证、新乘车段与
状态变化写为 `state/revisions/RV-*.json` 修订解释，**确认快照字节不变**；未确认范围
直接以当前 canonical 为准。

## 4. 发布查询（publish / village-day / person）

```bash
python -I warehouse/cli.py publish --state state --output outputs
# 村日数据集 outputs/village_day.json，线路服务日 outputs/route_service_day.json
```

村级可达班次数按 `opportunity_id` 去重（替班链不倍增）；独立人数按 `person_id`、
乘车段数按 `leg_id`，三种粒度分别统计。

钻取与反查：

```bash
# 从一个村日追到计划未兑现/已兑现/资料不足的机会及依据
python -I warehouse/cli.py village-day --state state --village V2 --date 2026-11-04
# 反查某位合成人员的每段乘车为何算换乘、已匹配或尚待核对
python -I warehouse/cli.py person --state state --person P1
```

`person` 输出含确认/修订审计轨迹：哪些凭证在确认时已冻结、哪些是确认后迟到并仅作
解释。

## 5. 演示资料的预期结论（用于人工核对）

- **2026-11-02（周一）**：T1 被 E1 取消（R1 当日无车）；E2 增开 T2，机会键 `EXTRA1`，
  无调度凭据 → `not_fulfilled`。取消只影响 T1，不影响同日其他线路。
- **2026-11-04（周三）**：T1 模板 25:10 实际于 11-05 01:10 发车，服务日仍为 11-04；
  S2 当日失效、S2N 生效（显式替代），计划经停解析为 S1/S2N/S3；D1 行驶 S1→S2 段、
  D2 替班 S2→S3 段，共享履约身份 `T1@2026-11-04`，三站均有 `boarding_open` →
  `fulfilled`，各村可达班次按一个机会计数。
- **P1**：C1/C9 完成 S1→S2N（D1），C10/C2 完成 S2N→S3（D2），S2N 同站先下后上 →
  两段均唯一匹配预约 R1，第二段标记换乘；C9/C10 迟到，产生修订解释但不改确认快照。
- **P2/P3/P4**：只有 D1 上车与 D2 下车凭证，缺 S2N 交接证据 → 拆为两个待核对半段，
  跨调度区间进入 `ride_resolution`，不补造、不强行配对。P2 的 R2 已取消，但已发生的
  上车/下车事实仍然保留。
- **P5/P6**：无任何凭证，预约不产生乘车事实，村日人数不计入。

## 6. 重跑与离线验证

全流程无外部依赖（Python 3.14 标准库），同批输入重跑得到字节一致的事实表
（`_manifest.json` 时间戳除外，已有幂等测试覆盖）。注意 `-I` 隔离模式不把当前目录
加入模块路径，因此业务命令统一用脚本直调 `python -I warehouse/cli.py ...`；
测试需要仓库根作为顶层目录：

```bash
python -I -m unittest discover -s tests -t .
```
