# echo-agent 镜像：agent-contract v1.0 的最小参考实现
# build（仓库根）：docker build -t echo-agent:latest -f echo_agent/Dockerfile .
#
# 关键：镜像自带入口（CMD 自启 uvicorn），沙箱服务不注入启动命令
# （对应 sandbox_service 默认 AGENT_COMMAND 为空 = 用镜像 CMD）。

FROM python:3.12-slim

WORKDIR /srv

COPY echo_agent/requirements.txt /tmp/requirements.txt
# 默认走清华镜像加速；海外构建可覆盖：--build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r /tmp/requirements.txt

COPY echo_agent /srv/echo_agent

ENV PYTHONPATH=/srv \
    AGENT_PORT=8080 \
    WORKSPACE=/session/workspace \
    DEBUG_DIR=/session/debug

EXPOSE 8080

# 监听 0.0.0.0:${AGENT_PORT}（agent-contract §1）
CMD ["sh", "-c", "uvicorn echo_agent.app:app --host 0.0.0.0 --port ${AGENT_PORT:-8080}"]
