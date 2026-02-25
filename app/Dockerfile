FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl cron tzdata procps \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Chicago

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /logs /data && touch /logs/cron.log

# Install cron schedule
RUN chmod +x /app/cron/cron.sh && crontab /app/cron/crontab

ENV PYTHONUNBUFFERED=1

CMD ["/bin/sh", "-c", "/etc/init.d/cron start && exec uvicorn main:app --host 0.0.0.0 --port 8000"]
