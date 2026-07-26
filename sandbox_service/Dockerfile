# 通用沙箱服务镜像
# build（仓库根）：docker build -t sandbox-service:latest -f sandbox_service/Dockerfile .

FROM python:3.12-slim

WORKDIR /srv

COPY sandbox_service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY sandbox_service /srv/sandbox_service

ENV PYTHONPATH=/srv
EXPOSE 8001

CMD ["uvicorn", "sandbox_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
