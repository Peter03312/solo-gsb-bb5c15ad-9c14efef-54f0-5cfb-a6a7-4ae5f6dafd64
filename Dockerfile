FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TRACE_DB_PATH=/data/trace.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据目录由 compose 卷持久化；启动入口负责幂等初始化数据库
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 先初始化数据库（建表幂等），再启动 HTTP 服务
CMD ["sh", "-c", "python -m app.db init && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
