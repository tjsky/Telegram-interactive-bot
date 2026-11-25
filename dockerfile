FROM python:3.10-alpine

# 不缓冲 stdout，日志才能实时进 docker logs；时区交给 tzdata，否则 scheduler 会按 UTC 跑
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apk add --no-cache tzdata

COPY ./requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY . /app

# 字体在 /app/fonts，跟代码一起打进镜像。
# 只有 assets/ 存数据库，单独声明成卷，挂载时不会把字体遮住。
VOLUME ["/app/assets"]

CMD ["python", "-m", "interactive-bot"]
