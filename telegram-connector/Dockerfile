FROM python:3.12-slim-bookworm

# Use the image's Debian release, including its security updates. No pip video stack.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py transfers.py video_tools.py download_links.py copy_jobs.py login.py ./

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data

CMD ["python", "server.py"]
