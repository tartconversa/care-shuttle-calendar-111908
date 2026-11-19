# 农村助老接驳的计划班次与实驶事实仓资料包

助老接驳计划、调度、轨迹和乘车原始资料。

目前提供来源数据、资料约定和输入自检。业务装载、状态处理和查询程序尚未提供；资料校验不代表业务功能已经完成。

## 内容

docs/source-contract.md 解释来源身份、适用范围和业务含义。data/catalog.json 登记来源文件格式、条数、字段与内容摘要。support/check_inputs.py 只检查资料可读性和引用，不生成仓表或业务答案。tests 是资料自检及检查器负向测试。

## 环境与使用

Python 3.14 标准库，无需在线安装依赖。Dockerfile 锁定基础镜像完整摘要；首次构建需要本地镜像或拉取该摘要。

    python support/check_inputs.py
    python -m unittest discover -s tests -v
    docker build -t care-shuttle-calendar-inputs .
    docker run --rm --network none care-shuttle-calendar-inputs

容器默认进行资料自检；可在镜像名后加 python -m unittest discover -s tests -v 运行资料测试。业务程序形成后请补充实际入口、状态目录及操作说明。来源自检只报告输入完整性，不宣称尚未提供的业务能力已验收。
