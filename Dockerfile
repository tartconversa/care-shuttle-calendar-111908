FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
WORKDIR /work
COPY . /work
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
# 默认仅做来源资料自检；业务流程见 docs/pipeline.md，例如：
#   docker run --rm --network none IMAGE \
#     python -I warehouse/cli.py ingest --state state --batches batches/b01
# 运行测试（-I 下需指定仓库根为顶层目录）：
#   docker run --rm --network none IMAGE \
#     python -I -m unittest discover -s tests -t . -v
CMD ["python", "support/check_inputs.py"]
