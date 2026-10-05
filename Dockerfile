FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PARK_DB_PATH=/data/park.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seed ./seed
COPY wsgi.py start.sh ./
RUN chmod +x start.sh && mkdir -p /data

EXPOSE 8080

# 初始化数据（仅首次，库为空时导入种子）并启动服务
CMD ["./start.sh"]
