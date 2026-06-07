FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Predictions log lives here; mount a volume to persist across runs.
RUN mkdir -p /app/data

EXPOSE 8000

# 2 workers is plenty for a personal dashboard. Live fetch per request.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "60", "webapp:app"]
