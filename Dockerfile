# Build:      docker build -t fastlane .
# Run engine: docker run -d --restart unless-stopped --env-file .env -v fastlane-results:/app/fastlane/results fastlane
# Run API:    docker run -p 127.0.0.1:8787:8787 --env-file .env -v fastlane-results:/app/fastlane/results fastlane \
#               python3 -m uvicorn fastlane.api:app --host 0.0.0.0 --port 8787
# Backups go to fastlane/results/backups inside the same volume; add -v fastlane-backups:/backups -e BACKUP_DIR=/backups
# to keep them on a separate volume.
# Note: --env-file cannot hold multi-line values, so put the Kalshi key body on one line or mount a key file.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fastlane fastlane
ENV PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 1000 app && mkdir -p /app/fastlane/results && chown -R app /app
USER app
CMD ["python3", "-m", "fastlane.run"]
