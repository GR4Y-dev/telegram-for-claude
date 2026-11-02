FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py login.py ./

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data

CMD ["python", "server.py"]
