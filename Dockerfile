FROM python:3.11-slim

WORKDIR /app

# Ensure unbuffered python output and timezone
ENV PYTHONUNBUFFERED=1
ENV TZ="Asia/Jerusalem"

# Default persistent data paths (mount a Docker volume at /app/data for persistence)
ENV DB_PATH=/app/data/zajel_users.db
ENV APP_KEY_FILE=/app/data/.app_secret.key

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create persistent data directory and run as non-root
# Backups default to /app/data/backups (inside the mounted volume)
RUN mkdir -p /app/data \
    && useradd --create-home --uid 10001 zajel \
    && chown -R zajel:zajel /app
USER zajel

# Declare persistent volume (mount at /app/data)
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
