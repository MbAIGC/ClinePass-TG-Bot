FROM python:3.11-slim

# 基础环境：不写字节码、日志实时输出、时区为北京时间
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层，改代码时不必重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core.py bot.py ./

# 以非 root 运行；数据目录预留给配置卷
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

# 配置默认落在数据卷里，而不是镜像内
ENV CONFIG_FILE=/app/data/config.json
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
