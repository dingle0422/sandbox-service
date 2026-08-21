# 通用沙箱服务镜像
# build（仓库根）：docker build -t sandbox-service:latest -f sandbox_service/Dockerfile .

FROM python:3.12-slim

WORKDIR /srv

COPY sandbox_service/requirements.txt /tmp/requirements.txt
# 默认走清华镜像加速；海外构建可覆盖：--build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r /tmp/requirements.txt

COPY sandbox_service /srv/sandbox_service

ENV PYTHONPATH=/srv
EXPOSE 8001

CMD ["uvicorn", "sandbox_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
