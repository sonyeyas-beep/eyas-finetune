FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py orders.py ./
ENV PYTHONUNBUFFERED=1 ORDERS_FILE=/data/orders.json
VOLUME /data
CMD ["python", "bot.py"]
