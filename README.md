# 农村助老接驳的计划班次与实驶事实仓

助老接驳计划、调度、轨迹和乘车原始资料，以及把分批资料转换为三层事实并发布
村日/线路服务日数据集的 Python 事实仓。

## 内容

- `docs/source-contract.md`：来源身份、适用范围和业务含义。
- `data/catalog.json`：来源文件格式、条数、字段、内容摘要与指纹。
- `support/check_inputs.py`：只检查资料可读性和引用，不生成仓表或业务答案。
- `warehouse/`：事实仓程序（标准库，无第三方依赖）。
  - `clock.py` 服务日与跨日时刻；`sources.py` 分批装载、指纹与引用校验；
  - `canonical.py` 计划/运行/乘车三层事实与关联规则；
  - `associate.py` 状态目录、确认快照与迟到修订；`publish.py` 村日、线路日与钻取；
  - `cli.py` 命令行入口。
- `batches/b01`、`batches/b02`：由 `data/` 切分的演示批次（B02 为迟到凭证）。
- `docs/pipeline.md`：从分批进入、运行关联到发布查询的可重复流程与预期结论。
- `tests/`：资料自检、检查器负向测试与事实仓业务规则/端到端测试。

## 环境与使用

Python 3.14 标准库，无需在线安装依赖。建议始终以 `-I` 隔离模式运行，来源目录作为
参数传入，不依赖工作目录。

    python support/check_inputs.py
    python -I -m unittest discover -s tests -t . -v
完整流程（状态与产物落在被 git 忽略的 `state/`、`outputs/`）：

    python -I warehouse/cli.py ingest  --state state --batches batches/b01
    python -I warehouse/cli.py confirm --state state --date 2026-11-04 \
        --at 2026-11-06T09:30:00+08:00 --authority 交通养老联合工作组
    python -I warehouse/cli.py ingest  --state state --batches batches/b01 batches/b02
    python -I warehouse/cli.py revise  --state state --at 2026-11-06T12:00:00+08:00
    python -I warehouse/cli.py publish --state state --output outputs
    python -I warehouse/cli.py village-day --state state --village V2 --date 2026-11-04
    python -I warehouse/cli.py person --state state --person P1

Dockerfile 锁定基础镜像完整摘要；首次构建需要本地镜像或拉取该摘要。

    docker build -t care-shuttle-factstore .
    docker run --rm --network none care-shuttle-factstore
    docker run --rm --network none care-shuttle-factstore \
        python -I -m unittest discover -s tests -t . -v

容器默认进行资料自检；业务程序入口、状态目录与操作说明见 `docs/pipeline.md`。
来源自检只报告输入完整性，业务结论以事实仓表与查询为准。
