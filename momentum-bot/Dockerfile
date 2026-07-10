FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist state across redeploys by mounting a volume at /app/data and setting
# STATE_DB_PATH=/app/data/state.db TRADES_CSV_PATH=/app/data/trades.csv
RUN useradd --create-home bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

# Runs paper trading by default. See .env.example for the live-mode gate.
CMD ["python", "main.py"]
