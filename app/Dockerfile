FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl cron tzdata procps sqlite3 \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Chicago

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /logs /data && touch /logs/cron.log

# Install runtime scripts
RUN chmod +x /app/cron/cron.sh /app/cron/render-crontab.sh /app/start.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/start.sh"]
