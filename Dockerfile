FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py orders.py ./
RUN mkdir -p /data && useradd -r -u 10001 bot && chown bot /data
USER bot
ENV PYTHONUNBUFFERED=1 ORDERS_FILE=/data/orders.json
CMD ["python", "bot.py"]
