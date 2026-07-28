FROM python:3.12-slim

# Не писать .pyc, не буферизовать stdout (логи сразу видны в docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data

WORKDIR /app

# Сначала зависимости — кешируется отдельным слоем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY max_source.py max_to_ok_crosspost.py max_to_vk_crosspost.py ./
COPY app ./app

# Каталог состояния (монтируется как volume)
RUN mkdir -p /data
VOLUME ["/data"]

# Площадка выбирается CROSSPOST_TARGET (ok | vk)
CMD ["python", "app/main.py"]
